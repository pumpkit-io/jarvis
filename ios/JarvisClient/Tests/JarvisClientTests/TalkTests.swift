import Foundation
import Testing
@testable import JarvisClient

@MainActor final class FakeTalkRecorder: TalkRecorder {
    var level = 0.0
    var starts = 0
    var stops = 0
    var active = false
    var pcm = Data(repeating: 1, count: 32_000)
    var fails = false
    func start(limitReached: @escaping @MainActor @Sendable () -> Void) throws {
        if fails { throw CocoaError(.featureUnsupported) }
        starts += 1; active = true
    }
    func stop() -> Data { stops += 1; active = false; return pcm }
}

/// Stands in for the Hub socket and the Outbox around a TalkController.
@MainActor final class TalkWire {
    var connected = true
    var begins: [String] = []
    var cancels: [String] = []
    var deliveries: [(talkID: String, pcm: Data, recordedAt: Date)] = []
    var commands: [String] { begins.map { _ in "talk_begin" } + cancels.map { _ in "talk_cancel" } }

    func attach(_ talk: TalkController) {
        talk.configure(begin: { id in
            guard self.connected else { return false }
            self.begins.append(id); return true
        }, cancel: { self.cancels.append($0) }, deliver: { self.deliveries.append(($0, $1, $2)) })
    }
}

@MainActor final class HeldMicrophonePermission {
    var waiter: CheckedContinuation<Bool, Never>?
    func request() async -> Bool { await withCheckedContinuation { waiter = $0 } }
    func grant() { waiter?.resume(returning: true); waiter = nil }
}

@MainActor private func wired(_ recorder: FakeTalkRecorder, permission: @escaping @MainActor () async -> Bool = { true }) -> (TalkController, TalkWire) {
    let talk = TalkController(recorder: recorder, permission: permission, clock: { Date(timeIntervalSince1970: 500) })
    let wire = TalkWire(); wire.attach(talk)
    return (talk, wire)
}

@MainActor @Test func releaseQueuesTheTalkUnderItsBegunIdentityAndTheKeyStaysFree() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    await talk.press()
    #expect(recorder.active && talk.state == .recording)
    try await eventuallySession { wire.begins.count == 1 }
    await talk.release()
    #expect(!recorder.active)
    #expect(wire.deliveries.map(\.talkID) == wire.begins)
    #expect(wire.deliveries.first?.pcm == recorder.pcm)
    #expect(wire.deliveries.first?.recordedAt == Date(timeIntervalSince1970: 500))
    #expect(talk.state == .idle && talk.canPress)
    await talk.press(); await talk.release()
    #expect(wire.deliveries.count == 2)
}

@MainActor @Test func recordingNeverWaitsOnTheConnection() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    wire.connected = false
    #expect(talk.canPress)
    await talk.press()
    #expect(recorder.active)
    await talk.release()
    #expect(wire.deliveries.count == 1 && wire.begins.isEmpty)
    await talk.press(); await talk.cancel()
    #expect(wire.cancels.isEmpty && wire.deliveries.count == 1)
}

@MainActor @Test func delayedMicrophoneGrantCannotReviveReleasedOrCancelledHold() async throws {
    for action in ["release", "cancel"] {
        let recorder = FakeTalkRecorder(), permission = HeldMicrophonePermission()
        let (talk, wire) = wired(recorder, permission: { await permission.request() })
        let press = Task { await talk.press() }
        try await eventuallySession { permission.waiter != nil }
        if action == "release" { await talk.release() } else { await talk.cancel() }
        permission.grant(); await press.value
        #expect(recorder.starts == 0)
        #expect(wire.begins.isEmpty && wire.deliveries.isEmpty)
    }
}

