import Foundation
import Testing
@testable import JarvisClient

/// Records session calls and the audio objects starting and stopping between them, in the order they happen, from any thread.
private final class SessionLog: @unchecked Sendable {
    enum Entry: Equatable { case activate(AudioSessionSetup), deactivate, microphoneStarted, microphoneStopped, speechStopped }
    private let lock = NSLock()
    private var recorded: [Entry] = []
    private var main: [Entry] = []
    private var activeTwice = false
    var entries: [Entry] { lock.withLock { recorded } }
    /// The Talk calls that ran on the main thread, where they would freeze the orb.
    var onMainThread: [Entry] { lock.withLock { main } }
    /// iOS ducks only on activation, so applying a setup to an already active session would leave the duck undefined.
    var activatedWhileActive: Bool { lock.withLock { activeTwice } }
    func append(_ entry: Entry) {
        let onMain = Thread.isMainThread
        lock.withLock {
            recorded.append(entry)
            if onMain { main.append(entry) }
        }
    }
    func flagActivatedWhileActive() { lock.withLock { activeTwice = true } }
}

private final class LoggingDriver: AudioSessionDriver, @unchecked Sendable {
    let log: SessionLog
    private let lock = NSLock()
    private var active = false
    /// When set, the next deactivation waits for it, standing in for the hardware being slow.
    var gate: DispatchSemaphore?
    init(log: SessionLog) { self.log = log }
    func activate(_ setup: AudioSessionSetup) throws {
        if lock.withLock({ defer { active = true }; return active }) { log.flagActivatedWhileActive() }
        log.append(.activate(setup))
    }
    func deactivate() {
        if let gate = lock.withLock({ defer { gate = nil }; return gate }) { gate.wait() }
        lock.withLock { active = false }
        log.append(.deactivate)
    }
}

private final class LoggingCapture: TalkCaptureSource, @unchecked Sendable {
    let log: SessionLog
    init(log: SessionLog) { self.log = log }
    func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws { log.append(.microphoneStarted) }
    func stop() { log.append(.microphoneStopped) }
}

@MainActor private final class LoggingSpeech: SpeechOutput {
    let log: SessionLog
    var finished: [@MainActor @Sendable () -> Void] = []
    var level: Double { 0 }
    init(log: SessionLog) { self.log = log }
    func play(_ pcm: Data, finished: @escaping @MainActor @Sendable () -> Void) throws { self.finished.append(finished) }
    func stop() { log.append(.speechStopped) }
}

@MainActor private func playReply(_ player: SpeechPlayer, _ speech: LoggingSpeech, id: String) {
    player.begin(speechID: id, replyID: nil)
    player.append(speechID: id, replyID: nil, index: 0, text: "Done.", pcm: Data([1, 0]))
    player.end(speechID: id, replyID: nil, lastIndex: 0)
    speech.finished.last?()
}

/// A Talk's session calls and microphone run on the audio queue, so a press, a release and a cancel never wait for the audio hardware on the main thread.
@MainActor private func talk(_ recorder: NativeTalkRecorder, _ audio: NativeTalkAudioSession) throws {
    try recorder.start(limitReached: {}, failed: {})
    audio.drain()
    _ = recorder.stop()
    audio.drain()
}

@MainActor @Test func aTalkDucksOnActivationAndUnducksByDeactivatingAfterTheMicrophoneStopsAllOffTheMainThread() throws {
    let log = SessionLog(), audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let recorder = NativeTalkRecorder(session: audio, source: LoggingCapture(log: log))
    try recorder.start(limitReached: {}, failed: {})
    audio.drain()
    #expect(log.entries == [.activate(.recording), .microphoneStarted])
    _ = recorder.stop()
    audio.drain()
    #expect(log.entries == [.activate(.recording), .microphoneStarted, .microphoneStopped, .deactivate])
    #expect(log.onMainThread.isEmpty)
}

