import Foundation
import Testing
@testable import JarvisClient

actor FakeSocket: HubSocket {
    nonisolated let frames: AsyncThrowingStream<SocketMessage, any Error>
    nonisolated let input: AsyncThrowingStream<SocketMessage, any Error>.Continuation
    var sent: [HubCommand] = []
    var closed = false
    var holdClose = false
    var closeWaiter: CheckedContinuation<Void, Never>?
    let autoPresence: Bool
    init(autoPresence: Bool = true) { self.autoPresence = autoPresence; (frames, input) = AsyncThrowingStream.makeStream() }
    func receive() async throws -> SocketMessage {
        var iterator = frames.makeAsyncIterator()
        guard let next = try await iterator.next() else { throw URLError(.networkConnectionLost) }
        return next
    }
    func send(_ data: Data) async throws {
        let command = try JSONDecoder().decode(HubCommand.self, from: data)
        sent.append(command)
        if autoPresence, command.type == "set_presence", command.payload["can_show_text"] == .bool(true) {
            input.yield(.text(Data("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(command.id)\",\"ok\":true,\"result\":{\"can_show_text\":true}}".utf8)))
        }
    }
    func close() async {
        if closed { return }
        closed = true
        if holdClose { await withCheckedContinuation { closeWaiter = $0 } }
        input.finish()
    }
    func blockClose() { holdClose = true }
    func releaseClose() { holdClose = false; closeWaiter?.resume(); closeWaiter = nil }
    func text(_ text: String) { input.yield(.text(Data(text.utf8))) }
}

actor FakeTransport {
    var sockets: [FakeSocket] = []
    var cursors: [Int64] = []
    let autoPresence: Bool
    init(autoPresence: Bool = true) { self.autoPresence = autoPresence }
    func connect(_ after: Int64) -> any HubSocket {
        let socket = FakeSocket(autoPresence: autoPresence); sockets.append(socket); cursors.append(after); return socket
    }
    func socket(_ index: Int) -> FakeSocket? { sockets.indices.contains(index) ? sockets[index] : nil }
}

func eventually(_ condition: @escaping () async -> Bool) async throws {
    for _ in 0..<1000 { if await condition() { return }; try await Task.sleep(for: .milliseconds(1)) }
    Issue.record("Condition did not become true")
}

@Test func reconnectRetainsCursorAndOriginalCommand() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { after in await transport.connect(after) }), sleep: { _ in })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    await #expect(throws: HubProtocolError.self) { try await client.send(HubCommand(id: "early", type: "say_text")) }
    let first = try #require(await transport.socket(0))
    await first.text(#"{"version":1,"type":"replay_complete","through_seq":7,"oldest_seq":null}"#)
    try await eventually { await client.state == .ready }
    let command = HubCommand(id: "same", type: "say_text", payload: ["text": .string("hello")])
    try await client.send(command)
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.text(#"{"version":1,"type":"replay_complete","through_seq":9,"oldest_seq":null}"#)
    try await eventually { await second.sent.contains(command) }
    #expect(await transport.cursors == [0, 7])
    await second.text(#"{"version":1,"type":"command_result","id":"same","ok":false,"error":{"code":"outcome_unknown","message":"Check history","retry_after_seconds":null}}"#)
    try await eventually { await client.pendingCommands.isEmpty }
    #expect(await client.results["same"]?.error?.code == "outcome_unknown")
    await client.stop()
}

@Test func backgroundReplyRequiresPresenceAcknowledgementThenRetiresAtDrain() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await client.state == .ready }
    await socket.text(event(1, "speech_begin", #"{"speech_id":"draining","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await client.cursor == 1 }
    let transition = Task { await client.setForeground(false, playingSpeechID: "draining") }
    try await eventually { await socket.sent.contains(where: { $0.payload["can_show_text"] == .bool(false) }) }
    let presence = try #require(await socket.sent.first(where: { $0.payload["can_show_text"] == .bool(false) }))
    #expect(presence.payload == ["can_show_text": .bool(false)])
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await transition.value
    #expect(await socket.closed == false)
    try await client.reportPlayback(HubCommand(id: "played", type: "playback_report", payload: ["speech_id": .string("draining")]))
    await client.pathChanged()
    #expect(await transport.sockets.count == 1)
    await client.playbackDrained(speechID: "stale-playback")
    #expect(await socket.closed == false)
    await client.playbackDrained(speechID: "draining")
    #expect(await socket.closed)
    await client.pathChanged()
    #expect(await transport.sockets.count == 1)
    await client.setForeground(true)
    try await eventually { await transport.socket(1) != nil }
    await client.stop()
}

@Test func backgroundIdleAndStaleReadersCannotReplaceForegroundConnection() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let old = try #require(await transport.socket(0))
    await client.pathChanged()
    try await eventually { await transport.socket(1) != nil }
    let current = try #require(await transport.socket(1))
    old.input.finish(throwing: URLError(.networkConnectionLost))
    await current.text(#"{"version":1,"type":"replay_complete","through_seq":4}"#)
    try await eventually { await client.state == .ready }
    #expect(await current.closed == false)
    await client.setForeground(false)
    #expect(await current.closed)
    await client.pathChanged()
    await client.start()
    #expect(await transport.sockets.count == 2)
    await #expect(throws: HubProtocolError.self) { try await client.send(HubCommand(type: "say_text")) }
    await client.stop()
}

actor FakeSleeper {
    var waits: [Double] = []
    var waiter: CheckedContinuation<Void, any Error>?
    func sleep(_ seconds: Double) async throws {
        waits.append(seconds)
        try await withCheckedThrowingContinuation { waiter = $0 }
    }
    func advance() { waiter?.resume(); waiter = nil }
}

@Test func retryBackoffIsBoundedAndResetsAfterReady() async throws {
    let clock = FakeSleeper()
    actor Attempts {
        var attempts = 0
        let socket = FakeSocket()
        func connect() throws -> any HubSocket {
            attempts += 1
            if attempts == 7 { return socket }
            throw URLError(.timedOut)
        }
    }
    let attempts = Attempts()
    let client = HubClient(transport: HubTransport(connect: { _ in try await attempts.connect() }),
                           sleep: { try await clock.sleep($0) }, jitter: { 1 })
    await client.start()
    for count in 1...6 {
        try await eventually { await clock.waits.count == count }
        await clock.advance()
    }
    #expect(await clock.waits == [1, 2, 4, 8, 15, 15])
    let socket = attempts.socket
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await client.state == .ready }
    socket.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await clock.waits.count == 7 }
    #expect(await clock.waits.last == 1)
    await client.stop()
    await clock.advance()
}

