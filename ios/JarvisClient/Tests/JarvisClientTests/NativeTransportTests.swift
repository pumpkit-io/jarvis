import Foundation
import Network
import Testing
@testable import JarvisClient

func largeRecordedExchange() throws -> [String: Any] {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let fixture = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(fixture["exchanges"] as? [String: Any])
    return try #require(exchanges["large_event_replay"] as? [String: Any])
}

private final class RecordedEventWebSocket: @unchecked Sendable {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "jarvis.tests.recorded-websocket")
    private let lock = NSLock()
    private var connections: [NWConnection] = []

    init(message: Data) throws {
        let webSocket = NWProtocolWebSocket.Options()
        webSocket.setClientRequestHandler(queue) { protocols, _ in
            NWProtocolWebSocket.Response(status: protocols.contains("jarvis.v1") ? .accept : .reject,
                                         subprotocol: "jarvis.v1")
        }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        parameters.defaultProtocolStack.applicationProtocols.insert(webSocket, at: 0)
        listener = try NWListener(using: parameters)
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { connection.cancel(); return }
            self.lock.withLock { self.connections.append(connection) }
            connection.stateUpdateHandler = { state in
                guard case .ready = state else { return }
                connection.stateUpdateHandler = nil
                let context = NWConnection.ContentContext(identifier: "recorded-event",
                    metadata: [NWProtocolWebSocket.Metadata(opcode: .text)])
                connection.send(content: message, contentContext: context, isComplete: true,
                                completion: .contentProcessed { _ in })
            }
            connection.start(queue: self.queue)
        }
    }

    func start() async throws -> URL {
        let deadline = Task { [listener] in
            try? await Task.sleep(for: .seconds(5))
            if !Task.isCancelled { listener.cancel() }
        }
        defer { deadline.cancel() }
        try await withCheckedThrowingContinuation { (ready: CheckedContinuation<Void, any Error>) in
            listener.stateUpdateHandler = { [listener] state in
                switch state {
                case .ready: listener.stateUpdateHandler = nil; ready.resume()
                case .failed(let error): listener.stateUpdateHandler = nil; ready.resume(throwing: error)
                case .cancelled: listener.stateUpdateHandler = nil; ready.resume(throwing: URLError(.timedOut))
                default: break
                }
            }
            listener.start(queue: queue)
        }
        let port = try #require(listener.port)
        return try #require(URL(string: "ws://127.0.0.1:\(port.rawValue)/v1/events"))
    }

    func stop() {
        listener.cancel()
        let current = lock.withLock { let current = connections; connections.removeAll(); return current }
        for connection in current { connection.stateUpdateHandler = nil; connection.cancel() }
    }
}

@Test func nativeReceiveBoundaryPreservesLargeRecordedHubText() async throws {
    let recorded = try largeRecordedExchange()
    let frames = try #require(recorded["replay"] as? [[String: Any]])
    let large = try #require(frames.first(where: { $0["type"] as? String == "show" }))
    let event = try JSONSerialization.data(withJSONObject: large)
    // Legal JSON whitespace exceeds the native receive ceiling without copying or changing the shared fixture.
    let data = event + Data(repeating: 0x20, count: 8 * 1_024 * 1_024)
    let server = try RecordedEventWebSocket(message: data)
    defer { server.stop() }
    var request = URLRequest(url: try await server.start())
    request.setValue("jarvis.v1", forHTTPHeaderField: "Sec-WebSocket-Protocol")
    let socket = URLSessionHubSocket(request: request)
    do {
        let actual = try await withThrowingTaskGroup(of: Data.self) { group in
            group.addTask {
                try await socket.open()
                guard case .text(let received) = try await socket.receive() else { throw HubProtocolError.invalidFrame }
                return received
            }
            group.addTask {
                try await Task.sleep(for: .seconds(10))
                await socket.close()
                throw URLError(.timedOut)
            }
            defer { group.cancelAll() }
            return try #require(await group.next())
        }
        #expect(actual == data)
        #expect(try HubFrame.decode(actual) == HubFrame.decode(event))
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
    func sendBinary(_ data: Data) async throws { try await wire.sendBinary(data) }
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
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in },
                           cache: { await cache.commit($0, through: $1) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let original = try #require(await transport.socket(0))
    for frame in live {
        if let seq = frame["seq"] as? Int64, seq > after { break }
        original.wire.input.yield(.text(try JSONSerialization.data(withJSONObject: frame)))
    }
    try await eventually { await client.cursor == after }
    original.wire.input.finish(throwing: URLError(.networkConnectionLost))
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