@MainActor @Test func aCancelledTalkStopsTheMicrophoneBeforeDeactivatingAllOffTheMainThread() async throws {
    let log = SessionLog(), audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let talk = TalkController(recorder: NativeTalkRecorder(session: audio, source: LoggingCapture(log: log)), permission: { true })
    talk.configure(begin: { _ in true }, cancel: { _ in }, deliver: { _, _, _ in })
    await talk.press()
    audio.drain()
    #expect(log.entries == [.activate(.recording), .microphoneStarted])
    await talk.discard()
    audio.drain()
    #expect(talk.state == .cancelled)
    #expect(log.entries == [.activate(.recording), .microphoneStarted, .microphoneStopped, .deactivate])
    #expect(log.onMainThread.isEmpty)
    #expect(!log.activatedWhileActive)
}

@MainActor @Test func aPressAndAReleaseReturnWhileTheAudioHardwareIsStillBusy() throws {
    let log = SessionLog(), driver = LoggingDriver(log: log), audio = NativeTalkAudioSession(driver: driver)
    let recorder = NativeTalkRecorder(session: audio, source: LoggingCapture(log: log))
    try talk(recorder, audio)
    let hardware = DispatchSemaphore(value: 0)
    driver.gate = hardware
    try recorder.start(limitReached: {}, failed: {})
    audio.drain()
    _ = recorder.stop()
    // The deactivation is still waiting for the hardware, and the main actor has already moved on.
    try recorder.start(limitReached: {}, failed: {})
    #expect(log.entries.filter { $0 == .deactivate }.count == 1)
    hardware.signal()
    audio.drain()
    #expect(log.entries.suffix(4) == [.microphoneStopped, .deactivate, .activate(.recording), .microphoneStarted])
    #expect(!log.activatedWhileActive)
}

@MainActor @Test func aTalkReleasedBeforeTheAudioQueueReachesItNeverTurnsTheMicrophoneOn() throws {
    let log = SessionLog(), speech = LoggingSpeech(log: log), driver = LoggingDriver(log: log)
    let audio = NativeTalkAudioSession(driver: driver)
    let recorder = NativeTalkRecorder(session: audio, source: LoggingCapture(log: log))
    let player = SpeechPlayer(session: audio, output: speech)
    let hardware = DispatchSemaphore(value: 0)
    driver.gate = hardware
    // The reply's deactivation holds the queue while a quick tap starts and ends.
    playReply(player, speech, id: "s")
    try recorder.start(limitReached: {}, failed: {})
    #expect(recorder.stop().isEmpty)
    hardware.signal()
    audio.drain()
    #expect(log.entries == [.activate(.playback), .speechStopped, .deactivate])
}

@MainActor @Test func aMicrophoneThatFailsOnTheQueueDeactivatesAndFreesTheSessionForTheNextTalk() async throws {
    final class FailingCapture: TalkCaptureSource, @unchecked Sendable {
        let log: SessionLog
        var fail = true
        init(log: SessionLog) { self.log = log }
        func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws {
            if fail { fail = false; throw HubProtocolError.invalidFrame }
            log.append(.microphoneStarted)
        }
        func stop() { log.append(.microphoneStopped) }
    }
    let log = SessionLog(), audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let recorder = NativeTalkRecorder(session: audio, source: FailingCapture(log: log))
    var failures = 0
    try recorder.start(limitReached: {}, failed: { failures += 1 })
    audio.drain()
    try await eventuallySession { failures == 1 }
    #expect(log.entries == [.activate(.recording), .microphoneStopped, .deactivate])
    #expect(recorder.stop().isEmpty)
    try talk(recorder, audio)
    #expect(log.entries.suffix(4) == [.activate(.recording), .microphoneStarted, .microphoneStopped, .deactivate])
}

@MainActor @Test func aReplyDucksOnActivationAndUnducksByDeactivatingAfterSpeechStops() {
    let log = SessionLog(), speech = LoggingSpeech(log: log), audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let player = SpeechPlayer(session: audio, output: speech)
    playReply(player, speech, id: "s")
    audio.drain()
    #expect(log.entries == [.activate(.playback), .speechStopped, .deactivate])
}

