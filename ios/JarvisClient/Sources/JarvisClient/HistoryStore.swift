import CryptoKit
import Foundation

public struct CachedHistory: Codable, Sendable {
    public var events: [HubEvent] = []
    public var cursor: Int64 = 0
}

/// Keeps the history in memory and writes it in batches, events and cursor together, so a store
/// ended before its batch leaves the previous consistent file and the Hub replays the rest.
public actor HistoryStore {
    private struct Entry {
        let event: HubEvent
        let date: Date
    }

    private let file: URL
    private let now: @Sendable () -> Date
    private let writeDelay: Duration
    private var retired = false
    private var loaded = false
    private var entries: [Entry] = []
    private var cursor: Int64 = 0
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
        if prune() { try save() }
        return snapshot
    }

    /// Returns once the history is in memory; it reaches disk with the next batch or `flush()`.
    /// While a write is failing, each commit retries it and throws instead of accepting more.
    public func commit(_ event: HubEvent?, through cursor: Int64) throws {
        try loadOnce()
        if failedWrite != nil { try save() }
        if let event { add(event) }
        self.cursor = max(self.cursor, cursor)
        _ = prune()
        unsaved = true
        scheduleWrite()
    }

    public func merge(_ events: [HubEvent]) throws {
        try loadOnce()
        for event in events { add(event) }
        _ = prune()
        try save()
    }

    /// Writes any batched history now, as before the app is suspended.
    public func flush() throws {
        guard !retired else { throw CancellationError() }
        if unsaved || failedWrite != nil { try save() }
    }

    public func clear() throws {
        retired = true
        pendingWrite?.cancel(); pendingWrite = nil
        if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
    }

    private var snapshot: CachedHistory { CachedHistory(events: entries.map(\.event), cursor: cursor) }

    private func loadOnce() throws {
        guard !retired else { throw CancellationError() }
        guard !loaded else { return }
        guard FileManager.default.fileExists(atPath: file.path) else { loaded = true; return }
        let saved = try JSONDecoder().decode(CachedHistory.self, from: Data(contentsOf: file))
        guard saved.cursor >= 0, saved.events.allSatisfy({ $0.replayable && $0.seq >= 0 && utcDate($0.time) != nil }) else {
            throw HubProtocolError.invalidFrame
        }
        cursor = saved.cursor
        for event in saved.events { add(event) }
        loaded = true
        if prune() || entries.map(\.event) != saved.events { try save() }
    }

    private func add(_ event: HubEvent) {
        guard event.replayable, let date = utcDate(event.time), sequences.insert(event.seq).inserted else { return }
        let entry = Entry(event: event, date: date)
        if let last = entries.last, last.event.seq > event.seq {
            entries.insert(entry, at: entries.firstIndex { $0.event.seq > event.seq } ?? entries.endIndex)
        } else {
            entries.append(entry)
        }
        oldest = min(oldest ?? date, date)
    }

    /// Drops events older than thirty days, scanning only when the oldest one has expired.
    private func prune() -> Bool {
        let cutoff = now().addingTimeInterval(-30 * 24 * 3600)
        guard let oldest, oldest < cutoff else { return false }
        entries.removeAll { $0.date < cutoff }
        sequences = Set(entries.map(\.event.seq))
        self.oldest = entries.map(\.date).min()
        return true
    }

    private func scheduleWrite() {
        guard pendingWrite == nil else { return }
        pendingWrite = Task { [weak self, writeDelay] in
            do { try await Task.sleep(for: writeDelay) } catch { return }
            await self?.writeBatch()
        }
    }

    private func writeBatch() {
        guard !Task.isCancelled else { return }
        pendingWrite = nil
        guard !retired, unsaved else { return }
        try? save()
    }

    private func save() throws {
        do { try write(); failedWrite = nil } catch { failedWrite = error; throw error }
    }

    private func write() throws {
        guard !retired else { throw CancellationError() }
        pendingWrite?.cancel(); pendingWrite = nil
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(snapshot).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var location = file; try location.setResourceValues(values)
        unsaved = false
    }
}
