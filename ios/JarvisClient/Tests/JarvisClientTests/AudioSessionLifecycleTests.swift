import Foundation
import Testing
@testable import JarvisClient

/// Records session calls and the audio objects stopping between them, in the order they happen.
@MainActor private final class SessionLog {
    enum Entry: Equatable { case activate(AudioSessionSetup), deactivate, microphoneStopped, speechStopped }
    var entries: [Entry] = []
    /// iOS ducks only on activation, so applying a setup to an already active session would leave the duck undefined.
    var activatedWhileActive = false
}

@MainActor private final class LoggingDriver: AudioSessionDriver {
    let log: SessionLog
    private var active = false
    init(log: SessionLog) { self.log = log }
    func activate(_ setup: AudioSessionSetup) throws {
        if active { log.activatedWhileActive = true }
        active = true
        log.entries.append(.activate(setup))
    }
    func deactivate() { active = false; log.entries.append(.deactivate) }
}

@MainActor private final class LoggingCapture: TalkCaptureSource {
    let log: SessionLog
    init(log: SessionLog) { self.log = log }
    func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws {}
    func stop() { log.entries.append(.microphoneStopped) }
}

@MainActor private final class LoggingSpeech: SpeechOutput {
    let log: SessionLog
    var finished: [@MainActor @Sendable () -> Void] = []
    var level: Double { 0 }
    init(log: SessionLog) { self.log = log }
    func play(_ pcm: Data, finished: @escaping @MainActor @Sendable () -> Void) throws { self.finished.append(finished) }
    func stop() { log.entries.append(.speechStopped) }
}

@MainActor private func playReply(_ player: SpeechPlayer, _ speech: LoggingSpeech, id: String) {
    player.begin(speechID: id, replyID: nil)
    player.append(speechID: id, replyID: nil, index: 0, text: "Done.", pcm: Data([1, 0]))
    player.end(speechID: id, replyID: nil, lastIndex: 0)
    speech.finished.last?()
}

@MainActor @Test func aTalkDucksOnActivationAndUnducksByDeactivatingAfterTheMicrophoneStops() throws {
    let log = SessionLog()
    let recorder = NativeTalkRecorder(session: NativeTalkAudioSession(driver: LoggingDriver(log: log)), source: LoggingCapture(log: log))
    try recorder.start {}
    #expect(log.entries == [.activate(.recording)])
    _ = recorder.stop()
    #expect(log.entries == [.activate(.recording), .microphoneStopped, .deactivate])
}

@MainActor @Test func aReplyDucksOnActivationAndUnducksByDeactivatingAfterSpeechStops() {
    let log = SessionLog(), speech = LoggingSpeech(log: log)
    let player = SpeechPlayer(session: NativeTalkAudioSession(driver: LoggingDriver(log: log)), output: speech)
    playReply(player, speech, id: "s")
    #expect(log.entries == [.activate(.playback), .speechStopped, .deactivate])
}

@MainActor @Test func everyTalkAndReplyGetsItsOwnActivationAndNeverReconfiguresAnActiveSession() throws {
    let log = SessionLog(), speech = LoggingSpeech(log: log)
    let audio = NativeTalkAudioSession(driver: LoggingDriver(log: log))
    let recorder = NativeTalkRecorder(session: audio, source: LoggingCapture(log: log))
    let player = SpeechPlayer(session: audio, output: speech)
    for round in 0..<2 {
        try recorder.start {}
        _ = recorder.stop()
        playReply(player, speech, id: "s\(round)")
    }
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
    #expect(log.entries == [.activate(.recording)])
    // A reply still playing on Live mode's engine keeps the session until it stops.
    audio.releaseLive()
    #expect(log.entries == [.activate(.recording)])
    audio.releasePlayback()
    #expect(log.entries == [.activate(.recording), .deactivate])
    try audio.activateLive()
    audio.releaseLive()
    #expect(log.entries == [.activate(.recording), .deactivate, .activate(.recording), .deactivate])
    #expect(!log.activatedWhileActive)
}

@MainActor @Test func aFailedActivationLeavesNothingToDeactivateAndTheNextTalkActivatesAgain() throws {
    final class FailingOnce: AudioSessionDriver {
        var calls: [String] = []
        var fail = true
        func activate(_ setup: AudioSessionSetup) throws {
            calls.append("activate")
            if fail { fail = false; throw HubProtocolError.invalidFrame }
        }
        func deactivate() { calls.append("deactivate") }
    }
    let driver = FailingOnce(), audio = NativeTalkAudioSession(driver: driver)
    #expect(throws: HubProtocolError.invalidFrame) { try audio.activate() }
    audio.release()
    try audio.activate()
    audio.release()
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
    #expect(log.entries.isEmpty)
    await talk.press()
    #expect(log.entries == [.activate(.recording)])
    await session.setForeground(false)
    #expect(log.entries == [.activate(.recording), .microphoneStopped, .deactivate])
    await session.setForeground(true)
    #expect(log.entries.count == 3)
    await session.unpair()
}
