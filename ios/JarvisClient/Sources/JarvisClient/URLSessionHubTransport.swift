import Foundation
import Network

public extension HubTransport {
    /// Supply a token from the pairing store. Recreate the transport when pairing changes.
    static func urlSession(origin: URL, token: String) throws -> HubTransport {
        guard var components = URLComponents(url: origin, resolvingAgainstBaseURL: false),
              components.scheme == "https", let host = components.host, !host.isEmpty, components.user == nil,
              components.password == nil, components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/", !token.isEmpty,
              !token.contains(where: { $0.isNewline }) else { throw HubProtocolError.invalidOrigin }
        components.path = ""
        guard let root = components.url else { throw HubProtocolError.invalidOrigin }
        return HubTransport(connect: { after in
            var socketURL = URLComponents(url: root, resolvingAgainstBaseURL: false)!
            socketURL.scheme = "wss"; socketURL.path = "/v1/events"
            socketURL.queryItems = [URLQueryItem(name: "after", value: String(after))]
            guard let url = socketURL.url else { throw HubProtocolError.invalidOrigin }
            var request = URLRequest(url: url)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("jarvis.v1", forHTTPHeaderField: "Sec-WebSocket-Protocol")
            let socket = URLSessionHubSocket(request: request)
            do { try await socket.open(); return socket }
            catch { await socket.close(); throw error }
        }, request: { path in
            guard path.hasPrefix("/v1/"), !path.contains("#"), let url = URL(string: path, relativeTo: root)?.absoluteURL,
                  url.host == root.host, url.scheme == root.scheme, url.port == root.port else { throw HubProtocolError.invalidOrigin }
            var request = URLRequest(url: url)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let delegate = RefuseRedirects()
            let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
            defer { session.invalidateAndCancel() }
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw HubProtocolError.invalidFrame }
            if http.statusCode == 401 { throw HubTransportError.unauthorized }
            guard (200..<300).contains(http.statusCode) else { throw HubTransportError.http(http.statusCode) }
            return data
        })
    }
}

private final class RefuseRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

private final class URLSessionHubSocket: NSObject, HubSocket, URLSessionWebSocketDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, any Error>?
    private var opened = false
    private var ended = false
    private var session: URLSession!
    private var task: URLSessionWebSocketTask!

    init(request: URLRequest) {
        super.init()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 15
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        task = session.webSocketTask(with: request)
        // A 300-second 24 kHz PCM clip can occupy 14.4 MB; text is bounded separately.
        task.maximumMessageSize = 14_400_000
    }

    func open() async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (waiter: CheckedContinuation<Void, any Error>) in
                let cancelled = lock.withLock { () -> Bool in
                    if ended { return true }
                    continuation = waiter; return false
                }
                if cancelled { waiter.resume(throwing: CancellationError()) }
                else { task.resume() }
            }
        } onCancel: { self.shutdown() }
    }

    func send(_ data: Data) async throws {
        guard lock.withLock({ opened && !ended }) else { throw HubProtocolError.notConnected }
        guard data.count <= 65_536, let text = String(data: data, encoding: .utf8) else { throw HubProtocolError.invalidFrame }
        try await task.send(.string(text))
    }

    func receive() async throws -> SocketMessage {
        do {
            switch try await task.receive() {
            case .string(let text):
                guard text.utf8.count <= 65_536 else { throw HubProtocolError.invalidFrame }
                return .text(Data(text.utf8))
            case .data(let data): return .binary(data)
            @unknown default: throw HubProtocolError.invalidFrame
            }
        } catch {
            if task.closeCode == .policyViolation { throw HubTransportError.unauthorized }
            throw error
        }
    }

    func close() async { shutdown() }
    func closeForProtocolError() async { shutdown(code: .protocolError) }

    private func shutdown(code: URLSessionWebSocketTask.CloseCode = .goingAway) {
        let waiter = lock.withLock { () -> CheckedContinuation<Void, any Error>? in
            ended = true; opened = false
            defer { continuation = nil }; return continuation
        }
        waiter?.resume(throwing: CancellationError())
        task.cancel(with: code, reason: nil)
        session.invalidateAndCancel()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        let waiter = lock.withLock { () -> CheckedContinuation<Void, any Error>? in
            guard !ended else { return nil }
            opened = `protocol` == "jarvis.v1"
            defer { continuation = nil }; return continuation
        }
        if `protocol` == "jarvis.v1" { waiter?.resume() }
        else { waiter?.resume(throwing: HubTransportError.incompatibleProtocol); shutdown() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        let waiter = lock.withLock { () -> CheckedContinuation<Void, any Error>? in
            ended = true; opened = false
            defer { continuation = nil }; return continuation
        }
        let status = (task.response as? HTTPURLResponse)?.statusCode
        if status == 401 { waiter?.resume(throwing: HubTransportError.unauthorized) }
        else if status == 426 { waiter?.resume(throwing: HubTransportError.incompatibleProtocol) }
        else if let status, status != 101 { waiter?.resume(throwing: HubTransportError.http(status)) }
        else { waiter?.resume(throwing: error ?? HubProtocolError.handshake) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {}
}

/// Keep this owner alive alongside the Client and cancel it when unpairing.
public final class HubNetworkPaths: Sendable {
    public let changes: AsyncStream<Void>
    private let monitor: NWPathMonitor
    private let continuation: AsyncStream<Void>.Continuation

    public init() {
        (changes, continuation) = AsyncStream.makeStream()
        monitor = NWPathMonitor()
        let output = continuation
        monitor.pathUpdateHandler = { _ in output.yield(()) }
        monitor.start(queue: DispatchQueue(label: "jarvis.network-path"))
    }

    public func cancel() { monitor.cancel(); continuation.finish() }
    deinit { monitor.cancel(); continuation.finish() }
}
