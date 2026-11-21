import Foundation
import Testing
@testable import JarvisClient

@Test func durableHistoryRestartsWithOrderedTextAndLiveOnlyWatermark() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let now = Date(timeIntervalSince1970: 1_795_089_600)
    let store = HistoryStore(directory: directory, credentials: credentials, now: { now })
    let event = try JSONDecoder().decode(HubEvent.self, from: Data(#"{"seq":14,"time":"2026-11-19T12:00:00Z","type":"heard","payload":{"text":"Remember this","request_id":"request"}}"#.utf8))
    try await store.commit(event, through: 14)
    try await store.commit(nil, through: 20)
    try await store.merge([event])
    let restored = try await HistoryStore(directory: directory, credentials: credentials, now: { now }).load()
    #expect(restored.cursor == 20)
    #expect(restored.events == [event])
    try await store.clear()
    await #expect(throws: (any Error).self) { try await store.commit(event, through: 30) }
    #expect(try await HistoryStore(directory: directory, credentials: credentials, now: { now }).load().events.isEmpty)
}

@Test func historyPrunesAtThirtyDaysAndSeparatesHubIdentity() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let now = Date(timeIntervalSince1970: 1_800_000_000)
    let credentials = try testPairingCredentials()
    let store = HistoryStore(directory: directory, credentials: credentials, now: { now })
    let old = try JSONDecoder().decode(HubEvent.self, from: Data(#"{"seq":1,"time":"2020-01-01T12:00:00Z","type":"notice","payload":{"text":"Expired"}}"#.utf8))
    try await store.commit(old, through: 100)
    #expect(try await store.load().events.isEmpty)
    #expect(try await store.load().cursor == 100)
    try await store.flush()
    let other = try PairingCredentials(origin: URL(string: "https://another.example")!, clientID: credentials.clientID, deviceToken: "other-secret", macName: "Other")
    #expect(try await HistoryStore(directory: directory, credentials: other).load().cursor == 0)
    let bytes = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil).map { try String(contentsOf: $0, encoding: .utf8) }.joined()
    #expect(!bytes.contains("secret"))
}

@MainActor @Test func productionCompositionHydratesOfflineWithoutReadinessAndClearsOnUnpair() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let cache = HistoryStore(directory: directory, credentials: credentials, now: { Date(timeIntervalSince1970: 1000) })
    let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures")!)) as! [String: Any]
    let exchanges = fixture["exchanges"] as! [String: Any]
    let frames = exchanges["speech_replay"] as! [[String: Any]]
    for frame in frames {
        if case .event(let event) = try HubFrame.decode(JSONSerialization.data(withJSONObject: frame)) {
            try await cache.commit(event, through: event.seq)
        }
    }
    try await cache.commit(nil, through: 900)
    try await cache.flush()
    let store = MemoryPairingStore(); store.record = credentials
    let cursors = HistoryCursors()
    let session = AppSession(store: store, historyDirectory: directory, historyNow: { Date(timeIntervalSince1970: 1000) },
        makeTransport: { _ in HubTransport(connect: { cursor in
            await cursors.append(cursor)
            throw URLError(.cannotConnectToHost)
        }) }, makePaths: { nil })
    await session.launch()
    try await eventuallySession { await cursors.values.first == 900 }
    #expect(!session.conversation.rows.isEmpty)
    #expect(!session.connection.canSend)
    #expect(!session.canSubmit)
    #expect(session.playback.activeSpeechID == nil)
    await session.unpair()
    #expect(session.conversation.rows.isEmpty)
    #expect(try await HistoryStore(directory: directory, credentials: credentials).load().cursor == 0)
}

actor HistoryCursors {
    var values: [Int64] = []
    func append(_ value: Int64) { values.append(value) }
}

