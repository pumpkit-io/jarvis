import Foundation

/// Hands Outbox uploads to the system, which waits for the network and finishes them even while the app is suspended.
@MainActor public final class BackgroundOutboxTransport: NSObject, OutboxTransport {
    public static let identifier = "io.pumpkit.jarvis.outbox"
    public static let shared = BackgroundOutboxTransport()

    /// Set by the app delegate when iOS wakes the app for finished uploads; called once their results are handled.
    public var finishBackgroundEvents: (() -> Void)?
    private var handler: (@MainActor (String, OutboxResponse) -> Void)?
    private var unclaimed: [(String, OutboxResponse)] = []
    private var bodies: [Int: Data] = [:]
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.background(withIdentifier: Self.identifier)
        configuration.isDiscretionary = false
        configuration.sessionSendsLaunchEvents = true
        configuration.timeoutIntervalForRequest = 60
        configuration.httpMaximumConnectionsPerHost = 1
        return URLSession(configuration: configuration, delegate: Relay(owner: self), delegateQueue: .main)
    }()

    /// Recreates the session at launch so uploads finished while the app was gone report back.
    public func activate() { _ = session }

    public func onResponse(_ handler: @escaping @MainActor (String, OutboxResponse) -> Void) {
        self.handler = handler
        let pending = unclaimed; unclaimed = []
        for (id, response) in pending { handler(id, response) }
    }

    public func start(_ delivery: OutboxDelivery) {
        let task = session.uploadTask(with: delivery.request, fromFile: delivery.body)
        task.taskDescription = delivery.itemID
        task.earliestBeginDate = delivery.notBefore
        task.resume()
    }

    public func inFlight() async -> Set<String> {
        Set(await session.allTasks.filter { $0.state == .running || $0.state == .suspended }.compactMap(\.taskDescription))
    }

    public func cancelAll() {
        session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
    }

    fileprivate func received(_ data: Data, task: Int) { bodies[task, default: Data()].append(data) }

    fileprivate func completed(_ task: URLSessionTask, error: (any Error)?) {
        let body = bodies.removeValue(forKey: task.taskIdentifier) ?? Data()
        guard let id = task.taskDescription else { return }
        if let error = error as? URLError, error.code == .cancelled { return }
        let response: OutboxResponse
        if error == nil, let http = task.response as? HTTPURLResponse {
            response = .http(status: http.statusCode, body: body,
                              retryAfter: http.value(forHTTPHeaderField: "Retry-After").flatMap(Double.init))
        } else { response = .unreachable }
        if let handler { handler(id, response) } else { unclaimed.append((id, response)) }
    }

    fileprivate func finishedEvents() {
        let finish = finishBackgroundEvents; finishBackgroundEvents = nil
        finish?()
    }
}

/// URLSession keeps its delegate strongly, so the relay holds the transport weakly and hops onto the main actor.
private final class Relay: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    weak var owner: BackgroundOutboxTransport?
    init(owner: BackgroundOutboxTransport) { self.owner = owner }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        let id = dataTask.taskIdentifier
        MainActor.assumeIsolated { owner?.received(data, task: id) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        MainActor.assumeIsolated { owner?.completed(task, error: error) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        MainActor.assumeIsolated { owner?.finishedEvents() }
    }
}
