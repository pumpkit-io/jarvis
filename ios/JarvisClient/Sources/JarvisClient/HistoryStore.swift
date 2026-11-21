import CryptoKit
import Foundation

public struct CachedHistory: Codable, Sendable {
    public var events: [HubEvent] = []
    public var cursor: Int64 = 0
}

/// Keeps the cached history in memory and writes it in batches: a catch-up of thousands of events
/// costs one file write instead of one per event. Each write holds events and cursor together,
/// so a store ended before its batch leaves the previous consistent history, which the Hub replays from.
public actor HistoryStore {
    private let file: URL
    private let now: @Sendable () -> Date
    private let writeDelay: Duration
    private var retired = false
    private var loaded = false
    private var history = CachedHistory()
    private var dates: [Date] = []
    private var sequences: Set<Int64> = []
    private var oldest: Date?
    private var unsaved = false
    private var pendingWrite: Task<Void, Never>?
    private var failedWrite: (any Error)?

    public static var applicationDirectory: URL {
        URL.applicationSupportDirectory.appendingPathComponent("Jarvis/History", isDirectory: true)
    }

    public init(directory: URL, credentials: PairingCredentials,
                now: @escaping @Sendable () -> Date = { Date() }, writeDelay: Duration = .milliseconds(500)) {
        let identity = credentials.origin.absoluteString + "\n" + credentials.clientID
        let key = SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        file = directory.appendingPathComponent(key).appendingPathExtension("json")
        self.now = now
        self.writeDelay = writeDelay
    }

    public func load() throws -> CachedHistory {
        try loadOnce()
        if prune() { try write() }
        return history
    }

    /// Returns once the history is in memory; it reaches disk with the next batch, or at `flush()`.
    public func commit(_ event: HubEvent?, through cursor: Int64) throws {
        try loadOnce()
        if let failedWrite { self.failedWrite = nil; throw failedWrite }
        if let event { add(event) }
        history.cursor = max(history.cursor, cursor)
        _ = prune()
        unsaved = true
        scheduleWrite()
    }

    public func merge(_ events: [HubEvent]) throws {
        try loadOnce()
        for event in events { add(event) }
        _ = prune()
        try write()
    }

    /// Writes any batched history now, as before the app is suspended.
    public func flush() throws {
        guard !retired else { throw CancellationError() }
        if let failedWrite { self.failedWrite = nil; throw failedWrite }
        if unsaved { try write() }
    }

    public func clear() throws {
        retired = true
        pendingWrite?.cancel(); pendingWrite = nil
        if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
    }

    private func loadOnce() throws {
        guard !retired else { throw CancellationError() }
        guard !loaded else { return }
        guard FileManager.default.fileExists(atPath: file.path) else { loaded = true; return }
        let saved = try JSONDecoder().decode(CachedHistory.self, from: Data(contentsOf: file))
        let times = saved.events.map { utcDate($0.time) }
        guard saved.cursor >= 0, saved.events.allSatisfy({ $0.replayable && $0.seq >= 0 }), !times.contains(nil) else {
            throw HubProtocolError.invalidFrame
        }
        history.cursor = saved.cursor
        for event in saved.events { add(event) }
        loaded = true
        if prune() || history.events != saved.events { try write() }
    }

    private func add(_ event: HubEvent) {
        guard event.replayable, let date = utcDate(event.time), sequences.insert(event.seq).inserted else { return }
        let index = history.events.last.map { $0.seq < event.seq } ?? true
            ? history.events.endIndex
            : history.events.firstIndex { $0.seq > event.seq } ?? history.events.endIndex
        history.events.insert(event, at: index)
        dates.insert(date, at: index)
        oldest = min(oldest ?? date, date)
    }

    /// Drops events older than thirty days; returns whether any were dropped.
    private func prune() -> Bool {
        let cutoff = now().addingTimeInterval(-30 * 24 * 3600)
        guard let oldest, oldest < cutoff else { return false }
        let kept = dates.indices.filter { dates[$0] >= cutoff }
        history.events = kept.map { history.events[$0] }
        dates = kept.map { dates[$0] }
        sequences = Set(history.events.map(\.seq))
        self.oldest = dates.min()
        return true
    }

    private func scheduleWrite() {
        guard pendingWrite == nil else { return }
        pendingWrite = Task { [weak self, writeDelay] in
            try? await Task.sleep(for: writeDelay)
            await self?.writeBatch()
        }
    }

    private func writeBatch() {
        pendingWrite = nil
        guard !retired, unsaved else { return }
        do { try write() } catch { failedWrite = error }
    }

    private func write() throws {
        guard !retired else { throw CancellationError() }
        pendingWrite?.cancel(); pendingWrite = nil
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(history).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var location = file; try location.setResourceValues(values)
        unsaved = false
    }
}