actor HeldHistoryPage {
    var waiting = false
    var calls = 0
    var continuation: CheckedContinuation<Data, Never>?
    func request() async -> Data {
        calls += 1; waiting = true
        return await withCheckedContinuation { continuation = $0 }
    }
    func finish() {
        continuation?.resume(returning: Data(#"{"version":1,"events":[{"version":1,"seq":2,"time":"2026-11-19T00:00:00Z","type":"notice","payload":{"text":"Older page"}}],"next_before":2,"has_more":false}"#.utf8))
        continuation = nil; waiting = false
    }
}

@MainActor @Test func olderPageIsSingleFlightAndCannotRecreateUnpairedHistory() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let socket = FakeSocket(), page = HeldHistoryPage()
    let session = AppSession(store: store, historyDirectory: directory,
        makeTransport: { _ in HubTransport(connect: { _ in socket }, request: { _ in await page.request() }) }, makePaths: { nil })
    await session.launch(); await socket.ready(through: 20)
    try await eventuallySession { session.connection.canSend }
    let pending = Task { await session.loadOlderHistory() }
    try await eventuallySession { await page.waiting }
    await session.loadOlderHistory()
    #expect(await page.calls == 1)
    await session.unpair()
    await page.finish(); await pending.value
    #expect(session.conversation.rows.isEmpty)
    #expect(try await HistoryStore(directory: directory, credentials: testPairingCredentials()).load().cursor == 0)
}

@MainActor @Test func olderPagesMergeWithLiveEventsWithoutRewindingCursor() async throws {
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let socket = FakeSocket(), page = HeldHistoryPage()
    let client = HubClient(transport: HubTransport(connect: { _ in socket }, request: { _ in await page.request() }))
    let session = AppSession(store: store, makeClient: { _ in client }, makePaths: { nil })
    await session.launch(); await socket.ready(through: 20)
    try await eventuallySession { session.connection.canSend }
    let pending = Task { await session.loadOlderHistory() }
    try await eventuallySession { await page.waiting }
    await socket.text(event(30, "notice", #"{"text":"Live event"}"#))
    try await eventuallySession { session.conversation.rows.last?.sequence == 30 }
    await page.finish(); await pending.value
    #expect(session.conversation.rows.map(\.sequence) == [2, 30])
    #expect(await client.cursor == 30)
    #expect(!session.hasOlderHistory)
    await session.loadOlderHistory()
    #expect(await page.calls == 1)
    await session.unpair()
}

@MainActor @Test func corruptHistoryRecoversWithoutRevokingPairing() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let cache = HistoryStore(directory: directory, credentials: credentials)
    try await cache.commit(nil, through: 42)
    try await cache.flush()
    let file = try #require(FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil).first)
    try Data("incomplete record".utf8).write(to: file)
    let store = MemoryPairingStore(); store.record = credentials
    let socket = FakeSocket()
    let session = AppSession(store: store, historyDirectory: directory,
        makeTransport: { _ in HubTransport(connect: { _ in socket }) }, makePaths: { nil })
    await session.launch()
    #expect(session.credentials == credentials)
    #expect(store.clears == 0)
    #expect(session.historyMessage?.contains("could not be read") == true)
    #expect(await session.client?.cursor == 0)
    await session.unpair()
}

private func historyEvent(_ seq: Int64, time: String = "2026-11-19T12:00:00.123456Z", text: String = "Line") throws -> HubEvent {
    try JSONDecoder().decode(HubEvent.self, from: Data(#"{"seq":\#(seq),"time":"\#(time)","type":"notice","payload":{"text":"\#(text)"}}"#.utf8))
}

@Test func batchedCommitsLeaveTheLastConsistentHistoryOnDiskUntilFlushed() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let now = Date(timeIntervalSince1970: 1_795_089_600)
    let store = HistoryStore(directory: directory, credentials: credentials, now: { now }, writeDelay: .seconds(60))
    let first = try historyEvent(1), second = try historyEvent(2)
    try await store.commit(first, through: 1)
    try await store.commit(nil, through: 5)
    let reader = { try await HistoryStore(directory: directory, credentials: credentials, now: { now }).load() }
    #expect(try await reader().cursor == 0)
    try await store.flush()
    try await store.commit(second, through: 6)
    let killed = try await reader()
    #expect(killed.events == [first] && killed.cursor == 5)
    #expect(try await store.load().events == [first, second])
}

@Test func batchedCommitsReachDiskAfterTheWriteDelay() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let now = Date(timeIntervalSince1970: 1_795_089_600)
    let store = HistoryStore(directory: directory, credentials: credentials, now: { now }, writeDelay: .milliseconds(20))
    try await store.commit(try historyEvent(3), through: 3)
    let deadline = Date().addingTimeInterval(5)
    while try await HistoryStore(directory: directory, credentials: credentials, now: { now }).load().cursor != 3 {
        #expect(Date() < deadline); if Date() >= deadline { return }
        try await Task.sleep(for: .milliseconds(10))
    }
}

@Test func aFailedBatchedWriteStopsTheNextCommit() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer {
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        try? FileManager.default.removeItem(at: directory)
    }
    let credentials = try testPairingCredentials()
    let now = Date(timeIntervalSince1970: 1_795_089_600)
    let store = HistoryStore(directory: directory, credentials: credentials, now: { now }, writeDelay: .seconds(60))
    try await store.commit(try historyEvent(1), through: 1)
    try await store.flush()
    try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: directory.path)
    try await store.commit(try historyEvent(2), through: 2)
    await #expect(throws: (any Error).self) { try await store.flush() }
}

@Test func aCatchUpOfThousandsOfEventsCommitsInUnderTwoSeconds() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let now = Date(timeIntervalSince1970: 1_795_089_600)
    let store = HistoryStore(directory: directory, credentials: credentials, now: { now })
    let text = String(repeating: "Task snapshot text. ", count: 90)
    try await store.merge(try (1...1500).map { try historyEvent($0, text: text) })
    let start = Date()
    for seq in Int64(1501)...3000 { try await store.commit(try historyEvent(seq, text: text), through: seq) }
    try await store.flush()
    #expect(Date().timeIntervalSince(start) < 2)
    #expect(try await HistoryStore(directory: directory, credentials: credentials, now: { now }).load().events.count == 3000)
}

@MainActor @Test func goingToTheBackgroundSavesTheBatchedHistory() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let store = MemoryPairingStore(); store.record = credentials
    let socket = FakeSocket()
    let session = AppSession(store: store, historyDirectory: directory, historyNow: { Date(timeIntervalSince1970: 1_795_089_600) },
        makeTransport: { _ in HubTransport(connect: { _ in socket }) }, makePaths: { nil })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(#"{"version":1,"seq":2,"time":"2026-11-19T12:00:00Z","type":"notice","payload":{"text":"Keep me"}}"#)
    try await eventuallySession { session.conversation.rows.contains { $0.text == "Keep me" } }
    await session.setForeground(false)
    let saved = try await HistoryStore(directory: directory, credentials: credentials, now: { Date(timeIntervalSince1970: 1_795_089_600) }).load()
    #expect(saved.events.contains { $0.seq == 2 })
    await session.unpair()
}
