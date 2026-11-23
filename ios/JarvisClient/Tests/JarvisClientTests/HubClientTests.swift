import Foundation
import Testing
@testable import JarvisClient

actor FakeSocket: HubSocket {
    nonisolated let frames: AsyncThrowingStream<SocketMessage, any Error>
    nonisolated let input: AsyncThrowingStream<SocketMessage, any Error>.Continuation
    var sent: [HubCommand] = []
    var rawSent: [Data] = []
    var binarySent: [Data] = []
    var closed = false
    var holdClose = false
    var closeWaiter: CheckedContinuation<Void, Never>?
    var heldSendType: String?
    var failingSendType: String?
    var sendWaiter: CheckedContinuation<Void, Never>?
    let autoPresence: Bool
    init(autoPresence: Bool = true) { self.autoPresence = autoPresence; (frames, input) = AsyncThrowingStream.makeStream() }
    func receive() async throws -> SocketMessage {
        var iterator = frames.makeAsyncIterator()
        guard let next = try await iterator.next() else { throw URLError(.networkConnectionLost) }
        return next
    }
    func send(_ data: Data) async throws {
        let command = try JSONDecoder().decode(HubCommand.self, from: data)
        if command.type == failingSendType { throw URLError(.networkConnectionLost) }
        sent.append(command)
        rawSent.append(data)
        if command.type == heldSendType { await withCheckedContinuation { sendWaiter = $0 } }
        if autoPresence, command.type == "set_presence", command.payload["can_show_text"] == .bool(true) {
            input.yield(.text(Data("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(command.id)\",\"ok\":true,\"result\":{\"can_show_text\":true}}".utf8)))
        }
    }
    func sendBinary(_ data: Data) async throws { binarySent.append(data) }
    var pings = 0
    var pingFails = false
    func ping() async throws {
        pings += 1
        if pingFails { throw URLError(.timedOut) }
    }
    func failPings() { pingFails = true }
    func close() async {
        if closed { return }
        closed = true
        if holdClose { await withCheckedContinuation { closeWaiter = $0 } }
        input.finish()
    }
    func blockClose() { holdClose = true }
    func releaseClose() { holdClose = false; closeWaiter?.resume(); closeWaiter = nil }
    func blockSend(_ type: String) { heldSendType = type }
    func failSend(_ type: String) { failingSendType = type }
    func releaseSend(keepingBlocked: Bool = false) {
        if !keepingBlocked { heldSendType = nil }
        let waiter = sendWaiter; sendWaiter = nil; waiter?.resume()
    }
    func text(_ text: String) { input.yield(.text(Data(text.utf8))) }
    func ready(through: Int64 = 0) {
        input.yield(.text(Data("{\"version\":1,\"seq\":\(through + 1),\"time\":\"2026-11-19T00:00:00Z\",\"type\":\"status\",\"client_id\":\"test-status\",\"payload\":{\"ready\":true,\"usage_limit\":{\"limited\":false,\"resets_at\":null},\"quiet\":false,\"focus_client_id\":null,\"mac_voice_on\":true,\"silence\":false}}".utf8)))
        text("{\"version\":1,\"type\":\"replay_complete\",\"through_seq\":\(through + 1)}")
    }
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
    await first.ready(through: 7)
    try await eventually { await client.state == .ready }
    let command = HubCommand(id: "same", type: "say_text", payload: ["text": .string("hello")])
    try await client.send(command)
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 9)
    try await eventually { await second.sent.contains(command) }
    #expect(await transport.cursors == [0, 8])
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
    await socket.ready(through: 0)
    try await eventually { await client.state == .ready }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"draining","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await client.cursor == 2 }
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
    await current.ready(through: 4)
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
    var waiters: [UUID: CheckedContinuation<Void, any Error>] = [:]
    var order: [UUID] = []
    var pending: Int { waiters.count }
    func sleep(_ seconds: Double) async throws {
        let id = UUID()
        waits.append(seconds)
        try await withTaskCancellationHandler {
            try Task.checkCancellation()
            try await withCheckedThrowingContinuation { waiters[id] = $0; order.append(id) }
        } onCancel: { Task { await self.cancel(id) } }
    }
    private func cancel(_ id: UUID) { waiters.removeValue(forKey: id)?.resume(throwing: CancellationError()) }
    func advance() {
        while !order.isEmpty {
            if let waiter = waiters.removeValue(forKey: order.removeFirst()) { waiter.resume(); return }
        }
    }
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
    await socket.ready(through: 0)
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
    await first.ready(through: 0)
    try await eventually { await first.sent.contains(where: { $0.type == "set_presence" }) }
    #expect(await client.state == .connecting)
    await client.setForeground(false)
    #expect(await first.closed)
    await client.setForeground(true)
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 1)
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
        if case .event(let event) = update, event.client_id != "test-status" { events.append(event) }
        if case .audio(_, let data) = update { clips.append(data) }
    }
}

