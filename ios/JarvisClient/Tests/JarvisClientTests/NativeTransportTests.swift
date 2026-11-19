import Foundation
import Testing
@testable import JarvisClient

func largeRecordedExchange() throws -> [String: Any] {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let fixture = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(fixture["exchanges"] as? [String: Any])
    return try #require(exchanges["large_event_replay"] as? [String: Any])
}

@Test func nativeReceiveBoundaryPreservesLargeRecordedHubText() async throws {
    let recorded = try largeRecordedExchange()
    let frames = try #require(recorded["replay"] as? [[String: Any]])
    let large = try #require(frames.first(where: { $0["type"] as? String == "show" }))
    let data = try JSONSerialization.data(withJSONObject: large)
    #expect(data.count > 65_536)
    let text = try #require(String(data: data, encoding: .utf8))
    let socket = URLSessionHubSocket(request: URLRequest(url: URL(string: "wss://example.invalid/v1/events")!),
                                     receiveMessage: { .string(text) })
    #expect(socket.task.maximumMessageSize == Int.max)
    do {
        guard case .text(let actual) = try await socket.receive() else { Issue.record("Expected text"); await socket.close(); return }
        #expect(actual == data)
    } catch {
        await socket.close()
        throw error
    }
    await socket.close()
}

struct NativeReceiveSocket: HubSocket {
    let wire: FakeSocket
    let native: URLSessionHubSocket

    init() {
        let wire = FakeSocket()
        self.wire = wire
        native = URLSessionHubSocket(request: URLRequest(url: URL(string: "wss://example.invalid/v1/events")!), receiveMessage: {
            switch try await wire.receive() {
            case .text(let data): return .string(try #require(String(data: data, encoding: .utf8)))
            case .binary(let data): return .data(data)
            }
        })
    }

    func receive() async throws -> SocketMessage { try await native.receive() }
    func send(_ data: Data) async throws { try await wire.send(data) }
    func close() async { await wire.close(); await native.close() }
}

actor NativeReplayTransport {
    var sockets: [NativeReceiveSocket] = []
    var cursors: [Int64] = []
    func connect(_ after: Int64) -> any HubSocket {
        let socket = NativeReceiveSocket()
        sockets.append(socket); cursors.append(after)
        return socket
    }
    func socket(_ index: Int) -> NativeReceiveSocket? { sockets.indices.contains(index) ? sockets[index] : nil }
}

actor RecordedCache {
    var events: [Int64: HubEvent] = [:]
    var cursor: Int64 = 0
    func commit(_ event: HubEvent?, through: Int64) {
        if let event { events[event.seq] = event }
        cursor = max(cursor, through)
    }
}

@Test func recordedLargeEventsCacheAfterNativeReplayAndResumeFromCommittedCursor() async throws {
    let recorded = try largeRecordedExchange()
    let after = try #require(recorded["after"] as? Int64)
    let live = try #require(recorded["live"] as? [[String: Any]])
    let replay = try #require(recorded["replay"] as? [[String: Any]])
    let transport = NativeReplayTransport()
    let cache = RecordedCache()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           cache: { await cache.commit($0, through: $1) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let original = try #require(await transport.socket(0))
    for frame in live {
        if let seq = frame["seq"] as? Int64, seq > after { break }
        original.wire.input.yield(.text(try JSONSerialization.data(withJSONObject: frame)))
    }
    try await eventually { await client.cursor == after }
    await client.pathChanged()
    try await eventually { await transport.socket(1) != nil }
    let replacement = try #require(await transport.socket(1))
    for frame in replay { replacement.wire.input.yield(.text(try JSONSerialization.data(withJSONObject: frame))) }
    let last = try #require(replay.compactMap { $0["seq"] as? Int64 }.max())
    try await eventually { await client.cursor == last }
    try await eventually { await client.state == .ready }
    #expect(await transport.cursors == [0, after])
    #expect(await cache.cursor == last)
    for frame in replay where ["show", "reply_delta", "reply_end"].contains(frame["type"] as? String) {
        guard case .event(let expected) = try HubFrame.decode(JSONSerialization.data(withJSONObject: frame)) else {
            Issue.record("Expected recorded event"); continue
        }
        #expect(await cache.events[expected.seq] == expected)
        #expect(expected.string("text")?.utf8.count == 70_000)
    }
    #expect(await replacement.wire.closed == false)
    await client.stop()
}
