import AVFoundation
import Combine
import Foundation
import Observation

@MainActor public protocol LiveCaptureSource: AnyObject {
    /// `frames` runs on the audio thread with 16 kHz mono PCM16LE, each a whole number of 10 ms frames and at most 100 ms.
    func start(frames: @escaping @Sendable (Data) -> Void) throws
    func stop()
    var onFailure: (@MainActor () -> Void)? { get set }
    /// Plays through the capture's voice-processing engine, so iOS cancels it as echo; nil while capture is stopped.
    var speechOutput: (any SpeechOutput)? { get }
}

/// Starts each reply on the Live mode capture's output while capture runs, and on `standard` otherwise.
@MainActor public final class LiveSpeechRoute: SpeechOutput {
    private let standard: any SpeechOutput
    private let capture: any LiveCaptureSource
    private var current: (any SpeechOutput)?
    public init(standard: any SpeechOutput, capture: any LiveCaptureSource) {
        self.standard = standard; self.capture = capture
    }
    public var level: Double { current?.level ?? 0 }
    // A reply keeps the output it started on until stopped: moving it mid-reply would overlap two engines.
    public func play(_ pcm: Data, finished: @escaping @MainActor @Sendable () -> Void) throws {
        let output = current ?? capture.speechOutput ?? standard
        current = output
        try output.play(pcm, finished: finished)
    }
    public func stop() { (current ?? standard).stop(); current = nil }
}

private struct LiveFrame: Sendable {
    let pcm: Data
    let captured: ContinuousClock.Instant
}

/// Live mode on this iPhone: the Mac listens for the Wake word on the microphone audio streamed to it.
/// Off at every launch and never saved.
@MainActor @Observable public final class LiveModeController {
    /// The Hub's voice floor as this iPhone sees it; every other Live mode iPhone sees `idle`.
    public enum VoiceState: String, Sendable { case idle, listening, thinking, speaking, followUp = "follow_up" }
    public static let onMessage = "Live mode on. Say “Jarvis” to talk."
    public static let textModeMessage = "Live mode needs Jarvis in voice mode on the Mac."
    public static let offlineMessage = "Live mode on. Listening starts when the Mac is back."
    public static let inactivityMessage = "Live mode turned off after 30 minutes of inactivity."
    static let failedMessage = "Couldn't reach the Mac. Not changed."
    static let microphoneFailedMessage = "The microphone couldn't start. Try again."
    public private(set) var isOn = false
    public private(set) var message: String?
    public private(set) var voiceState = VoiceState.idle
    /// RMS of the last frame streamed, for the orb; read every frame, so it is not observed.
    @ObservationIgnored public private(set) var level = 0.0
    private var request: HubCommand?
    private var busy = false
    private var paired = false
    private var ready = false
    private var audioReadyAt: ContinuousClock.Instant?
    private var foreground = true
    private var interrupted = false
    private var generation: UInt64 = 0
    /// A Talk holds the microphone from its press until it is sent or discarded.
    private var talking = false
    private let capture: any LiveCaptureSource
    private let permission: @MainActor () async -> Bool
    private let sleep: @Sendable (Double) async throws -> Void
    private let now: @MainActor () -> ContinuousClock.Instant
    private let inactivitySleep: @Sendable (Duration) async throws -> Void
    private var send: @MainActor (HubCommand) async throws -> Void = { _ in throw HubProtocolError.notConnected }
    private var sendAudio: @Sendable (Data) async -> Void = { _ in }
    private var permissionAnswered: @MainActor (Bool) -> Void = { _ in }
    private var onCaptureFailure: @MainActor () -> Void = {}
    private var onTurnedOff: @MainActor () async -> Void = {}
    private var inactivityDeadline: ContinuousClock.Instant?
    private var inactivityTimer: Task<Void, Never>?
    private var frames: AsyncStream<LiveFrame>.Continuation?
    private var streaming: Task<Void, Never>?
    private var notice: Task<Void, Never>?

    public init(capture: any LiveCaptureSource, permission: @escaping @MainActor () async -> Bool,
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                now: @escaping @MainActor () -> ContinuousClock.Instant = { .now },
                inactivitySleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.capture = capture; self.permission = permission; self.sleep = sleep
        self.now = now; self.inactivitySleep = inactivitySleep
        capture.onFailure = { [weak self] in self?.captureFailed() }
    }