@MainActor @Test func slidingLeftDuringPermissionDiscardsBeforeRecordingCanStart() async throws {
    let recorder = FakeTalkRecorder(), permission = HeldMicrophonePermission()
    let (talk, wire) = wired(recorder, permission: { await permission.request() })
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { permission.waiter != nil }
    await talk.holdMoved(horizontalTranslation: -121, verticalTranslation: 0, interaction: control).value
    #expect(talk.state == .cancelled && talk.outcome == .discarded)
    permission.grant()
    try await eventuallySession { talk.state != .authorizing }
    await talk.holdEnded(interaction: control).value
    #expect(recorder.starts == 0)
    #expect(wire.begins.isEmpty && wire.deliveries.isEmpty)
}

@MainActor @Test func slidingLeftWhileInterruptingDiscardsBeforeRequestingPermission() async throws {
    let recorder = FakeTalkRecorder()
    var stoppedSpeech: CheckedContinuation<Void, Never>?
    var permissionRequests = 0
    let talk = TalkController(recorder: recorder, permission: { permissionRequests += 1; return true })
    let wire = TalkWire()
    talk.configure(begin: { id in wire.begins.append(id); return true },
                   cancel: { wire.cancels.append($0) },
                   deliver: { wire.deliveries.append(($0, $1, $2)) },
                   stopSpeech: { await withCheckedContinuation { stoppedSpeech = $0 } })
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { stoppedSpeech != nil }
    await talk.holdMoved(horizontalTranslation: -121, verticalTranslation: 0, interaction: control).value
    #expect(talk.state == .cancelled && talk.outcome == .discarded)
    stoppedSpeech?.resume(); stoppedSpeech = nil
    await talk.holdEnded(interaction: control).value
    #expect(permissionRequests == 0 && recorder.starts == 0)
    #expect(wire.begins.isEmpty && wire.deliveries.isEmpty)
}

@MainActor @Test func slideCancellationStopsRecorderAndOnlyTellsAHubThatHeardTheBegin() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    await talk.press(); await talk.cancel(); await talk.release()
    #expect(!recorder.active && recorder.stops == 1)
    #expect(wire.cancels == wire.begins && wire.cancels.count == 1)
    #expect(wire.deliveries.isEmpty)
    #expect(talk.state == .cancelled && talk.outcome == .discarded && talk.message == nil)
    await talk.press()
    #expect(recorder.starts == 2)
}

@MainActor @Test func aSlipOfTheFingerIsCancelledInsteadOfQueued() async throws {
    let recorder = FakeTalkRecorder()
    recorder.pcm = Data(repeating: 1, count: 9_598)
    let (talk, wire) = wired(recorder)
    await talk.press(); await talk.release()
    #expect(wire.deliveries.isEmpty && wire.cancels.count == 1)
    #expect(talk.state == .cancelled && talk.outcome == .tooShort)
    #expect(talk.message == nil)
}

@MainActor @Test func fiveMinuteTimerSendsOnceAndPermissionRefusalDoesNotCapture() async throws {
    let sleeper = FakeSleeper(), recorder = FakeTalkRecorder(), clock = TalkTestClock()
    let talk = TalkController(recorder: recorder, permission: { true }, now: { clock.time }, sleep: { try await sleeper.sleep($0) })
    let wire = TalkWire(); wire.attach(talk)
    await talk.press()
    try await eventuallySession { await sleeper.pending == 1 }
    clock.time = 301
    await sleeper.advance()
    try await eventuallySession { wire.deliveries.count == 1 }
    await talk.release()
    #expect(recorder.stops == 1)
    #expect(talk.elapsed == 300)
    let refused = TalkController(recorder: recorder, permission: { false })
    wire.attach(refused)
    await refused.press()
    #expect(refused.state == .refused)
    #expect(refused.message == "Jarvis needs the microphone to hear you. Allow it in Settings.")
    #expect(recorder.starts == 1)
}

@MainActor final class TalkTestClock { var time = 0.0 }

@MainActor @Test func permissionRefusalRemainsVisibleAfterPermissionDialogEndsHold() async throws {
    let permission = HeldMicrophonePermission(), recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder, permission: { await permission.request() })
    let press = Task { await talk.press() }
    try await eventuallySession { permission.waiter != nil }
    await talk.cancel()
    permission.waiter?.resume(returning: false); permission.waiter = nil
    await press.value
    #expect(talk.microphoneRefused)
    #expect(talk.message == "Jarvis needs the microphone to hear you. Allow it in Settings.")
    #expect(recorder.starts == 0 && wire.begins.isEmpty)
}

