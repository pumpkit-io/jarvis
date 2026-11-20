import CryptoKit
import Foundation
import Observation

public struct OutboxItem: Codable, Identifiable, Equatable, Sendable {
    public enum Content: Codable, Equatable, Sendable {
        case talk(talkID: String, seconds: Double)
        case line(text: String)
    }
    public enum Status: Codable, Equatable, Sendable {
        case waiting, sending, sent(requestID: String), failed(String), uncertain
    }
    public let id: String
    public var content: Content
    public var commandID: String
    public let recordedAt: Date
    public var status: Status
    public var attempts = 0

    public var message: String {
        switch status {
        case .waiting: "Waiting to send"
        case .sending: "Sending…"
        case .sent: "Sent"
        case .failed(let message): message
        case .uncertain: "This may have reached Jarvis. Check the conversation before sending it again."
        }
    }
}

/// What came back for one delivery: an HTTP answer, or nothing at all.
public enum OutboxResponse: Sendable, Equatable {
    case http(status: Int, body: Data, retryAfter: Double?)
    case unreachable
}

public struct OutboxDelivery: Sendable {
    public let itemID: String
    public let request: URLRequest
    public let body: URL
    public let notBefore: Date?
}

@MainActor public protocol OutboxTransport: AnyObject {
    func onResponse(_ handler: @escaping @MainActor (String, OutboxResponse) -> Void)
    func start(_ delivery: OutboxDelivery)
    func inFlight() async -> Set<String>
    func cancelAll()
}

/// One directory per pairing: the queue's order and state, and each item's exact request body.
public struct OutboxStore: Sendable {
    private let directory: URL

    public static var applicationDirectory: URL {
        URL.applicationSupportDirectory.appendingPathComponent("Jarvis/Outbox", isDirectory: true)
    }

    public init(directory: URL, credentials: PairingCredentials) {
        let identity = credentials.origin.absoluteString + "\n" + credentials.clientID
        let key = SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        self.directory = directory.appendingPathComponent(key, isDirectory: true)
    }

    public func load() throws -> [OutboxItem] {
        let index = directory.appendingPathComponent("index.json")
        guard FileManager.default.fileExists(atPath: index.path) else { return [] }
        return try JSONDecoder().decode([OutboxItem].self, from: Data(contentsOf: index))
    }

    public func save(_ items: [OutboxItem]) throws {
        try write(JSONEncoder().encode(items), to: directory.appendingPathComponent("index.json"))
    }

    public func body(_ id: String) -> URL { directory.appendingPathComponent(id).appendingPathExtension("body") }

    public func writeBody(_ data: Data, for id: String) throws { try write(data, to: body(id)) }

    public func removeBody(_ id: String) { try? FileManager.default.removeItem(at: body(id)) }

    public func clear() throws {
        if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
    }

    private func write(_ data: Data, to file: URL) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var excluded = directory
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try excluded.setResourceValues(values)
        // The background upload daemon reads bodies while the phone is locked.
        #if os(iOS)
        try data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: file, options: .atomic)
        #endif
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }
}

/// Every Talk and typed line waits here until the Hub has answered it, sent one at a time in the order they were said.
@MainActor @Observable public final class Outbox {
    public private(set) var items: [OutboxItem] = []
    public private(set) var saveFailed = false
    private let store: OutboxStore
    private let transport: any OutboxTransport
    private let credentials: PairingCredentials
    private let now: () -> Date
    private let sleep: @Sendable (Double) async throws -> Void
    private var failures = 0
    private var retryAt: Date?
    private var retry: Task<Void, Never>?
    private var heardRequests: [String] = []
    private var retired = false

    /// A message rejected for a reason other than its content is sent again under new ids, but not forever.
    static let maxAttempts = 6