    /// `send` delivers a `set_live` command whose result comes back through `receive`; `sendAudio` drops audio it cannot send.
    public func configure(send: @escaping @MainActor (HubCommand) async throws -> Void,
                          sendAudio: @escaping @Sendable (Data) async -> Void,
                          permissionAnswered: @escaping @MainActor (Bool) -> Void = { _ in },
                          captureFailed: @escaping @MainActor () -> Void = {},
                          turnedOff: @escaping @MainActor () async -> Void = {}) {
        self.send = send; self.sendAudio = sendAudio; self.permissionAnswered = permissionAnswered
        self.onCaptureFailure = captureFailed
        self.onTurnedOff = turnedOff
        paired = true
    }

    public func toggle() async {
        guard paired, !busy, request == nil || isOn else { return }
        busy = true
        defer { busy = false }
        if isOn { await turnOff() } else { await turnOn() }
    }

    public func receive(_ result: CommandResult) {
        guard !expireIfInactive(), let request, result.id == request.id else { return }
        self.request = nil
        if result.ok, result.result?["on"] == .bool(true) {
            guard isOn || foreground else { return }
            audioReadyAt = .now
            let resuming = isOn
            isOn = true
            if !resuming { recordActivity() }
            if startCapture(), !resuming { show(Self.onMessage, for: 2.5) }
        } else if !result.ok {
            _ = stopLive(message: result.error?.code == "voice_unavailable" ? Self.textModeMessage : Self.failedMessage,
                         notifyHub: false)
        }
    }

    public func receive(voiceState event: HubEvent) {
        guard !expireIfInactive(), isOn, let payload = try? event.decodePayload(VoiceStatePayload.self) else { return }
        let next = VoiceState(rawValue: payload.state) ?? .idle
        let changed = next != voiceState
        voiceState = next
        if changed, next == .listening || next == .thinking { recordActivity() }
    }

    public func dropped(commandID: String) {
        guard request?.id == commandID else { return }
        request = nil; enableOffline()
    }

    /// Pauses streaming while a Talk holds the microphone and resumes it once the Talk is sent or discarded.
    public func talkHoldsMicrophone(_ held: Bool) {
        guard talking != held else { return }
        talking = held
        recordActivity()
        if held { pauseCapture() } else { _ = startCapture() }
    }

    public func setForeground(_ value: Bool) {
        foreground = value
        _ = expireIfInactive()
    }

    public func audioInterrupted(_ began: Bool) {
        guard interrupted != began else { return }
        interrupted = began
        guard !expireIfInactive() else { return }
        if began { pauseCapture(); voiceState = .idle } else { _ = startCapture() }
    }

    public func unpair() {
        generation &+= 1
        cancelInactivity()
        paired = false
        request = nil; isOn = false; ready = false; audioReadyAt = nil; voiceState = .idle
        show(nil); pauseCapture()
    }

    public func connectionChanged(ready: Bool) async {
        _ = expireIfInactive()
        guard self.ready != ready else { return }
        self.ready = ready
        if !ready { voiceState = .idle; audioReadyAt = nil }
        else if isOn, request == nil { await requestLive() }
    }

    private func enableOffline() {
        guard !expireIfInactive(), isOn || foreground else { return }
        let resuming = isOn
        isOn = true
        if !resuming { recordActivity() }
        if startCapture() { show(Self.offlineMessage, for: 2.5) }
    }

    private func turnOn() async {
        guard foreground else { return }
        let owner = generation
        show(nil)
        let allowed = await permission()
        guard generation == owner else { return }
        permissionAnswered(allowed)
        guard allowed, foreground else { return }
        if !ready { enableOffline(); return }
        await requestLive()
    }

    private func requestLive() async {
        let command = HubCommand(type: "set_live", payload: ["on": .bool(true)])
        request = command
        do { try await send(command) }
        catch {
            guard request?.id == command.id else { return }
            request = nil; enableOffline()
        }
    }

    private func turnOff() async {
        await stopLive(message: nil).value
    }

