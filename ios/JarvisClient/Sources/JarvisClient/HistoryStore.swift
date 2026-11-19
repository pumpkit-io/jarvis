import CryptoKit
import Foundation

public struct CachedHistory: Codable, Sendable {
    public var events: [HubEvent] = []
    public var cursor: Int64 = 0
}

public actor HistoryStore {
    private let file: URL
    private let now: @Sendable () -> Date
    private var retired = false

    public static var applicationDirectory: URL {
        URL.applicationSupportDirectory.appendingPathComponent("Jarvis/History", isDirectory: true)
    }

    public init(directory: URL, credentials: PairingCredentials,
                now: @escaping @Sendable () -> Date = { Date() }) {
        let identity = credentials.origin.absoluteString + "\n" + credentials.clientID
        let key = SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        file = directory.appendingPathComponent(key).appendingPathExtension("json")
        self.now = now
    }

    public func load() throws -> CachedHistory {
        guard !retired else { throw CancellationError() }
        guard FileManager.default.fileExists(atPath: file.path) else { return CachedHistory() }
        let saved = try JSONDecoder().decode(CachedHistory.self, from: Data(contentsOf: file))
        guard saved.cursor >= 0, saved.events.allSatisfy({ $0.replayable && $0.seq >= 0 && utcDate($0.time) != nil }) else {
            throw HubProtocolError.invalidFrame
        }
        let pruned = bounded(saved)
        if pruned.events != saved.events { try write(pruned) }
        return pruned
    }

    public func commit(_ event: HubEvent?, through cursor: Int64) throws {
        var saved = try load()
        if let event, event.replayable { saved.events.append(event) }
        saved.cursor = max(saved.cursor, cursor)
        try write(bounded(saved))
    }

    public func merge(_ events: [HubEvent]) throws {
        var saved = try load()
        saved.events += events.filter(\.replayable)
        try write(bounded(saved))
    }

    public func clear() throws {
        retired = true
        if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
    }

    private func bounded(_ history: CachedHistory) -> CachedHistory {
        let cutoff = now().addingTimeInterval(-30 * 24 * 3600)
        var seen = Set<Int64>()
        return CachedHistory(events: history.events.filter {
            $0.replayable && (utcDate($0.time).map { $0 >= cutoff } ?? false) && seen.insert($0.seq).inserted
        }.sorted { $0.seq < $1.seq }, cursor: history.cursor)
    }

    private func write(_ history: CachedHistory) throws {
        guard !retired else { throw CancellationError() }
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(history).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var location = file; try location.setResourceValues(values)
    }
}