@Test func pythonTypedExchangeRunsThroughClientWithoutCopyingFrames() async throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let fixture = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(fixture["exchanges"] as? [String: Any])
    let exchange = try #require(exchanges["typed_partial"] as? [String: Any])
    let command = try JSONDecoder().decode(HubCommand.self, from: JSONSerialization.data(withJSONObject: try #require(exchange["command"])))
    let streams = exchanges["typed_wire_streams"] as? [[[String: Any]]]
    let frames = try streams?.first ?? #require(exchange["events"] as? [[String: Any]])
    let expected = try frames.filter { $0["seq"] != nil }.map { try HubFrame.decode(JSONSerialization.data(withJSONObject: $0)) }
    let transport = FakeTransport()
    let captured = CapturedUpdates()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    if streams == nil {
        let initial = try #require(exchange["initial"] as? [[String: Any]])
        let replay = try #require(initial.first(where: { $0["type"] as? String == "replay_complete" }))
        socket.input.yield(.text(try JSONSerialization.data(withJSONObject: replay)))
    }
    var submitted = false
    for frame in frames {
        if streams != nil, !submitted, frame["type"] as? String == "heard" {
            try await eventually { await client.state == .ready }
            try await client.send(command)
            submitted = true
        }
        socket.input.yield(.text(try JSONSerialization.data(withJSONObject: frame)))
    }
    try await eventually { await captured.events.count == expected.count }
    #expect(await captured.events.map(HubFrame.event) == expected)
    if streams != nil {
        try await eventually { await client.results[command.id] != nil }
        #expect(await socket.sent.contains(command))
    }
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
    await socket.ready(through: 0)
    try await eventually { await client.state == .ready }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"a","byte_length":4}"#))
    socket.input.yield(.binary(Data([0, 0, 1, 0])))
    try await eventually { await client.cursor == 3 }
    try await eventually { await captured.clips.count == 1 }
    #expect(await captured.clips == [Data([0, 0, 1, 0])])
    await socket.text(event(4, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":1,"text":"b","byte_length":4}"#))
    socket.input.yield(.binary(Data([0, 0])))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 3)
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
    await socket.ready(through: 10)
    try await eventually { await client.state == .ready }
    await socket.text(event(14, "future_event", #"{"optional":true}"#))
    await socket.text(event(14, "future_event", #"{"optional":true}"#))
    await socket.text(event(12, "notice", #"{"text":"duplicate older event"}"#))
    await socket.text(event(20, "notice", #"{"text":"new"}"#))
    try await eventually { await client.cursor == 20 }
    let page = try await client.history(before: 10)
    #expect(page.events.first?.seq == 2)
    #expect(await client.cursor == 20)
    try await eventually { await captured.events.last?.seq == 20 }
    #expect(await captured.events.map(\.seq) == [14, 20])
    await client.stop(); reader.cancel()
}

@Test func failedDurableCacheDoesNotAdvanceCursor() async throws {
    let transport = FakeTransport()
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           sleep: { try await clock.sleep($0) }, cache: { event, _ in
        if event?.kind == .notice { throw CocoaError(.fileWriteOutOfSpace) }
    })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready(through: 5)
    try await eventually { await client.state == .ready }
    await socket.text(event(7, "notice", #"{"text":"must not be lost"}"#))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 6)
    await client.stop(); await clock.advance()
}

@Test func failedBackgroundPresenceClosesInsteadOfLeavingDisplayEligibilityAlive() async throws {
    let transport = FakeTransport()
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), lifecycleSleep: { try await clock.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready(through: 0)
    try await eventually { await client.state == .ready }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"draining","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await client.cursor == 2 }
    try await eventually { await clock.pending == 0 }
    let transition = Task { await client.setForeground(false, playingSpeechID: "draining") }
    try await eventually { await clock.pending == 1 }
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

@Test func pathChangesProbeAWorkingSocketAndOnlyReplaceAFailedOne() async throws {
    let transport = FakeTransport()
    let (paths, changes) = AsyncStream<Void>.makeStream()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), cursor: 42,
                           sleep: { _ in try await Task.sleep(for: .seconds(100)) })
    await client.start(paths: paths)
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready(through: 42)
    try await eventually { await client.state == .ready }
    changes.yield(())
    try await eventually { await first.pings == 1 }
    try await Task.sleep(for: .milliseconds(10))
    #expect(await first.closed == false)
    #expect(await transport.sockets.count == 1)
    #expect(await client.state == .ready)
    await first.failPings()
    changes.yield(())
    try await eventually { await client.state == .unreachable }
    #expect(await first.closed)
    changes.yield(())
    try await eventually { await transport.socket(1) != nil }
    #expect(await transport.cursors == [42, 43])
    await client.setForeground(false)
    changes.yield(())
    #expect(await transport.sockets.count == 2)
    changes.finish(); await client.stop()
}

@Test func keepaliveClosesASocketWhoseHubStoppedAnswering() async throws {
    let transport = FakeTransport(), clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in },
                           keepaliveSleep: { try await clock.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await client.state == .ready }
    try await eventually { await clock.pending == 1 }
    await clock.advance()
    try await eventually { await first.pings == 1 }
    #expect(await first.closed == false)
    await first.failPings()
    try await eventually { await clock.pending == 1 }
    await clock.advance()
    try await eventually { await transport.socket(1) != nil }
    #expect(await first.closed)
    await client.stop()
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
    await socket.ready(through: 0)
    try await eventually { await client.state == .ready }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"old","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await client.cursor == 2 }
    let transition = Task { await client.setForeground(false, playingSpeechID: "old") }
    try await eventually { await socket.sent.contains(where: { $0.payload["can_show_text"] == .bool(false) }) }
    let presence = try #require(await socket.sent.last)
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await transition.value
    await socket.text(event(3, "speech_begin", #"{"speech_id":"new","reply_id":"other","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 2)
    #expect(await transport.sockets.count == 1)
    await client.stop()
}

@Test func runningWarmupAndMissingStatusStayConnectingUntilPositiveReadiness() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":0}"#)
    try await eventually { await socket.sent.contains(where: { $0.type == "set_presence" }) }
    try await Task.sleep(for: .milliseconds(5))
    #expect(await client.state == .connecting)
    await socket.text(event(1, "status", #"{"ready":false,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":null,"mac_voice_on":true,"silence":false}"#))
    try await eventually { await client.cursor == 1 }
    #expect(await client.state == .connecting)
    await #expect(throws: HubProtocolError.self) { try await client.send(HubCommand(type: "say_text")) }
    await socket.text(event(2, "status", #"{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":null,"mac_voice_on":true,"silence":false}"#))
    try await eventually { await client.state == .ready }
    try await client.send(HubCommand(id: "after-warmup", type: "say_text"))
    await client.stop()
}

@Test func invalidLocalCommandsNeverEnterPendingOrDamageHealthyConnection() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    let commands = [
        HubCommand(id: "nan", type: "say_text", payload: ["number": .number(.nan)]),
        HubCommand(id: "inf", type: "say_text", payload: ["number": .number(.infinity)]),
        HubCommand(id: "negative-inf", type: "say_text", payload: ["number": .number(-.infinity)]),
        HubCommand(id: "huge", type: "say_text", payload: ["text": .string(String(repeating: "a", count: 65_537))]),
        HubCommand(id: "", type: "say_text"),
        HubCommand(id: "control\n", type: "say_text"),
        HubCommand(id: String(repeating: "é", count: 129), type: "say_text"),
        HubCommand(id: "bad-type", type: ""),
    ]
    for command in commands {
        await #expect(throws: (any Error).self) { try await client.send(command) }
        #expect(await client.pendingCommands.isEmpty)
        #expect(await socket.closed == false)
    }
    let valid = HubCommand(id: "valid", type: "say_text", payload: ["text": .string("hello")])
    try await client.send(valid)
    socket.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 1)
    try await eventually { await second.sent.contains(valid) }
    #expect(await second.sent.filter { $0.type != "set_presence" } == [valid])
    await second.text(#"{"version":1,"type":"command_result","id":"valid","ok":true,"result":{"request_id":"request-1"}}"#)
    try await eventually { await client.pendingCommands.isEmpty }
    await client.stop()
}

@Test func healthySocketRetriesExactBytesThenKeepsUnansweredOutcomeVisible() async throws {
    let transport = FakeTransport()
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           acknowledgementSleep: { try await clock.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    let command = HubCommand(id: "unanswered", type: "say_text", payload: ["text": .string("hello")])
    try await client.send(command)
    for attempt in 1...3 {
        try await eventually { await clock.waits.count == attempt }
        await clock.advance()
        if attempt < 3 { try await eventually { await socket.sent.filter { $0.id == command.id }.count == attempt + 1 } }
    }
    try await eventually { await client.unansweredCommands.contains(command.id) }
    #expect(await clock.waits == [5, 5, 5])
    let sent = await socket.rawSent.filter { (try? JSONDecoder().decode(HubCommand.self, from: $0).id) == command.id }
    #expect(sent.count == 3)
    #expect(Set(sent).count == 1)
    #expect(await client.pendingCommands == [command])
    #expect(await socket.closed == false)
    await socket.text(#"{"version":1,"type":"command_result","id":"unanswered","ok":false,"error":{"code":"outcome_unknown","message":"Check history","retry_after_seconds":null}}"#)
    try await eventually { await client.pendingCommands.isEmpty }
    #expect(await client.results[command.id]?.error?.code == "outcome_unknown")
    #expect(await client.unansweredCommands.isEmpty)
    await client.stop()
}

@Test func acknowledgementCancelsDeadlineWithoutRetryingOrDamagingConnection() async throws {
    let transport = FakeTransport()
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           acknowledgementSleep: { try await clock.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    try await client.send(HubCommand(id: "acknowledged", type: "say_text"))
    try await eventually { await clock.waits.count == 1 }
    await socket.text(#"{"version":1,"type":"command_result","id":"acknowledged","ok":true,"result":{"request_id":"r"}}"#)
    try await eventually { await client.pendingCommands.isEmpty }
    await clock.advance()
    try await Task.sleep(for: .milliseconds(5))
    #expect(await socket.sent.filter { $0.id == "acknowledged" }.count == 1)
    #expect(await client.unansweredCommands.isEmpty)
    #expect(await client.state == .ready)
    await client.stop()
}

@Test func unansweredForegroundPresenceExpiresAndReconnectsWithFreshIdentity() async throws {
    let transport = FakeTransport(autoPresence: false)
    let clock = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           sleep: { _ in }, lifecycleSleep: { try await clock.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await clock.waits.count == 1 }
    let oldPresence = try #require(await first.sent.first)
    await clock.advance()
    try await eventually { await transport.socket(1) != nil }
    #expect(await first.closed)
    let replacement = try #require(await transport.socket(1))
    await replacement.ready(through: 1)
    try await eventually { await replacement.sent.count == 1 }
    let newPresence = try #require(await replacement.sent.first)
    #expect(oldPresence.id != newPresence.id)
    await replacement.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(newPresence.id)\",\"ok\":true,\"result\":{\"can_show_text\":true}}")
    try await eventually { await client.state == .ready }
    #expect(await transport.cursors == [0, 1])
    await client.stop(); await clock.advance()
}

@Test func pythonSpeechFixturePairsExactPcmWithoutReplayAudio() async throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let root = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(root["exchanges"] as? [String: Any])
    let frames = try #require(exchanges["speech_live"] as? [[String: Any]])
    let replay = try #require(exchanges["speech_replay"] as? [[String: Any]])
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    let captured = CapturedUpdates()
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    for frame in frames {
        if let encoded = frame["binary_base64"] as? String {
            socket.input.yield(.binary(try #require(Data(base64Encoded: encoded))))
        } else {
            socket.input.yield(.text(try JSONSerialization.data(withJSONObject: frame)))
        }
    }
    try await eventually { await captured.clips.count == 2 }
    let expected = Data([1, 128, 1, 192, 0, 0, 255, 63, 255, 127, 255, 127])
    #expect(await captured.clips == [expected, expected])
    #expect(!replay.contains { ($0["type"] as? String)?.hasPrefix("speech_") == true })
    await client.stop(); reader.cancel()
}

@Test func pythonLiveModeFixtureStreamsItsCuesAndReplyAndKeepsVoiceStateOutOfHistory() async throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let root = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(root["exchanges"] as? [String: Any])
    let live = try #require(exchanges["live_mode"] as? [String: Any])
    let frames = try #require(live["downlink"] as? [[String: Any]])
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    let captured = CapturedUpdates()
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    for frame in frames {
        if let encoded = frame["binary_base64"] as? String {
            socket.input.yield(.binary(try #require(Data(base64Encoded: encoded))))
        } else {
            socket.input.yield(.text(try JSONSerialization.data(withJSONObject: frame)))
        }
    }
    try await eventually { await captured.events.contains { $0.type == "speech_end" && $0.string("cue") == "closing" } }
    #expect(await captured.clips.count == 3)
    #expect(await socket.closed == false)
    let states = await captured.events.filter { $0.type == "voice_state" }
    #expect(states.map { $0.string("state") } == ["idle", "listening", "thinking", "speaking", "follow_up", "idle"])
    #expect(states.allSatisfy { !$0.replayable })
    await client.stop(); reader.cancel()
}

@Test(arguments: [#""reply_id":"r","cue":"chime","#, ""])
func speechBeginNamesItsReplyOrItsCueAndNeverBoth(names: String) async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready(through: 0)
    try await eventually { await client.state == .ready }
    let cursor = await client.cursor
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s",\#(names)"sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventually { await socket.closed }
    #expect(await client.cursor == cursor)
    await client.stop()
}

@Test func speechDownlinkDoesNotInheritUploadDurationLimit() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    let captured = CapturedUpdates()
    let reader = Task { for await update in client.updates { await captured.record(update) } }
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    let pcm = Data(repeating: 0, count: 14_400_002)
    await socket.text(event(2, "speech_begin", #"{"speech_id":"long","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", "{\"speech_id\":\"long\",\"reply_id\":\"r\",\"index\":0,\"text\":\"Long sentence\",\"byte_length\":\(pcm.count)}"))
    socket.input.yield(.binary(pcm))
    try await eventually { await captured.clips.count == 1 }
    #expect(await captured.clips.first?.count == pcm.count)
    await client.stop(); reader.cancel()
}

@Test(arguments: ["speech_clip", "speech_end", "speech_stop"])
func speechFramesCannotChangeCanonicalReplyIdentity(_ kind: String) async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"original","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, kind, #"{"speech_id":"s","reply_id":"other","index":0,"byte_length":2,"last_clip_index":-1,"reason":"interrupted"}"#))
    try await eventually { await socket.closed }
    #expect(await client.cursor == 2)
    await client.stop()
}

@Test func storedSilenceIsAcknowledgedBeforePresenceAndReadyInput() async throws {
    let socket = FakeSocket()
    let client = HubClient(transport: HubTransport(connect: { _ in socket }), desiredSilence: true)
    await client.start()
    await socket.ready()
    try await eventually { await socket.sent.count == 1 }
    let off = try #require(await socket.sent.first)
    #expect(off.type == "set_silence")
    #expect(off.payload == ["silence": .bool(true)])
    #expect(await client.state.canSend == false)
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(off.id)\",\"ok\":true,\"result\":{\"silence\":true}}")
    try await eventually { await client.state == .ready }
    #expect(await socket.sent.map(\.type) == ["set_silence", "set_presence"])
    await client.stop()
}

func acknowledgeSilence(_ socket: FakeSocket, _ command: HubCommand, value: Bool) async {
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(command.id)\",\"ok\":true,\"result\":{\"silence\":\(value)}}")
}

@Test func latestSilenceChoiceSupersedesRetainedCommandAndLateAcknowledgement() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await client.state == .ready }
    await client.setSilence(false)
    let old = try #require(await first.sent.last)
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await transport.socket(1) != nil }
    await client.setSilence(true)
    let second = try #require(await transport.socket(1))
    await second.ready(through: 3)
    try await eventually { await second.sent.count == 1 }
    let latest = try #require(await second.sent.first)
    #expect(latest.type == "set_silence" && latest.payload["silence"] == .bool(true))
    #expect(latest.id != old.id)
    await acknowledgeSilence(second, old, value: false)
    try await Task.sleep(for: .milliseconds(10))
    #expect(await second.sent.count == 1)
    await client.setSilence(false)
    let newest = try #require(await second.sent.last)
    await acknowledgeSilence(second, latest, value: true)
    #expect(await client.state.canSend == false)
    await acknowledgeSilence(second, newest, value: false)
    try await eventually { await client.state == .ready }
    #expect(await second.sent.map(\.type) == ["set_silence", "set_silence", "set_presence"])
    await client.stop()
}

@Test func unchangedUncertainSilenceRetainsOriginalIdentityBeforeReconnectPresence() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), desiredSilence: true, sleep: { _ in })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await first.sent.count == 1 }
    let original = try #require(await first.sent.first)
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 4)
    try await eventually { await second.sent.count == 1 }
    #expect(await second.sent.first == original)
    await acknowledgeSilence(second, original, value: true)
    try await eventually { await client.state == .ready }
    #expect(await second.sent.map(\.type) == ["set_silence", "set_presence"])
    await client.stop()
}

@Test func noLocalChoicePreservesHubSilenceAndOfflineChoiceWaitsForForeground() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.text(event(1, "status", #"{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":null,"mac_voice_on":true,"silence":true}"#))
    await first.text(#"{"version":1,"type":"replay_complete","through_seq":1}"#)
    try await eventually { await client.state == .ready }
    #expect(await first.sent.map(\.type) == ["set_presence"])
    await client.setForeground(false)
    await client.setSilence(true)
    #expect(await first.sent.map(\.type) == ["set_presence"])
    await client.setForeground(true)
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 3)
    try await eventually { await second.sent.count == 1 }
    let off = try #require(await second.sent.first)
    await acknowledgeSilence(second, off, value: true)
    try await eventually { await client.state == .ready }
    #expect(await second.sent.map(\.type) == ["set_silence", "set_presence"])
    await client.stop()
}