    private func stopLive(message: String?, notifyHub: Bool = true) -> Task<Void, Never> {
        cancelInactivity()
        request = nil
        isOn = false; audioReadyAt = nil; voiceState = .idle; show(message)
        pauseCapture()
        // The Hub closes the socket on audio after Live mode is off, so the last frame goes before this command.
        let pending = streaming, owner = generation
        return Task {
            guard generation == owner, !isOn, request == nil else { return }
            await onTurnedOff()
            guard notifyHub else { return }
            await pending?.value
            guard generation == owner, !isOn, request == nil else { return }
            try? await send(HubCommand(type: "set_live", payload: ["on": .bool(false)]))
        }
    }

    public func recordActivity() {
        guard !expireIfInactive(), isOn else { return }
        inactivityDeadline = now().advanced(by: .seconds(1800))
        armInactivityTimer()
    }

    private func armInactivityTimer() {
        inactivityTimer?.cancel()
        guard let deadline = inactivityDeadline else { return }
        let delay = max(.zero, now().duration(to: deadline))
        inactivityTimer = Task { [weak self, inactivitySleep] in
            do { try await inactivitySleep(delay) } catch { return }
            guard let self, !Task.isCancelled, self.inactivityDeadline == deadline else { return }
            if !self.expireIfInactive() { self.armInactivityTimer() }
        }
    }

    private func cancelInactivity() {
        inactivityTimer?.cancel(); inactivityTimer = nil; inactivityDeadline = nil
    }

    private func expireIfInactive() -> Bool {
        guard isOn, let deadline = inactivityDeadline, now() >= deadline else { return false }
        _ = stopLive(message: Self.inactivityMessage)
        return true
    }

    /// Answers false when the microphone wouldn't start, which turns Live mode off.
    private func startCapture() -> Bool {
        guard !expireIfInactive() else { return false }
        guard isOn, !talking, !interrupted, frames == nil else { return true }
        // Newest frames win if the socket falls behind: late audio would only distort the Mac's turn timing.
        let (stream, continuation) = AsyncStream<LiveFrame>.makeStream(bufferingPolicy: .bufferingNewest(10))
        do { try capture.start { continuation.yield(LiveFrame(pcm: $0, captured: .now)) } }
        catch {
            continuation.finish(); captureFailed()
            return false
        }
        frames = continuation
        let sendAudio = sendAudio, previous = streaming
        streaming = Task { [weak self] in
            // A paused stream may still be sending its last frame; this one goes after it.
            await previous?.value
            for await frame in stream {
                guard !Task.isCancelled else { return }
                // A blocked old write must not carry buffered outage audio onto a reconnected socket.
                guard let self, let readyAt = self.audioReadyAt, frame.captured >= readyAt,
                      frame.captured.duration(to: .now) <= .milliseconds(100) else { continue }
                self.level = pcm16Level(frame.pcm)
                await sendAudio(frame.pcm)
            }
        }
        return true
    }

    private func captureFailed() {
        guard isOn else { return }
        _ = stopLive(message: Self.microphoneFailedMessage)
        onCaptureFailure()
    }

    /// Frames captured but not yet sent are dropped: the Talk or the end of Live mode replaces them.
    private func pauseCapture() {
        capture.stop(); frames?.finish(); frames = nil
        streaming?.cancel(); level = 0
    }

    private func show(_ text: String?, for seconds: Double? = nil) {
        notice?.cancel(); notice = nil
        message = text
        guard let seconds else { return }
        notice = Task { [weak self, sleep] in
            do { try await sleep(seconds) } catch { return }
            guard let self, !Task.isCancelled else { return }
            self.message = nil; self.notice = nil
        }
    }
}

/// Captures with iOS voice processing on and plays replies through the same engine, whose output is the echo reference.
@MainActor public final class NativeLiveCaptureSource: LiveCaptureSource {
    private let session: NativeTalkAudioSession
    private var engine: AVAudioEngine?
    private var speech: NativeSpeechOutput?
    private var capturing = false
    private var frames: (@Sendable (Data) -> Void)?
    private var configurationObserver: AnyCancellable?
    private var generation = UUID()
    public var onFailure: (@MainActor () -> Void)?
    public init(session: NativeTalkAudioSession) { self.session = session }
    public var speechOutput: (any SpeechOutput)? { capturing ? speech : nil }