    public init(store: OutboxStore, transport: any OutboxTransport, credentials: PairingCredentials,
                now: @escaping () -> Date = { Date() },
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }) {
        self.store = store; self.transport = transport; self.credentials = credentials; self.now = now; self.sleep = sleep
        items = (try? store.load()) ?? []
        transport.onResponse { [weak self] id, response in self?.receive(id, response) }
    }

    /// Items marked as sending whose upload no longer exists were interrupted, so they go back in line.
    public func resume() async {
        let live = await transport.inFlight()
        guard !retired else { return }
        for index in items.indices where items[index].status == .sending && !live.contains(items[index].id) {
            items[index].status = .waiting
        }
        persist()
        deliverNext()
    }

    public func enqueueTalk(talkID: String, pcm: Data, recordedAt: Date) {
        let item = OutboxItem(id: UUID().uuidString, content: .talk(talkID: talkID, seconds: Double(pcm.count) / 32_000),
                              commandID: UUID().uuidString, recordedAt: recordedAt, status: .waiting)
        enqueue(item, body: pcm)
    }

    public func enqueueLine(_ text: String, recordedAt: Date) {
        let item = OutboxItem(id: UUID().uuidString, content: .line(text: text), commandID: UUID().uuidString,
                              recordedAt: recordedAt, status: .waiting)
        let body = try? JSONSerialization.data(withJSONObject: ["version": 1, "text": text, "recorded_at": stamp(recordedAt)],
                                               options: [.sortedKeys])
        enqueue(item, body: body ?? Data())
    }

    /// Removes a message that has not gone out, or whose outcome the User has settled.
    public func delete(_ id: String) {
        guard let index = items.firstIndex(where: { $0.id == id }), items[index].status != .sending else { return }
        store.removeBody(id)
        items.remove(at: index)
        persist()
    }

    public func retry(_ id: String) {
        guard let index = items.firstIndex(where: { $0.id == id }), case .failed = items[index].status else { return }
        renew(index)
        items[index].attempts = 0; items[index].status = .waiting
        persist()
        deliverNext()
    }

    /// The network or the Hub came back: skip the remaining wait.
    public func nudge() {
        failures = 0; retryAt = nil
        retry?.cancel(); retry = nil
        deliverNext()
    }

    /// Before suspension, hand the next message to the system so it goes out when the network returns.
    public func prepareForBackground() {
        retry?.cancel(); retry = nil
        deliverNext(notBefore: retryAt)
    }

    public func heard(requestID: String) {
        heardRequests.append(requestID)
        if heardRequests.count > 64 { heardRequests.removeFirst() }
        items.removeAll { $0.status == .sent(requestID: requestID) }
    }

    public func retire() {
        retired = true
        retry?.cancel(); retry = nil
        transport.cancelAll()
        items = []
        try? store.clear()
    }

    private func enqueue(_ item: OutboxItem, body: Data) {
        guard !retired else { return }
        do { try store.writeBody(body, for: item.id); saveFailed = false }
        catch { saveFailed = true }
        items.append(item)
        persist()
        deliverNext()
    }

    private func deliverNext(notBefore: Date? = nil) {
        guard !retired, !items.contains(where: { $0.status == .sending }),
              let index = items.firstIndex(where: { $0.status == .waiting }) else { return }
        if let retryAt, retryAt > now(), notBefore == nil {
            guard retry == nil else { return }
            let delay = retryAt.timeIntervalSince(now())
            retry = Task { [weak self, sleep] in
                do { try await sleep(delay) } catch { return }
                guard let self, !Task.isCancelled else { return }
                self.retry = nil; self.retryAt = nil
                self.deliverNext()
            }
            return
        }
        let item = items[index]
        items[index].status = .sending
        persist()
        transport.start(OutboxDelivery(itemID: item.id, request: request(item), body: store.body(item.id), notBefore: notBefore))
    }

    private func request(_ item: OutboxItem) -> URLRequest {
        var request: URLRequest
        switch item.content {
        case .talk(let talkID, _):
            request = URLRequest(url: credentials.origin.appendingPathComponent("v1/utterances").appendingPathComponent(talkID))
            request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            request.setValue("pcm_s16le_16000_mono", forHTTPHeaderField: "X-Jarvis-Audio-Format")
            request.setValue(stamp(item.recordedAt), forHTTPHeaderField: "X-Jarvis-Recorded-At")
        case .line:
            request = URLRequest(url: credentials.origin.appendingPathComponent("v1/lines"))
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        request.httpMethod = "POST"
        request.setValue("Bearer \(credentials.deviceToken)", forHTTPHeaderField: "Authorization")
        request.setValue(item.commandID, forHTTPHeaderField: "Idempotency-Key")
        return request
    }

    private func receive(_ id: String, _ response: OutboxResponse) {
        guard !retired, let index = items.firstIndex(where: { $0.id == id }), items[index].status == .sending else { return }
        switch response {
        case .unreachable:
            items[index].status = .waiting
            backOff()
        case .http(let status, let body, let retryAfter):
            guard let result = try? JSONDecoder().decode(CommandResult.self, from: body),
                  result.id == items[index].commandID else {
                // A proxy answered, not the Hub: the Mac or Jarvis is down.
                items[index].status = .waiting
                backOff()
                break
            }
            if status == 200, result.ok, case .string(let request) = result.result?["request_id"] {
                failures = 0
                store.removeBody(id)
                if heardRequests.contains(request) { items.remove(at: index) }
                else { items[index].status = .sent(requestID: request) }
            } else {
                settle(index, error: result.error, retryAfter: retryAfter)
            }
        }
        persist()
        deliverNext()
    }

    private func settle(_ index: Int, error: HubError?, retryAfter: Double?) {
        let code = error?.code ?? ""
        let isTalk = if case .talk = items[index].content { true } else { false }
        switch code {
        case "outcome_unknown":
            items[index].status = .uncertain
        case "empty_transcript":
            items[index].status = .failed("Jarvis heard no words in this recording.")
        case "audio_too_large", "unsupported_audio", "id_conflict", "unauthorized":
            items[index].status = .failed(error?.message ?? "Jarvis couldn't take this message.")
        case "invalid_payload" where !isTalk:
            items[index].status = .failed(error?.message ?? "Jarvis couldn't take this message.")
        default:
            // The Hub answered without admitting it, so the same words can go again under new ids.
            items[index].attempts += 1
            guard items[index].attempts < Self.maxAttempts else {
                items[index].status = .failed("Jarvis couldn't take this message. Try again.")
                return
            }
            renew(index)
            items[index].status = .waiting
            backOff(at: retryAfter)
        }
    }

    private func renew(_ index: Int) {
        items[index].commandID = UUID().uuidString
        if case .talk(_, let seconds) = items[index].content { items[index].content = .talk(talkID: UUID().uuidString, seconds: seconds) }
    }

    private func backOff(at seconds: Double? = nil) {
        let delay = seconds ?? min(30, pow(2, Double(min(failures, 5))))
        failures += 1
        retryAt = now().addingTimeInterval(delay)
    }

    private func persist() {
        do { try store.save(items.filter { if case .sent = $0.status { false } else { true } }); saveFailed = false }
        catch { saveFailed = true }
    }

    private func stamp(_ date: Date) -> String { ISO8601DateFormatter().string(from: date) }
}

/// Starts nothing and never answers: every message stays queued.
@MainActor public final class HeldOutboxTransport: OutboxTransport {
    public private(set) var started: [OutboxDelivery] = []
    public init() {}
    public func onResponse(_ handler: @escaping @MainActor (String, OutboxResponse) -> Void) {}
    public func start(_ delivery: OutboxDelivery) { started.append(delivery) }
    public func inFlight() async -> Set<String> { Set(started.map(\.itemID)) }
    public func cancelAll() { started = [] }
}