@MainActor @Test func backgroundAndUnpairInvalidateDelayedPermissionThroughAppSession() async throws {
    for transition in ["background", "unpair"] {
        let socket = FakeSocket(), recorder = FakeTalkRecorder(), permission = HeldMicrophonePermission()
        let controller = TalkController(recorder: recorder, permission: { await permission.request() })
        let store = MemoryPairingStore(); store.record = try testPairingCredentials()
        let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                                 makePaths: { nil }, talk: controller)
        await session.launch(); await socket.ready()
        try await eventuallySession { session.connection.canSend }
        let press = Task { await session.talk.press() }
        try await eventuallySession { permission.waiter != nil }
        if transition == "background" { await session.setForeground(false) }
        else { await session.unpair() }
        permission.grant(); await press.value
        #expect(recorder.starts == 0)
        #expect(await socket.sent.contains(where: { $0.type == "talk_begin" }) == false)
        await session.unpair()
    }
}

@MainActor @Test func sessionInterruptsSpeechThenBeginsAndQueuesTheTalkUnderTheBegunIdentity() async throws {
    let socket = FakeSocket(), recorder = FakeTalkRecorder(), store = MemoryPairingStore(), transport = FakeOutboxTransport()
    store.record = try testPairingCredentials()
    var stops = 0
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: TalkController(recorder: recorder, permission: { true }),
                             makeOutboxTransport: { transport }, stopPlayback: { stops += 1 })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"older","reply_id":"older-reply","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventuallySession { session.speechID == "older" }
    session.reportCompletedClip(speechID: "older", index: 0)
    session.reportCompletedClip(speechID: "stale", index: 50)
    await session.talk.press()
    #expect(stops == 1)
    try await eventuallySession { await socket.sent.contains { $0.type == "talk_begin" } }
    let commands = await socket.sent
    let interrupt = try #require(commands.first(where: { $0.type == "interrupt" }))
    #expect(interrupt.payload == ["speech_id": .string("older"), "last_clip_index": .integer(0)])
    let begin = try #require(commands.first(where: { $0.type == "talk_begin" }))
    #expect(commands.firstIndex(of: interrupt)! < commands.firstIndex(of: begin)!)
    await session.talk.release()
    #expect(session.talk.state == .idle && !recorder.active)
    let upload = try #require(transport.started.first)
    #expect(.string(String(upload.request.url!.lastPathComponent)) == begin.payload["talk_id"])
    #expect(session.outbox?.items.first?.status == .sending)
    await session.unpair()
}

@MainActor @Test func talkAndLinesQueueWhileTheMacIsUnreachableAndUnpairDropsThem() async throws {
    let socket = FakeSocket(), recorder = FakeTalkRecorder(), store = MemoryPairingStore(), transport = FakeOutboxTransport()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: TalkController(recorder: recorder, permission: { true }),
                             makeOutboxTransport: { transport })
    await session.launch()
    #expect(!session.connection.canSend && session.talk.canPress)
    await session.talk.press(); await session.talk.release()
    await session.talk.press(); await session.talk.release()
    session.draft = "And one more thing"
    #expect(session.canSubmit)
    await session.sendTypedLine()
    #expect(session.draft.isEmpty)
    #expect(session.outbox?.items.map(\.status) == [.sending, .waiting, .waiting])
    #expect(await socket.sent.isEmpty)
    transport.answer(0, request: "r1")
    transport.answer(1, request: "r2")
    #expect(transport.started.last?.request.url?.path == "/v1/lines")
    await socket.ready()
    await socket.text(event(2, "heard", #"{"request_id":"r1","text":"First","spoken":true}"#))
    try await eventuallySession { session.outbox?.items.count == 2 }
    await session.unpair()
    #expect(session.outbox == nil && transport.cancelled)
}

@MainActor @Test func aLiveTalkBehindOlderMessagesReleasesTheFloorAndGoesAsQueued() async throws {
    let socket = FakeSocket(), recorder = FakeTalkRecorder(), store = MemoryPairingStore(), transport = FakeOutboxTransport()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: TalkController(recorder: recorder, permission: { true }),
                             makeOutboxTransport: { transport })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    session.draft = "Earlier"; await session.sendTypedLine()
    await session.talk.press()
    try await eventuallySession { await socket.sent.contains { $0.type == "talk_begin" } }
    let begun = try #require(await socket.sent.first { $0.type == "talk_begin" }?.payload["talk_id"])
    await session.talk.release()
    try await eventuallySession { await socket.sent.contains { $0.type == "talk_cancel" } }
    #expect(await socket.sent.first { $0.type == "talk_cancel" }?.payload["talk_id"] == begun)
    guard case .talk(let queued, _) = session.outbox?.items.last?.content else { Issue.record("No queued Talk"); return }
    #expect(.string(queued) != begun)
    await session.unpair()
}