@MainActor @Test func everyTalkAndReplyGetsItsOwnActivationAndNeverReconfiguresAnActiveSession() throws {
    let log = SessionLog(), speech = LoggingSpeech(log: log)
    let audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let recorder = NativeTalkRecorder(session: audio, source: LoggingCapture(log: log))
    let player = SpeechPlayer(session: audio, output: speech)
    for round in 0..<2 {
        try recorder.start(limitReached: {}, failed: {})
        audio.drain()
        // No drain before the reply: its activation waits on the queue for the Talk's deactivation.
        _ = recorder.stop()
        playReply(player, speech, id: "s\(round)")
    }
    audio.drain()
    let deactivations = log.entries.filter { $0 == .deactivate }.count
    let activations = log.entries.filter { if case .activate = $0 { true } else { false } }.count
    #expect(activations == 4 && deactivations == 4)
    #expect(log.entries.last == .deactivate)
    #expect(!log.activatedWhileActive)
}

@MainActor @Test func liveModeHoldsOneActivationForItsRepliesAndDeactivatesWhenItEnds() throws {
    let log = SessionLog(), audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    try audio.activateLive()
    try audio.activatePlayback()
    audio.releasePlayback()
    try audio.activatePlayback()
    audio.drain()
    #expect(log.entries == [.activate(.recording)])
    // A reply still playing on Live mode's engine keeps the session until it stops.
    audio.releaseLive()
    audio.drain()
    #expect(log.entries == [.activate(.recording)])
    audio.releasePlayback()
    audio.drain()
    #expect(log.entries == [.activate(.recording), .deactivate])
    try audio.activateLive()
    audio.releaseLive()
    audio.drain()
    #expect(log.entries == [.activate(.recording), .deactivate, .activate(.recording), .deactivate])
    #expect(!log.activatedWhileActive)
}

@MainActor @Test func aTalkDuringLiveModeUsesItsSessionWithoutActivatingOrDeactivating() throws {
    let log = SessionLog(), audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let recorder = NativeTalkRecorder(session: audio, source: LoggingCapture(log: log))
    try audio.activateLive()
    try talk(recorder, audio)
    #expect(log.entries == [.activate(.recording), .microphoneStarted, .microphoneStopped])
    audio.releaseLive()
    audio.drain()
    #expect(log.entries.last == .deactivate)
}

@MainActor @Test func aFailedActivationLeavesNothingToDeactivateAndTheNextReplyActivatesAgain() throws {
    final class FailingOnce: AudioSessionDriver, @unchecked Sendable {
        var calls: [String] = []
        var fail = true
        func activate(_ setup: AudioSessionSetup) throws {
            calls.append("activate")
            if fail { fail = false; throw HubProtocolError.invalidFrame }
        }
        func deactivate() { calls.append("deactivate") }
    }
    let driver = FailingOnce(), audio = NativeTalkAudioSession(driver: driver)
    #expect(throws: HubProtocolError.invalidFrame) { try audio.activatePlayback() }
    audio.releasePlayback()
    try audio.activatePlayback()
    audio.releasePlayback()
    audio.drain()
    #expect(driver.calls == ["activate", "activate", "deactivate"])
}

@MainActor @Test func foregroundTouchesNoSessionAndBackgroundEndsATalkWithADeactivation() async throws {
    let log = SessionLog(), socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let talk = TalkController(recorder: NativeTalkRecorder(session: audio, source: LoggingCapture(log: log)), permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk,
                             playback: SpeechPlayer(session: audio, output: LoggingSpeech(log: log)))
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.setForeground(true)
    audio.drain()
    #expect(log.entries.isEmpty)
    await talk.press()
    #expect(talk.state == .recording)
    audio.drain()
    #expect(log.entries == [.activate(.recording), .microphoneStarted])
    await session.setForeground(false)
    audio.drain()
    #expect(log.entries == [.activate(.recording), .microphoneStarted, .microphoneStopped, .deactivate])
    await session.setForeground(true)
    audio.drain()
    #expect(log.entries.count == 4)
    #expect(log.onMainThread.isEmpty)
    await session.unpair()
}