@Test func foregroundReadinessWaitsForFreshPresenceAndBackgroundCannotEnableIt() async throws {
    let transport = FakeTransport(autoPresence: false)
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await first.sent.contains(where: { $0.type == "set_presence" }) }
    #expect(await client.state == .connecting)
    await client.setForeground(false)
    #expect(await first.closed)
    await client.setForeground(true)
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await second.sent.contains(where: { $0.type == "set_presence" }) }
    let firstID = try #require(await first.sent.first?.id)
    let secondID = try #require(await second.sent.first?.id)
    #expect(firstID != secondID)
    await second.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(secondID)\",\"ok\":true,\"result\":{\"can_show_text\":true}}")
    try await eventually { await client.state == .ready }
    await client.stop()
}

actor CapturedUpdates {
    var events: [HubEvent] = []
    var clips: [Data] = []
    func record(_ update: HubClientUpdate) {
        if case .event(let event) = update { events.append(event) }
        if case .audio(_, let data) = update { clips.append(data) }
    }
}

@Test func pythonTypedExchangeRunsThroughClientWithoutCopyingFrames() async throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let fixture = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(fixture["exchanges"] as? [String: Any])
    let exchange = try #require(exchanges["typed_partial"] as? [String: Any])
    let frames = try #require(exchange["events"] as? [[String: Any]])
    let initial = try #require(exchange["initial"] as? [[String: Any]])
    let replay = try #require(initial.first(where: { $0["type"] as? String == "replay_complete" }))
    let command = try JSONDecoder().decode(HubCommand.self, from: JSONSerialization.data(withJSONObject: try #require(exchange["command"])))
    let expected = try frames.map { try HubFrame.decode(JSONSerialization.data(withJSONObject: $0)) }
    let transport = FakeTransport()
    let captured = CapturedUpdates()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    socket.input.yield(.text(try JSONSerialization.data(withJSONObject: replay)))
    try await eventually { await client.state == .ready }
    try await client.send(command)
    for frame in frames { socket.input.yield(.text(try JSONSerialization.data(withJSONObject: frame))) }
    socket.input.yield(.text(try JSONSerialization.data(withJSONObject: try #require(exchange["result"]))))
    try await eventually { await client.results[command.id] != nil }
    try await eventually { await captured.events.count == expected.count }
    #expect(await captured.events.map(HubFrame.event) == expected)
    #expect(await socket.sent.contains(command))
    await client.stop(); reader.cancel()
}

func event(_ seq: Int, _ type: String, _ payload: String) -> String {
    "{\"version\":1,\"seq\":\(seq),\"time\":\"2026-11-19T00:00:00Z\",\"type\":\"\(type)\",\"payload\":\(payload)}"
}

@Test func audioPairingIsSerialAndMalformedBinaryClosesConnection() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    let captured = CapturedUpdates()
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await client.state == .ready }
    await socket.text(event(1, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(2, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"a","byte_length":4}"#))
    socket.input.yield(.binary(Data([0, 0, 1, 0])))
    try await eventually { await client.cursor == 2 }
    #expect(await captured.clips == [Data([0, 0, 1, 0])])
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":1,"text":"b","byte_length":4}"#))
    socket.input.yield(.binary(Data([0, 0])))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 2)
    #expect(await captured.clips.count == 1)
    await client.stop(); reader.cancel()
}

@Test func historyAndDuplicateEventsNeverRewindTheCursor() async throws {
    let transport = FakeTransport()
    let history = Data(#"{"version":1,"events":[{"version":1,"seq":2,"time":"2026-11-19T00:00:00Z","type":"notice","client_id":null,"payload":{"text":"older"}}],"next_before":2,"has_more":false}"#.utf8)
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }, request: { _ in history }))
    let captured = CapturedUpdates()
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":10,"history_pruned":true}"#)
    try await eventually { await client.state == .ready }
    await socket.text(event(14, "future_event", #"{"optional":true}"#))
    await socket.text(event(14, "future_event", #"{"optional":true}"#))
    await socket.text(event(12, "notice", #"{"text":"duplicate older event"}"#))
    await socket.text(event(20, "notice", #"{"text":"new"}"#))
    try await eventually { await client.cursor == 20 }
    let page = try await client.history(before: 10)
    #expect(page.events.first?.seq == 2)
    #expect(await client.cursor == 20)
    #expect(await captured.events.map(\.seq) == [14, 20])
    await client.stop(); reader.cancel()
}

@Test func failedDurableCacheDoesNotAdvanceCursor() async throws {
    let transport = FakeTransport()
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           sleep: { try await clock.sleep($0) }, cache: { event, _ in
        if event != nil { throw CocoaError(.fileWriteOutOfSpace) }
    })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":5}"#)
    try await eventually { await client.state == .ready }
    await socket.text(event(6, "notice", #"{"text":"must not be lost"}"#))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 5)
    await client.stop(); await clock.advance()
}

@Test func failedBackgroundPresenceClosesInsteadOfLeavingDisplayEligibilityAlive() async throws {
    let transport = FakeTransport()
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), lifecycleSleep: { try await clock.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await client.state == .ready }
    await socket.text(event(1, "speech_begin", #"{"speech_id":"draining","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await client.cursor == 1 }
    let transition = Task { await client.setForeground(false, playingSpeechID: "draining") }
    try await eventually { await clock.waits.count == 1 }
    await clock.advance()
    await transition.value
    #expect(await socket.closed)
    #expect(await client.state == .suspended)
    await client.pathChanged()
    #expect(await transport.sockets.count == 1)
    await client.stop()
}

@Test(arguments: [HubTransportError.http(502), .http(503), .unauthorized, .incompatibleProtocol])
func actualTransportOutcomesDetermineConnectionState(_ error: HubTransportError) async throws {
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { _ in throw error }), sleep: { try await clock.sleep($0) })
    await client.start()
    let expected: HubConnectionState = switch error {
    case .http: .hubStopped
    case .unauthorized: .unpaired
    case .incompatibleProtocol: .incompatibleProtocol
    }
    try await eventually { await client.state == expected }
    await client.stop(); await clock.advance()
}

@Test func injectedPathChangesReplaceAttemptWithoutChangingCursor() async throws {
    let transport = FakeTransport()
    let (paths, changes) = AsyncStream<Void>.makeStream()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), cursor: 42)
    await client.start(paths: paths)
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    changes.yield(())
    try await eventually { await transport.socket(1) != nil }
    #expect(await first.closed)
    #expect(await transport.cursors == [42, 42])
    await client.setForeground(false)
    changes.yield(())
    #expect(await transport.sockets.count == 2)
    changes.finish(); await client.stop()
}

@Test func invalidOriginsFailBeforeCreatingAnyNetworkRequest() throws {
    for raw in ["http://mac.example", "https://name@mac.example", "https://mac.example/path", "https://mac.example?token=secret"] {
        #expect(throws: HubProtocolError.self) {
            try HubTransport.urlSession(origin: #require(URL(string: raw)), token: "fake-token")
        }
    }
}

@Test func backgroundDrainCannotStartAnotherSpeechStream() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await client.state == .ready }
    await socket.text(event(1, "speech_begin", #"{"speech_id":"old","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await client.cursor == 1 }
    let transition = Task { await client.setForeground(false, playingSpeechID: "old") }
    try await eventually { await socket.sent.contains(where: { $0.payload["can_show_text"] == .bool(false) }) }
    let presence = try #require(await socket.sent.last)
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await transition.value
    await socket.text(event(2, "speech_begin", #"{"speech_id":"new","reply_id":"other","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 1)
    #expect(await transport.sockets.count == 1)
    await client.stop()
}

@Test func overlappingPathReplacementsCannotCreateTwoCurrentReaders() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.blockClose()
    let oldReplacement = Task { await client.pathChanged() }
    try await eventually { await first.closed }
    await client.pathChanged()
    try await eventually { await transport.socket(1) != nil }
    await first.releaseClose()
    await oldReplacement.value
    try await Task.sleep(for: .milliseconds(10))
    #expect(await transport.sockets.count == 2)
    await client.stop()
}
