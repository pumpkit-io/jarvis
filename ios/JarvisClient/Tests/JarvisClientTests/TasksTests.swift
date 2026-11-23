import Foundation
import Testing
@testable import JarvisClient

@MainActor @Test func tasksUpdateFromSnapshotsKeepWaitingFirstAndClearQuestions() async throws {
    let socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil })
    await session.launch()
    #expect(session.updatingTasks)
    await socket.ready()
    try await eventuallySession { session.connection.canSend }
    #expect(session.updatingTasks)
    await socket.text(event(2, "task_snapshot", #"{"tasks":[{"id":"running","title":"Write tests","state":"working","last_report":"Tests are running.","approval_question":null},{"id":"waiting","title":"Merge the fix","state":"needs_user","last_report":"Ready to merge.","approval_question":"Shall I merge PR 91 into main, including the migration?"}]}"#))
    try await eventuallySession { session.openTasks.count == 2 }
    #expect(!session.updatingTasks)
    #expect(session.openTasks.map(\.id) == ["waiting", "running"])
    #expect(session.openTasks.first?.approvalQuestion == "Shall I merge PR 91 into main, including the migration?")
    #expect(session.openTasks.last?.lastReport == "Tests are running.")
    await socket.text(event(3, "pending_approval", #"{"approvals":[]}"#))
    try await eventuallySession { session.openTasks.first?.approvalQuestion == nil }
    await socket.text(event(4, "task_snapshot", #"{"tasks":[]}"#))
    try await eventuallySession { session.openTasks.isEmpty }
    #expect(!session.updatingTasks)
    await session.unpair()
}

@MainActor @Test func cachedTasksStayReadableThroughReconnectAndOlderHistoryCannotRestoreClosedTasks() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: directory) }
    let credentials = try testPairingCredentials()
    let cache = HistoryStore(directory: directory, credentials: credentials, now: { Date(timeIntervalSince1970: 1000) })
    let saved = try conversationEvent(20, "task_snapshot", #"{"tasks":[{"id":"old","title":"Cached Task","state":"needs_user","last_report":"Waiting for your answer.","approval_question":"May I merge it?"}]}"#)
    try await cache.commit(saved, through: 20)
    try await cache.flush()
    let socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = credentials
    let session = AppSession(store: store, historyDirectory: directory, historyNow: { Date(timeIntervalSince1970: 1000) },
        makeTransport: { _ in HubTransport(connect: { _ in socket }, request: { _ in
            Data(#"{"version":1,"events":[{"version":1,"seq":10,"time":"2026-11-19T12:00:00Z","type":"task_snapshot","client_id":null,"payload":{"tasks":[{"id":"ancient","title":"Already closed","state":"working","last_report":null,"approval_question":null}]}}],"next_before":null,"has_more":false}"#.utf8)
        }) }, makePaths: { nil })
    await session.launch()
    #expect(session.openTasks.map(\.title) == ["Cached Task"])
    #expect(session.updatingTasks)
    await socket.ready(through: 20)
    try await eventuallySession { session.connection.canSend }
    #expect(session.updatingTasks)
    await socket.text(event(22, "task_snapshot", #"{"tasks":[]}"#))
    try await eventuallySession { !session.updatingTasks }
    #expect(session.openTasks.isEmpty)
    await session.loadOlderHistory()
    #expect(session.openTasks.isEmpty)
    #expect(session.conversation.events.contains { $0.seq == 10 })
    await session.unpair()
    #expect(session.openTasks.isEmpty)
}

@MainActor @Test func taskQuestionsExpireAndControlsUseTheSameTalkAndVoiceOwners() async throws {
    let socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let recorder = FakeTalkRecorder()
    let controller = TalkController(recorder: recorder, permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             historyNow: { Date(timeIntervalSince1970: 1000) }, makePaths: { nil }, talk: controller)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "task_snapshot", #"{"tasks":[{"id":"waiting","title":"Publish","state":"needs_user","last_report":"Prepared.","approval_question":"Publish the complete draft?"}]}"#))
    await socket.text(event(3, "pending_approval", #"{"approvals":[{"id":"a","task_id":"waiting","question":"Publish the complete draft?","expires_at":"1970-01-01T00:01:00Z"}]}"#))
    try await eventuallySession { session.conversation.events.last?.seq == 3 }
    #expect(session.openTasks.map(\.title) == ["Publish"])
    #expect(session.openTasks.first?.approvalQuestion == nil)
    await session.talk.press()
    #expect(recorder.active)
    try await eventuallySession { await socket.sent.contains { $0.type == "talk_begin" } }
    await session.setVoiceOn(false)
    #expect(recorder.active)
    let silence = try #require(await socket.sent.first { $0.type == "set_silence" })
    await acknowledgeSilence(socket, silence, value: true)
    try await eventuallySession { !session.voicePending }
    #expect(!session.voiceOn)
    await session.talk.cancel()
    #expect(!recorder.active)
    try await eventuallySession { await socket.sent.contains { $0.type == "talk_cancel" } }
    #expect(await socket.sent.map(\.type).filter { $0 == "talk_begin" || $0 == "talk_cancel" } == ["talk_begin", "talk_cancel"])
    #expect(await socket.sent.allSatisfy { !["approve", "refuse"].contains($0.type) })
    await session.unpair()
    #expect(session.openTasks.isEmpty)
}

@MainActor @Test func talkCancellationLeavesTheConnectionAndTasksAlone() async throws {
    let socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let controller = TalkController(recorder: FakeTalkRecorder(), permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: controller)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "task_snapshot", #"{"tasks":[{"id":"working","title":"Write tests","state":"working","last_report":"Running.","approval_question":null}]}"#))
    try await eventuallySession { !session.updatingTasks }
    await controller.press()
    await controller.cancel()
    try await eventuallySession { await socket.sent.contains { $0.type == "talk_cancel" } }
    #expect(session.connection.canSend)
    #expect(!session.updatingTasks)
    #expect(session.openTasks.map(\.title) == ["Write tests"])
    await session.unpair()
}

@MainActor @Test func actualReconnectKeepsTasksUpdatingUntilAFreshSnapshot() async throws {
    let transport = FakeTransport()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in }) }, makePaths: { nil })
    await session.launch()
    try await eventuallySession { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    await first.text(event(2, "task_snapshot", #"{"tasks":[{"id":"working","title":"Write tests","state":"working","last_report":"Running.","approval_question":null}]}"#))
    try await eventuallySession { session.connection.canSend && !session.updatingTasks }
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventuallySession { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    #expect(session.openTasks.map(\.title) == ["Write tests"])
    await second.text(event(3, "task_snapshot", #"{"tasks":[{"id":"working","title":"Write tests","state":"working","last_report":"Still running.","approval_question":null}]}"#))
    await second.ready(through: 3)
    try await eventuallySession { session.connection.canSend }
    #expect(session.openTasks.first?.lastReport == "Still running.")
    #expect(session.updatingTasks)
    await second.text(event(5, "task_snapshot", #"{"tasks":[]}"#))
    try await eventuallySession { !session.updatingTasks }
    #expect(session.openTasks.isEmpty)
    await session.unpair()
}

@MainActor @Test func healthyForegroundRenewalDoesNotMakeTasksWaitForAnotherSnapshot() async throws {
    let socket = FakeSocket(), output = FakeSpeechOutput(), audio = SpeechSession()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: SpeechPlayer(session: audio, output: output))
    await session.launch(); await socket.ready()
    await socket.text(event(2, "task_snapshot", #"{"tasks":[]}"#))
    try await eventuallySession { session.connection.canSend && !session.updatingTasks }
    await socket.text(event(3, "speech_begin", #"{"speech_id":"speech","reply_id":"reply","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(4, "speech_clip", #"{"speech_id":"speech","reply_id":"reply","index":0,"text":"Working.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    try await eventuallySession { output.bytes == [Data([1, 0])] }
    let background = Task { await session.setForeground(false) }
    try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
    let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await background.value
    await session.setForeground(true)
    try await eventuallySession { session.connection.canSend }
    #expect(!session.updatingTasks)
    #expect(audio.active)
    #expect(await socket.closed == false)
    await session.unpair()
}

private final class TaskDisplayClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value = Date(timeIntervalSince1970: 1000)
    func now() -> Date { lock.withLock { value } }
    func advance() { lock.withLock { value = Date(timeIntervalSince1970: 2000) } }
}

@MainActor @Test func unrelatedTaskReportPreservesKnownApprovalExpiryWhileOffline() async throws {
    let socket = FakeSocket(), clock = TaskDisplayClock()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             historyNow: { clock.now() }, makePaths: { nil })
    await session.launch(); await socket.ready()
    await socket.text(event(10, "task_snapshot", #"{"tasks":[{"id":"waiting","title":"Merge","state":"needs_user","last_report":"Prepared.","approval_question":"May I merge the fix?"},{"id":"other","title":"Other Task","state":"working","last_report":"Started.","approval_question":null}]}"#))
    await socket.text(event(11, "pending_approval", #"{"approvals":[{"id":"a","task_id":"waiting","question":"May I merge the fix?","expires_at":"1970-01-01T00:25:00Z"}]}"#))
    await socket.text(event(12, "task_snapshot", #"{"tasks":[{"id":"waiting","title":"Merge","state":"needs_user","last_report":"Prepared.","approval_question":"May I merge the fix?"},{"id":"other","title":"Other Task","state":"working","last_report":"Tests are running.","approval_question":null}]}"#))
    try await eventuallySession { session.openTasks.last?.lastReport == "Tests are running." }
    #expect(session.openTasks.first?.approvalQuestion == "May I merge the fix?")
    await session.setForeground(false)
    clock.advance()
    #expect(session.openTasks.first?.approvalQuestion == nil)
    #expect(session.openTasks.map(\.title) == ["Merge", "Other Task"])
    await session.unpair()
}

@MainActor @Test func newTaskQuestionReplacesOldExpiryAndNewPendingSnapshotCanSupersedeOrClearIt() async throws {
    let socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             historyNow: { Date(timeIntervalSince1970: 2000) }, makePaths: { nil })
    await session.launch(); await socket.ready()
    await socket.text(event(10, "task_snapshot", #"{"tasks":[{"id":"waiting","title":"Merge","state":"needs_user","last_report":"Prepared.","approval_question":"Old question?"}]}"#))
    await socket.text(event(11, "pending_approval", #"{"approvals":[{"id":"a","task_id":"waiting","question":"Old question?","expires_at":"1970-01-01T00:25:00Z"}]}"#))
    await socket.text(event(12, "task_snapshot", #"{"tasks":[{"id":"waiting","title":"Merge","state":"needs_user","last_report":"New plan.","approval_question":"Shall I merge the revised fix?"}]}"#))
    try await eventuallySession { session.openTasks.first?.approvalQuestion == "Shall I merge the revised fix?" }
    await socket.text(event(13, "pending_approval", #"{"approvals":[{"id":"b","task_id":"waiting","question":"Shall I publish instead?","expires_at":"1970-01-01T00:50:00Z"}]}"#))
    try await eventuallySession { session.openTasks.first?.approvalQuestion == "Shall I publish instead?" }
    await socket.text(event(14, "pending_approval", #"{"approvals":[]}"#))
    try await eventuallySession { session.openTasks.first?.approvalQuestion == nil }
    #expect(session.openTasks.first?.title == "Merge")
    await session.unpair()
}
