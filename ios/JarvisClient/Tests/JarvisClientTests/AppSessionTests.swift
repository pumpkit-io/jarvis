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
