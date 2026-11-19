import Foundation
import Testing
@testable import JarvisClient

func conversationEvent(_ seq: Int64, _ type: String, _ payload: String, client: String = "mac") throws -> HubEvent {
    let data = Data("{\"version\":1,\"seq\":\(seq),\"time\":\"2026-11-19T12:00:00Z\",\"type\":\"\(type)\",\"client_id\":\"\(client)\",\"payload\":\(payload)}".utf8)
    guard case .event(let event) = try HubFrame.decode(data) else { throw HubProtocolError.invalidFrame }
    return event
}

@Test func scriptGroupsStreamingDetailsAndAuthoritativeEndAcrossReplay() throws {
    var script = ConversationProjection()
    let events = try [
        conversationEvent(1, "heard", #"{"request_id":"a","text":"Hello","spoken":false}"#),
        conversationEvent(2, "show", #"{"reply_id":"r","title":"Plan","text":"safe **text**"}"#),
        conversationEvent(3, "reply_delta", #"{"reply_id":"r","request_id":"a","text":"Good "}"#),
        conversationEvent(4, "reply_delta", #"{"reply_id":"r","request_id":"a","text":"morning"}"#),
        conversationEvent(5, "reply_end", #"{"reply_id":"r","request_id":"a","text":"Good.","reason":"interrupted"}"#)
    ]
    for event in events.prefix(4) { script.ingest(event) }
    #expect(script.rows.map(\.text) == ["Hello", "Good morning"])
    #expect(script.rows[1].streaming)
    script.ingest(events[4])
    for event in events { script.ingest(event) }
    #expect(script.rows.map(\.text) == ["Hello", "Good."])
    #expect(script.rows[1].details.first?.text == "safe **text**")
    #expect(script.rows[1].requestID == "a")
    #expect(!script.rows[1].streaming)
    #expect(script.events.count == 5)
}

@MainActor @Test func sessionOwnsTypedAcknowledgementsAndRemoteConversation() async throws {
    let socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil })
    await session.launch()
    session.draft = "Hello"
    await session.sendTypedLine()
    #expect(session.typedLines.isEmpty)
    #expect(session.draft == "Hello")
    await socket.ready()
    try await eventuallySession { session.connection == .ready }
    await session.sendTypedLine()
    let line = try #require(session.typedLines.first)
    #expect(line.command.type == "say_text")
    #expect(line.command.payload == ["text": .string("Hello")])
    #expect(line.outcome == .unanswered)
    session.draft = "Hello"
    await session.sendTypedLine()
    #expect(session.typedLines.count == 1)
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(line.id)\",\"ok\":false,\"error\":{\"code\":\"outcome_unknown\",\"message\":\"Check history\",\"retry_after_seconds\":null}}")
    try await eventuallySession { session.typedLines.first?.outcome == .outcomeUnknown }
    await socket.text(event(2, "heard", #"{"request_id":"mac-r","text":"From Mac","spoken":true}"#))
    try await eventuallySession { session.conversation.rows.first?.text == "From Mac" }
    await session.unpair()
    #expect(session.conversation.rows.isEmpty)
    #expect(session.typedLines.isEmpty)
    #expect(session.draft.isEmpty)
}

@Test func pythonCanonicalConversationReplaysThroughOneProjection() throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let root = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(root["exchanges"] as? [String: Any])
    let streams = try #require(exchanges["typed_wire_streams"] as? [[[String: Any]]])
    var script = ConversationProjection()
    for stream in streams {
        for object in stream {
            if case .event(let event) = try HubFrame.decode(JSONSerialization.data(withJSONObject: object)) { script.ingest(event) }
        }
    }
    #expect(script.rows.map(\.text) == ["Hello", "One two."])
    #expect(script.rows.map(\.clientID) == ["client-1", "client-1"])
    let large = try #require(exchanges["large_event_replay"] as? [String: Any])
    var largeScript = ConversationProjection()
    let live = try #require(large["live"] as? [[String: Any]])
    for object in live {
        if case .event(let event) = try HubFrame.decode(JSONSerialization.data(withJSONObject: object)) { largeScript.ingest(event) }
    }
    #expect(largeScript.rows.first?.text == "Show the complete result")
    #expect(largeScript.rows.last?.details.first?.text.utf8.count == 70_000)
    let shown = try #require(live.first { $0["type"] as? String == "show" }?["payload"] as? [String: Any])
    #expect(largeScript.rows.last?.details.first?.text == shown["text"] as? String)
    #expect(largeScript.rows.last?.text.utf8.count == 70_000)
}

@Test func olderHistoryMergesWithBothClientsAndUnassociatedDetails() throws {
    var script = ConversationProjection()
    script.ingest(try conversationEvent(4, "show", #"{"reply_id":null,"title":"Report","text":"standalone"}"#))
    script.ingest(try conversationEvent(3, "heard", #"{"request_id":"phone","text":"Phone","spoken":false}"#, client: "iphone"))
    script.ingest(try conversationEvent(1, "heard", #"{"request_id":"terminal","text":"Mac","spoken":true}"#))
    script.ingest(try conversationEvent(2, "notice", #"{"text":"Waiting for you"}"#))
    #expect(script.rows.map(\.text) == ["Mac", "Waiting for you", "Phone", ""])
    #expect(script.rows.map(\.clientID) == ["mac", "mac", "iphone", "mac"])
    #expect(script.rows.last?.details.first?.text == "standalone")
}

actor UncertainTypedSocket: HubSocket {
    let socket = FakeSocket()
    func receive() async throws -> SocketMessage { try await socket.receive() }
    func send(_ data: Data) async throws {
        try await socket.send(data)
        if try JSONDecoder().decode(HubCommand.self, from: data).type == "say_text" { throw URLError(.networkConnectionLost) }
    }
    func close() async { await socket.close() }
}

@MainActor @Test func typedWriteFailurePreservesIdentityAndDraftWithoutDuplicateRetry() async throws {
    let socket = UncertainTypedSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil })
    await session.launch()
    await socket.socket.ready()
    try await eventuallySession { session.connection == .ready }
    session.draft = String(repeating: "é", count: 8193)
    #expect(!session.canSubmit)
    #expect(session.draftValidationMessage != nil)
    session.draft = "Maybe delivered"
    await session.sendTypedLine()
    #expect(session.draft == "Maybe delivered")
    #expect(session.typedLines.first?.outcome == .outcomeUnknown)
    #expect(!session.canSubmit)
    let line = try #require(session.typedLines.first)
    #expect(await session.client?.pendingCommands.contains(line.command) == true)
    await session.sendTypedLine()
    #expect(session.typedLines.count == 1)
    await session.unpair()
}

@MainActor @Test func sessionRecoversAfterPathChangeWithoutLosingCanonicalScript() async throws {
    let transport = FakeTransport()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { await transport.connect($0) }))
    }, makePaths: { nil })
    await session.launch()
    try await eventuallySession { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0)); await first.ready()
    try await eventuallySession { session.connection == .ready }
    let heard = event(2, "heard", #"{"request_id":"a","text":"On the Mac","spoken":true}"#)
    await first.text(heard)
    try await eventuallySession { session.conversation.rows.count == 1 }
    await session.client?.pathChanged()
    try await eventuallySession { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.text(heard); await second.ready(through: 2)
    try await eventuallySession { session.connection == .ready }
    #expect(session.conversation.rows.map(\.text) == ["On the Mac"])
    #expect(await transport.cursors == [0, 2])
    await session.unpair()
}
