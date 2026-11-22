import Foundation
import Testing
@testable import JarvisClient

@MainActor final class MemoryPairingStore: PairingStore {
    var record: PairingCredentials?
    var saves = 0
    var clears = 0
    func load() throws -> PairingCredentials? { record }
    func save(_ credentials: PairingCredentials) throws { record = credentials; saves += 1 }
    func clear() throws { record = nil; clears += 1 }
}

func testPairingCredentials() throws -> PairingCredentials {
    try PairingCredentials(origin: URL(string: "https://mac.example")!, clientID: "iphone-1", deviceToken: "secret", macName: "Mac")
}

@MainActor @Test func launchSkipsPairingAndRevocationClearsCredentialRecord() async throws {
    let store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { _ in throw HubTransportError.unauthorized }))
    }, makePaths: { nil })
    await session.launch()
    #expect(session.credentials != nil)
    try await eventuallySession { session.credentials == nil }
    #expect(store.record == nil)
    #expect(store.clears == 1)
    #expect(session.connection == .unpaired)
}

actor HeldPairing {
    var waiter: CheckedContinuation<(Data, Int), any Error>?
    func post() async throws -> (Data, Int) { try await withCheckedThrowingContinuation { waiter = $0 } }
    func finish() {
        waiter?.resume(returning: (Data(#"{"version":1,"client_id":"iphone-1","device_token":"secret","mac_name":"Mac","origin":"https://mac.example"}"#.utf8), 201))
        waiter = nil
    }
    var waiting: Bool { waiter != nil }
}

@MainActor @Test func stalePairingCannotRestoreUnpairedSecrets() async throws {
    let store = MemoryPairingStore()
    let held = HeldPairing()
    let session = AppSession(store: store, service: PairingService { _ in try await held.post() }, makePaths: { nil })
    await session.launch()
    let task = Task { await session.pair(try! PairingInput(origin: "https://mac.example", code: "001234")) }
    try await eventuallySession { await held.waiting }
    await session.unpair()
    await held.finish()
    await task.value
    #expect(store.saves == 0)
    #expect(store.record == nil)
    #expect(session.credentials == nil)
}

@MainActor @Test func keychainBoundaryStoresOneCoherentValidatedRecord() throws {
    var bytes: Data?
    let access = KeychainRecordAccess(read: { bytes }, write: { bytes = $0 }, remove: { bytes = nil })
    let store = KeychainPairingStore(access: access)
    let credentials = try testPairingCredentials()
    try store.save(credentials)
    #expect(try store.load() == credentials)
    let data = try #require(bytes)
    let record = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    #expect(record["deviceToken"] as? String == "secret")
    #expect(record["clientID"] as? String == "iphone-1")
    try store.clear()
    #expect(try store.load() == nil)
    bytes = Data(#"{"origin":"http://evil.example","deviceToken":"secret","clientID":"a","macName":"Mac"}"#.utf8)
    #expect(throws: (any Error).self) { try store.load() }
}

@MainActor @Test func cameraRefusalKeepsManualPairingAvailableWithoutRepeatedPrompts() async {
    let camera = PairingCamera(request: { false })
    #expect(await camera.authorize() == false)
    #expect(camera.permission == .refused)
    camera.refresh(.allowed)
    #expect(await camera.authorize())
}

@MainActor func eventuallySession(_ condition: () async -> Bool) async throws {
    for _ in 0..<1000 {
        if await condition() { return }
        try await Task.sleep(for: .milliseconds(1))
    }
    Issue.record("Session did not reach the expected state")
}

@MainActor @Test func successfulPairingStoresCredentialsAndStartsOneClient() async throws {
    let store = MemoryPairingStore()
    let socket = FakeSocket()
    var starts = 0
    let service = PairingService { request in
        let data = try #require(request.httpBody)
        let body = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(body["version"] as? Int == 1)
        #expect(body["code"] as? String == "001234")
        #expect(body["name"] as? String == "iPhone")
        #expect(body["kind"] as? String == "iphone")
        return (Data(#"{"version":1,"client_id":"iphone-1","device_token":"secret","mac_name":"Mac","origin":"https://mac.example"}"#.utf8), 201)
    }
    let session = AppSession(store: store, service: service, makeClient: { credentials in
        #expect(credentials.deviceToken == "secret")
        starts += 1
        return HubClient(transport: HubTransport(connect: { _ in socket }))
    }, makePaths: { nil })
    await session.launch()
    #expect(session.credentials == nil)
    await session.pair(try PairingInput(origin: "https://mac.example", code: "001234"))
    #expect(store.saves == 1)
    #expect(session.credentials == store.record)
    #expect(session.connection == .connecting)
    await session.launch()
    #expect(starts == 1)
    await session.setForeground(false)
    #expect(await session.client?.state == .suspended)
    await session.unpair()
    #expect(session.client == nil)
    #expect(store.record == nil)
}

@MainActor @Test func offlineVoiceChoiceSurvivesFactoryRestartAndWaitsBeforeHeldNewsPresence() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let first = AppSession(store: store, historyDirectory: directory,
                           makeTransport: { _ in HubTransport(connect: { _ in throw URLError(.cannotConnectToHost) }) }, makePaths: { nil })
    await first.launch()
    await first.setVoiceOn(false)
    #expect(!first.voiceOn && first.voicePending)
    await first.client?.stop()
    let socket = FakeSocket()
    let second = AppSession(store: store, historyDirectory: directory,
                            makeTransport: { _ in HubTransport(connect: { _ in socket }) }, makePaths: { nil })
    await second.launch()
    #expect(!second.voiceOn)
    await socket.ready()
    try await eventuallySession { await socket.sent.count == 1 }
    let off = try #require(await socket.sent.first)
    #expect(off.type == "set_silence" && off.payload["silence"] == .bool(true))
    #expect(!second.connection.canSend)
    await acknowledgeSilence(socket, off, value: true)
    try await eventuallySession { second.connection.canSend && !second.voicePending }
    #expect(!second.voiceOn)
    #expect(await socket.sent.map(\.type) == ["set_silence", "set_presence"])
    await socket.text(event(2, "reply_end", #"{"reply_id":"held-news","text":"Task complete.","reason":"complete"}"#))
    try await eventuallySession { second.conversation.rows.contains { $0.text == "Task complete." } }
    #expect(second.playback.playingSpeechID == nil)
    await second.unpair()
    #expect(try VoiceSettingStore(directory: directory.appendingPathComponent("Voice")).load(testPairingCredentials()) == nil)
}

@Test func voiceStoreSeparatesPairingsAndNeverStoresCredentials() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = VoiceSettingStore(directory: directory)
    let first = try testPairingCredentials()
    let other = try PairingCredentials(origin: first.origin, clientID: "another-phone", deviceToken: "other-secret", macName: "Mac")
    #expect(try store.load(first) == nil)
    try store.save(true, for: first)
    #expect(try store.load(first) == true)
    #expect(try store.load(other) == nil)
    let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
    #expect(files.count == 1)
    #expect(try Data(contentsOf: files[0]) == Data("true".utf8))
    try store.clear(first)
    #expect(try store.load(first) == nil)
}

@MainActor @Test func voiceChangeLeavesLiveTalkRecordingAndReadinessAlone() async throws {
    let recorder = FakeTalkRecorder(), socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let talk = TalkController(recorder: recorder, permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await talk.press()
    #expect(recorder.active)
    await session.setVoiceOn(false)
    let off = try #require(await socket.sent.last { $0.type == "set_silence" })
    try await Task.sleep(for: .milliseconds(10))
    #expect(recorder.active && talk.state == .recording)
    #expect(session.connection.canSend)
    await acknowledgeSilence(socket, off, value: true)
    try await eventuallySession { !session.voicePending }
    #expect(recorder.active && talk.state == .recording)
    await session.unpair()
}

@MainActor @Test func failedVoicePersistenceStaysVisibleAfterHubAcknowledgement() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let socket = FakeSocket(), store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             voiceStore: VoiceSettingStore(directory: directory), makePaths: { nil })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    try Data().write(to: directory)
    await session.setVoiceOn(false)
    let off = try #require(await socket.sent.last { $0.type == "set_silence" })
    await acknowledgeSilence(socket, off, value: true)
    try await eventuallySession { !session.voicePending }
    #expect(session.voiceMessage?.contains("could not be saved") == true)
    #expect(!session.voiceOn)
    try FileManager.default.removeItem(at: directory)
    await session.setVoiceOn(false)
    try await eventuallySession { !session.voicePending && session.voiceMessage == nil }
    await session.unpair()
}


@MainActor @Test func noSavedChoiceLearnsHubSilenceWithoutCreatingLocalPreference() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let socket = FakeSocket(), store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let voiceStore = VoiceSettingStore(directory: directory)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             voiceStore: voiceStore, makePaths: { nil })
    await session.launch()
    await socket.text(event(1, "status", #"{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":null,"mac_voice_on":true,"silence":true}"#))
    await socket.text(#"{"version":1,"type":"replay_complete","through_seq":1}"#)
    try await eventuallySession { session.connection.canSend && !session.voiceOn }
    #expect(await socket.sent.map(\.type) == ["set_presence"])
    #expect(try voiceStore.load(testPairingCredentials()) == nil)
    await session.unpair()
}

@MainActor @Test func queuedEarlierOffConfirmationCannotConfirmLatestOffChoice() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.setVoiceOn(false)
    let earlierOff = try #require(await socket.sent.last { $0.type == "set_silence" })
    await acknowledgeSilence(socket, earlierOff, value: true)
    try await eventuallySession { await session.client?.results[earlierOff.id] != nil }
    await session.setVoiceOn(true)
    await session.setVoiceOn(false)
    let latestOff = try #require(await socket.sent.last { $0.type == "set_silence" })
    #expect(latestOff.id != earlierOff.id)
    await socket.text(event(2, "reply_end", #"{"reply_id":"barrier","text":"Consumer resumed.","reason":"complete"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "Consumer resumed." } }
    #expect(!session.voiceOn && session.voicePending)
    await acknowledgeSilence(socket, latestOff, value: true)
    try await eventuallySession { !session.voicePending }
    await session.unpair()
}

@MainActor @Test func focusLabelFollowsCanonicalStatusAcrossLateAcknowledgement() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.takeFocus()
    let command = try #require(await socket.sent.first { $0.type == "take_focus" })
    #expect(command.payload.isEmpty)
    #expect(session.focusPending && !session.focusHere)
    await socket.text(event(2, "status", #"{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":"iphone-1","mac_voice_on":true,"silence":false}"#))
    try await eventuallySession { session.focusHere }
    await socket.text(event(3, "status", #"{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":"terminal","mac_voice_on":true,"silence":false}"#))
    try await eventuallySession { !session.focusHere }
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(command.id)\",\"ok\":true,\"result\":{\"focus_client_id\":\"iphone-1\"}}")
    try await eventuallySession { !session.focusPending }
    #expect(!session.focusHere)
    await session.unpair()
}

@MainActor @Test func disconnectedFocusDoesNotQueueAndUnpairClearsLegacyGesture() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let settings = VoiceSettingStore(directory: directory), store = MemoryPairingStore()
    let credentials = try testPairingCredentials()
    store.record = credentials
    try settings.save(false, for: credentials)
    let voiceFile = try #require(FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil).first)
    let legacyFile = voiceFile.deletingPathExtension().appendingPathExtension("gesture.json")
    try Data("\"tap\"".utf8).write(to: legacyFile)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in throw URLError(.notConnectedToInternet) })) }, voiceStore: settings, makePaths: { nil })
    await session.launch(); await session.takeFocus()
    #expect(session.focusMessage == "Couldn't reach the Mac. Not changed.")
    await session.unpair()
    #expect(!FileManager.default.fileExists(atPath: legacyFile.path))
    #expect(try settings.load(credentials) == nil)
}

@MainActor @Test func uncertainFocusRetainsOneRequestAndDoesNotClaimNotChanged() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, sendCommand: { client, command in
        try await client.send(command)
        if command.type == "take_focus" { throw URLError(.networkConnectionLost) }
    })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.takeFocus(); await session.takeFocus()
    #expect(session.focusPending)
    #expect(session.focusMessage?.contains("not confirmed") == true)
    #expect(await socket.sent.filter { $0.type == "take_focus" }.count == 1)
    await session.unpair()
}

@MainActor @Test func aVoiceChoiceTheMacHasntConfirmedShowsNoMessage() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, historyDirectory: directory,
                             makeTransport: { _ in HubTransport(connect: { _ in throw URLError(.cannotConnectToHost) }) }, makePaths: { nil })
    await session.launch()
    await session.setVoiceOn(false)
    try await Task.sleep(for: .milliseconds(300))
    #expect(!session.voiceOn && session.voicePending)
    #expect(session.voiceMessage == nil)
    await session.unpair()
}
