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
    func sendBinary(_ data: Data) async throws { try await socket.sendBinary(data) }
    func close() async { await socket.close() }
}

@MainActor @Test func sessionRecoversAfterLostSocketWithoutLosingCanonicalScript() async throws {
    let transport = FakeTransport()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    }, makePaths: { nil })
    await session.launch()
    try await eventuallySession { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0)); await first.ready()
    try await eventuallySession { session.connection == .ready }
    let heard = event(2, "heard", #"{"request_id":"a","text":"On the Mac","spoken":true}"#)
    await first.text(heard)
    try await eventuallySession { session.conversation.rows.count == 1 }
    await session.client?.pathChanged()
    #expect(await transport.socket(1) == nil)
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventuallySession { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.text(heard); await second.ready(through: 2)
    try await eventuallySession { session.connection == .ready }
    #expect(session.conversation.rows.map(\.text) == ["On the Mac"])
    #expect(await transport.cursors == [0, 2])
    await session.unpair()
}

@Test func olderEventsArrivingTogetherBuildTheSameScriptAsOneByOne() throws {
    let live = try [conversationEvent(10, "heard", #"{"request_id":"now","text":"Now","spoken":false}"#),
                    conversationEvent(11, "reply_end", #"{"reply_id":"now-reply","request_id":"now","text":"Done","reason":"complete"}"#)]
    let older = try [conversationEvent(1, "heard", #"{"request_id":"then","text":"Then","spoken":false}"#),
                     conversationEvent(2, "reply_delta", #"{"reply_id":"then-reply","request_id":"then","text":"Par"}"#),
                     conversationEvent(3, "reply_end", #"{"reply_id":"then-reply","request_id":"then","text":"Partial done","reason":"complete"}"#),
                     conversationEvent(10, "heard", #"{"request_id":"now","text":"Now","spoken":false}"#)]
    var batched = ConversationProjection(), oneByOne = ConversationProjection()
    batched.ingest(contentsOf: live); oneByOne.ingest(contentsOf: live)
    batched.ingest(contentsOf: older.reversed())
    for event in older.reversed() { oneByOne.ingest(event) }
    #expect(batched.rows == oneByOne.rows)
    #expect(batched.events.map(\.seq) == [1, 2, 3, 10, 11])
    #expect(batched.rows.map(\.text) == ["Then", "Partial done", "Now", "Done"])
}