    public func start(frames: @escaping @Sendable (Data) -> Void) throws {
        guard !capturing else { throw HubProtocolError.invalidFrame }
        let handler = try Self.makeTapHandler(frames: frames)
        self.frames = frames
        if let engine {
            // The last capture's engine is still playing a reply, so capture resumes on it.
            speech?.whenStopped = nil
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: handler)
            capturing = true
            if !engine.isRunning { engine.prepare(); try engine.start() }
            return
        }
        try session.activateLive()
        let engine = AVAudioEngine(), owner = UUID()
        self.engine = engine; generation = owner
        configurationObserver = Self.observeConfigurationChanges(of: engine) { [weak self] in
            self?.configurationChanged(owner: owner)
        }
        do {
            try engine.inputNode.setVoiceProcessingEnabled(true)
            // Voice processing ducks other audio hard by default; the session's own duck is the level the User chose.
            engine.inputNode.voiceProcessingOtherAudioDuckingConfiguration = .init(enableAdvancedDucking: true, duckingLevel: .min)
            speech = NativeSpeechOutput(sharing: engine)
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: handler)
            engine.prepare()
            try engine.start()
        } catch {
            engine.inputNode.removeTap(onBus: 0); shutDown()
            throw error
        }
        capturing = true
    }

    // The notification's sink must not inherit the main actor: the engine calls it on its own queue.
    nonisolated static func observeConfigurationChanges(of engine: AVAudioEngine,
                                                       changed: @escaping @MainActor @Sendable () -> Void) -> AnyCancellable {
        NotificationCenter.default.publisher(for: .AVAudioEngineConfigurationChange, object: engine)
            .sink { _ in
                Task { @MainActor in changed() }
            }
    }

    private func configurationChanged(owner: UUID) {
        guard generation == owner, capturing, let engine, !engine.isRunning, let frames else { return }
        // iOS stops the engine on a hardware format change; a fresh tap also resets the PCM converter's format.
        engine.inputNode.removeTap(onBus: 0)
        do {
            let handler = try Self.makeTapHandler(frames: frames)
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: handler)
            engine.prepare()
            try engine.start()
        } catch {
            shutDown(); onFailure?()
        }
    }

    // Core Audio invokes this closure on its render thread, outside the main actor.
    nonisolated static func makeTapHandler(frames: @escaping @Sendable (Data) -> Void) throws -> AVAudioNodeTapBlock {
        let chunker = LiveFrameChunker(deliver: frames)
        let sink = try NativePCMConverter { chunker.append($0) }
        return { pcm, _ in sink.receive(pcm) }
    }

    public func stop() {
        guard capturing, let engine, let speech else { return }
        capturing = false; frames = nil
        engine.inputNode.removeTap(onBus: 0)
        // A reply playing on this engine keeps it running until the reply stops, so turning Live mode off does not cut it.
        if speech.playing { speech.whenStopped = { [weak self] in self?.shutDown() } } else { shutDown() }
    }

    private func shutDown() {
        capturing = false; frames = nil; generation = UUID()
        configurationObserver?.cancel(); configurationObserver = nil
        engine?.stop(); engine = nil; speech = nil
        session.releaseLive()
    }
}

/// Cuts converted PCM into the uplink's messages: whole 10 ms frames, at most 100 ms each, carrying the remainder.
final class LiveFrameChunker: @unchecked Sendable {
    static let frameBytes = 320
    static let maximumBytes = 3_200
    private var pending = Data()
    private let deliver: @Sendable (Data) -> Void
    init(deliver: @escaping @Sendable (Data) -> Void) { self.deliver = deliver }

    // Called only from the one render thread, so the carry needs no lock.
    func append(_ bytes: Data) {
        pending.append(bytes)
        while pending.count >= Self.frameBytes {
            let size = min(Self.maximumBytes, pending.count - pending.count % Self.frameBytes)
            deliver(Data(pending.prefix(size)))
            pending = Data(pending.dropFirst(size))
        }
    }
}
