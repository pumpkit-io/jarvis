import Foundation
import Testing
@testable import JarvisClient

private func resetExchange() throws -> [String: Any] {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let fixture = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(fixture["exchanges"] as? [String: Any])
    return try #require(exchanges["reset_context"] as? [String: Any])
}

/// The fixture's frame, answering the command this session actually sent.
private func fixtureFrame(_ name: String, answering id: String) throws -> String {
    var frame = try #require(try resetExchange()[name] as? [String: Any])
    frame["id"] = id
    return String(decoding: try JSONSerialization.data(withJSONObject: frame), as: UTF8.self)
}

private func failure(_ code: String, answering id: String) -> String {
    "{\"version\":1,\"type\":\"command_result\",\"id\":\"\(id)\",\"ok\":false,\"error\":{\"code\":\"\(code)\",\"message\":\"x\",\"retry_after_seconds\":null}}"
}

@MainActor private func readySession(_ transport: FakeTransport) async throws -> AppSession {
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    }, makePaths: { nil })
    await session.launch()
    try await eventuallySession { await transport.socket(0) != nil }
    await transport.socket(0)?.ready()
    try await eventuallySession { session.connection.canSend }
    return session
}

@MainActor private func sentReset(_ socket: FakeSocket) async throws -> HubCommand {
    try #require(await socket.sent.first { $0.type == "reset_context" })
}

@Test func theDialogSaysTheResetIsFinalAndWhatItDeletesAndKeeps() {
    #expect(ContextReset.dialogTitle == "Reset context?")
    #expect(ContextReset.dialogMessage == "This cannot be undone. It deletes all the messages and the summary Jarvis keeps, while the 30-day conversation logs already saved are not deleted.")
    #expect(ContextReset.confirmTitle == "Reset context")
}

@MainActor @Test func confirmingSendsTheFixtureCommandAndShowsResettingUntilTheResultSaysReset() async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let socket = try #require(await transport.socket(0))
    #expect(session.canResetContext)
    #expect(session.contextReset.buttonTitle == "Reset context")
    #expect(session.contextReset.statusLine == nil)

    await session.resetContext()
    let command = try await sentReset(socket)
    let expected = try #require(try resetExchange()["command"] as? [String: Any])
    #expect(command.type == expected["type"] as? String)
    #expect(command.payload.isEmpty && (expected["payload"] as? [String: Any])?.isEmpty == true)
    #expect(session.contextReset.buttonTitle == "Resetting")
    #expect(!session.canResetContext)
    #expect(session.contextReset.statusLine == nil)

    await socket.text(try fixtureFrame("result", answering: command.id))
    try await eventuallySession { session.contextReset != .resetting }
    #expect(session.contextReset.statusLine == "Context reset.")
    #expect(session.contextReset.buttonTitle == "Reset context")
    #expect(session.canResetContext)
    await session.unpair()
}

@MainActor @Test(arguments: [
    ("outcome_unknown", "Not sure the reset happened. Try again."),
    ("reset_failed", "Could not finish the reset on the Mac. Try again."),
    ("invalid_payload", "Could not reach the Mac. Nothing was reset."),
])
func eachRefusalShowsItsStatusLineAndAllowsAnotherTry(code: String, line: String) async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let socket = try #require(await transport.socket(0))
    await session.resetContext()
    let command = try await sentReset(socket)
    await socket.text(failure(code, answering: command.id))
    try await eventuallySession { session.contextReset != .resetting }
    #expect(session.contextReset.statusLine == line)
    #expect(session.contextReset.buttonTitle == "Reset context")
    #expect(session.canResetContext)
    await session.unpair()
}

@MainActor @Test func theFixturesRefusedPayloadSaysNothingWasReset() async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let socket = try #require(await transport.socket(0))
    await session.resetContext()
    await socket.text(try fixtureFrame("refused_payload", answering: try await sentReset(socket).id))
    try await eventuallySession { session.contextReset != .resetting }
    #expect(session.contextReset.statusLine == "Could not reach the Mac. Nothing was reset.")
    await session.unpair()
}

@MainActor @Test func aFailedSendSaysNothingWasResetAndIsNeverQueuedOrSentAfterReconnecting() async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let first = try #require(await transport.socket(0))
    await first.failSend("reset_context")
    await session.resetContext()
    #expect(session.contextReset.statusLine == "Could not reach the Mac. Nothing was reset.")
    #expect(session.outbox?.items.isEmpty == true)
    #expect(await session.client?.pendingCommands.isEmpty == true)

    try await eventuallySession { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 1)
    try await eventuallySession { session.connection.canSend }
    #expect(await second.sent.allSatisfy { $0.type != "reset_context" })
    #expect(session.contextReset.statusLine == "Could not reach the Mac. Nothing was reset.")
    await session.unpair()
}

@MainActor @Test func aConnectionLostBeforeTheResultIsUnsureAndTheResetIsNotSentAgain() async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let first = try #require(await transport.socket(0))
    await session.resetContext()
    _ = try await sentReset(first)
    await first.close()
    try await eventuallySession { session.contextReset != .resetting }
    #expect(session.contextReset.statusLine == "Not sure the reset happened. Try again.")

    try await eventuallySession { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 1)
    try await eventuallySession { session.connection.canSend }
    #expect(await second.sent.allSatisfy { $0.type != "reset_context" })
    #expect(session.outbox?.items.isEmpty == true)
    await session.unpair()
}

@MainActor @Test func offlineOrUnpairedTheButtonIsDisabledWithNoStatusLineAndSendsNothing() async throws {
    let unpaired = AppSession(store: MemoryPairingStore(), makeClient: { _ in
        HubClient(transport: HubTransport(connect: { _ in throw URLError(.notConnectedToInternet) }))
    }, makePaths: { nil })
    await unpaired.launch()
    #expect(!unpaired.canResetContext)
    await unpaired.resetContext()
    #expect(unpaired.contextReset.statusLine == nil)

    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let offline = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { _ in throw URLError(.notConnectedToInternet) }))
    }, makePaths: { nil })
    await offline.launch()
    try await eventuallySession { offline.connection == .unreachable }
    #expect(!offline.canResetContext)
    await offline.resetContext()
    #expect(offline.contextReset.statusLine == nil)
    #expect(offline.contextReset.buttonTitle == "Reset context")
    #expect(await offline.client?.pendingCommands.isEmpty == true)
    await offline.unpair()
}

@MainActor @Test func leavingSettingsOrUnpairingClearsTheStatusLine() async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let socket = try #require(await transport.socket(0))
    await session.resetContext()
    await socket.text(try fixtureFrame("result", answering: try await sentReset(socket).id))
    try await eventuallySession { session.contextReset.statusLine == "Context reset." }
    session.leaveSettings()
    #expect(session.contextReset.statusLine == nil)

    await session.resetContext()
    await socket.text(failure("outcome_unknown", answering: try #require(await socket.sent.last { $0.type == "reset_context" }).id))
    try await eventuallySession { session.contextReset.statusLine != nil }
    await session.unpair()
    #expect(session.contextReset == .idle)
}

@MainActor @Test func leavingSettingsWhileResettingKeepsTheResultForTheNextVisit() async throws {
    let transport = FakeTransport()
    let session = try await readySession(transport)
    let socket = try #require(await transport.socket(0))
    await session.resetContext()
    session.leaveSettings()
    #expect(session.contextReset == .resetting)
    await socket.text(try fixtureFrame("result", answering: try await sentReset(socket).id))
    try await eventuallySession { session.contextReset.statusLine == "Context reset." }
    await session.unpair()
}