@MainActor @Test func theHeaderSaysReadyThroughABriefDropAndOneOutageShowsAfterThreeSeconds() async throws {
    let transport = FakeTransport(), store = MemoryPairingStore(), notice = FakeSleeper()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    }, makePaths: { nil }, noticeSleep: { try await notice.sleep($0) })
    await session.launch()
    try await eventuallySession { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0)); await first.ready()
    try await eventuallySession { session.shownConnection == .ready }
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventuallySession { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    second.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventuallySession { await transport.socket(2) != nil }
    #expect(session.shownConnection == .ready)
    #expect(await notice.pending == 1)
    await notice.advance()
    try await eventuallySession { session.shownConnection != .ready }
    let third = try #require(await transport.socket(2)); await third.ready(through: 1)
    try await eventuallySession { session.shownConnection == .ready }
    await session.unpair()
}

@MainActor @Test func oldViewCleanupCannotCancelTheCurrentCapture() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let old = UUID(), current = UUID()
    await talk.press(interaction: old)
    await talk.cancel()
    await talk.press(interaction: current)
    await talk.controlDisappeared(interaction: old)
    await talk.cancelInteraction(old)
    #expect(recorder.active)
    await talk.release()
    #expect(wire.deliveries.count == 1)
}

@MainActor @Test func secondAccessibilityActionRetiresPendingPermission() async throws {
    let recorder = FakeTalkRecorder(), permission = HeldMicrophonePermission()
    let (talk, wire) = wired(recorder, permission: { await permission.request() })
    let control = UUID()
    let first = talk.activate(interaction: control)
    try await eventuallySession { permission.waiter != nil }
    await talk.activate(interaction: control).value
    permission.grant(); await first.value
    #expect(!recorder.active && recorder.starts == 0)
    #expect(wire.begins.isEmpty && wire.deliveries.isEmpty)
}

@MainActor @Test func cancelledDeferredPressCannotOutliveItsReplacementInteraction() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let old = UUID(), replacement = UUID()
    let deferred = Task { await talk.press(interaction: old) }
    deferred.cancel()
    await talk.press(interaction: replacement)
    await deferred.value
    await talk.cancelInteraction(replacement)
    #expect(recorder.starts == 1 && !recorder.active)
    #expect(wire.begins.count == 1 && wire.cancels == wire.begins)
    #expect(wire.deliveries.isEmpty)
}

@MainActor @Test func secondTapBeforeDeferredStartCannotLeaveTheMicrophoneRunning() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let intent = UUID()
    let deferred = Task { await talk.press(interaction: intent) }
    await talk.releaseInteraction(intent, pendingPress: deferred).value
    await deferred.value
    #expect(recorder.starts == 0 && !recorder.active)
    #expect(wire.begins.isEmpty && wire.deliveries.isEmpty)
}

@MainActor @Test func interruptedHoldStopsCaptureAndNextHoldSendsNormally() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let first = UUID(), second = UUID()
    talk.holdBegan(interaction: first)
    try await eventuallySession { recorder.active }
    await talk.holdInterrupted(interaction: first).value
    #expect(!recorder.active && wire.deliveries.isEmpty)
    #expect(talk.state == .cancelled)
    talk.holdBegan(interaction: second)
    try await eventuallySession { recorder.active }
    await talk.holdEnded(interaction: second).value
    #expect(!recorder.active && wire.deliveries.count == 1)
}

@MainActor @Test func endedTouchCannotBeCancelledByItsGestureStateReset() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    let ended = talk.holdEnded(interaction: control)
    await talk.holdInterrupted(interaction: control).value
    await ended.value
    #expect(wire.deliveries.count == 1)
    #expect(wire.cancels.isEmpty)
}

@MainActor @Test func interruptedPermissionCannotReviveAndVoiceOverCanSendOrCancel() async throws {
    let recorder = FakeTalkRecorder(), permission = HeldMicrophonePermission()
    var askPermission = true
    let (talk, wire) = wired(recorder, permission: { askPermission ? await permission.request() : true })
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { permission.waiter != nil }
    await talk.holdInterrupted(interaction: control).value
    askPermission = false
    await talk.activate(interaction: control).value
    #expect(recorder.active)
    permission.grant()
    await talk.activate(interaction: control).value
    #expect(!recorder.active && wire.deliveries.count == 1)
    await talk.activate(interaction: control).value
    #expect(recorder.active)
    await talk.controlDisappeared(interaction: control)
    #expect(!recorder.active && wire.deliveries.count == 1)
}

@MainActor final class FakeTalkSession: TalkAudioSession {
    var active = false
    var activations = 0
    var releases = 0
    func activate() throws { active = true; activations += 1 }
    func release() { active = false; releases += 1 }
}

@MainActor final class FakeCaptureSource: TalkCaptureSource {
    var buffer: TalkPCMBuffer?
    var limit: (@MainActor @Sendable () -> Void)?
    var active = false
    var fails = false
    func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws {
        if fails { throw HubProtocolError.invalidFrame }
        self.buffer = buffer; limit = limitReached; active = true
    }
    func stop() { active = false }
    func frames(_ bytes: Data) { if buffer?.append(bytes) == true { limit?() } }
}

@MainActor @Test func recorderReleasesAudioSessionBeforeReturningPcmAndBoundsDelayedTimer() throws {
    let session = FakeTalkSession(), source = FakeCaptureSource()
    let recorder = NativeTalkRecorder(session: session, source: source)
    var reached = 0
    try recorder.start { reached += 1 }
    #expect(session.active && source.active)
    source.frames(Data(repeating: 9, count: 9_600_008))
    source.frames(Data(repeating: 8, count: 100))
    #expect(reached == 1)
    let bytes = recorder.stop()
    #expect(bytes.count == 9_600_000)
    #expect(!session.active && !source.active)
    #expect(session.releases == 1)
    #expect(recorder.stop().isEmpty)
    source.fails = true
    #expect(throws: HubProtocolError.self) { try recorder.start {} }
    #expect(!session.active)
    #expect(session.releases == 2)
}

@MainActor @Test func slidingUpLocksAndReleasingKeepsTheTalkUntilExplicitSend() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    await talk.holdMoved(horizontalTranslation: 0, verticalTranslation: -61, interaction: control).value
    #expect(talk.isLocked && talk.state == .recording)
    await talk.holdEnded(interaction: control).value
    #expect(recorder.active && wire.deliveries.isEmpty)
    await talk.send()
    await talk.send()
    #expect(!recorder.active && wire.deliveries.count == 1)
    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    await talk.holdEnded(interaction: control).value
    #expect(wire.deliveries.count == 2)
}

@MainActor @Test func slidingLeftOrTrashingALockedTalkDiscardsWithoutDelivery() async throws {
    for locked in [false, true] {
        let recorder = FakeTalkRecorder()
        let (talk, wire) = wired(recorder)
        let control = UUID()
        talk.holdBegan(interaction: control)
        try await eventuallySession { recorder.active }
        await talk.holdMoved(horizontalTranslation: -60, verticalTranslation: -30, interaction: control).value
        #expect(talk.lockProgress == 0.5 && talk.discardProgress == 0.5)
        if locked {
            await talk.holdMoved(verticalTranslation: -61, interaction: control).value
            await talk.holdEnded(interaction: control).value
            await talk.discard()
        } else {
            await talk.holdMoved(horizontalTranslation: -121, verticalTranslation: 0, interaction: control).value
            await talk.holdEnded(interaction: control).value
        }
        #expect(!recorder.active && !talk.isLocked)
        #expect(wire.deliveries.isEmpty && wire.cancels.count == 1)
    }
}

@MainActor @Test(arguments: ["control", "background", "unpair"])
func leavingALockedTalkRetiresTheMicrophoneWithoutSending(transition: String) async throws {
    let recorder = FakeTalkRecorder(), store = MemoryPairingStore(), socket = FakeSocket()
    store.record = try testPairingCredentials()
    let talk = TalkController(recorder: recorder, permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil }, talk: talk)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    await talk.holdMoved(verticalTranslation: -61, interaction: control).value
    await talk.holdEnded(interaction: control).value
    #expect(talk.isLocked && recorder.active)
    if transition == "control" { await talk.controlDisappeared(interaction: control) }
    else if transition == "background" { await session.setForeground(false) }
    else { await session.unpair() }
    #expect(!recorder.active && !talk.isLocked)
    #expect(session.outbox?.items.isEmpty ?? true)
    await session.unpair()
}

@MainActor @Test func accessibilityStartsAndSendsOneTalk() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let control = UUID()
    await talk.activate(interaction: control).value
    #expect(recorder.active)
    await talk.activate(interaction: control).value
    #expect(!recorder.active && wire.deliveries.count == 1)
}

@MainActor @Test func onlyTheUsersDiscardsCountAsDiscards() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, _) = wired(recorder)
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    await talk.holdMoved(horizontalTranslation: -121, verticalTranslation: 0, interaction: control).value
    await talk.holdEnded(interaction: control).value
    #expect(talk.discards == 1)

    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    await talk.holdMoved(verticalTranslation: -61, interaction: control).value
    await talk.holdEnded(interaction: control).value
    await talk.discard()
    #expect(talk.discards == 2)

    talk.holdBegan(interaction: control)
    try await eventuallySession { recorder.active }
    await talk.controlDisappeared(interaction: control)
    #expect(!recorder.active && talk.outcome == .discarded)
    #expect(talk.discards == 2)
}

@MainActor @Test func eachDiscardIsConfirmedOnceWhicheverScreenAsks() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, _) = wired(recorder)
    #expect(!talk.takeDiscardConfirmation())
    await talk.press(); await talk.discard()
    #expect(talk.takeDiscardConfirmation())
    #expect(!talk.takeDiscardConfirmation())
}

@MainActor @Test func cancellingFromVoiceOverIsADiscard() async throws {
    let recorder = FakeTalkRecorder()
    let (talk, wire) = wired(recorder)
    let control = UUID()
    await talk.activate(interaction: control).value
    #expect(recorder.active)
    await talk.cancel(interaction: control)
    #expect(!recorder.active && wire.deliveries.isEmpty)
    #expect(talk.discards == 1 && talk.takeDiscardConfirmation())
    await talk.cancel(interaction: control)
    #expect(talk.discards == 1)
}

@MainActor @Test func slidingAwayAFailedMicrophoneIsNotADiscard() async throws {
    let recorder = FakeTalkRecorder()
    recorder.fails = true
    let (talk, _) = wired(recorder)
    let control = UUID()
    talk.holdBegan(interaction: control)
    try await eventuallySession { talk.state == .failed }
    await talk.holdMoved(horizontalTranslation: -121, verticalTranslation: 0, interaction: control).value
    #expect(talk.discards == 0)
}
