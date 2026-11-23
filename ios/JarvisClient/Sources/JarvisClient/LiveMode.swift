import AVFoundation
import Foundation
import Observation

@MainActor public protocol LiveCaptureSource: AnyObject {
    /// `frames` runs on the audio thread with 16 kHz mono PCM16LE, each a whole number of 10 ms frames and at most 100 ms.
    func start(frames: @escaping @Sendable (Data) -> Void) throws
    func stop()
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

/// Live mode on this iPhone: the Mac listens for the Wake word on the microphone audio streamed to it.
/// Off at every launch and never saved.
@MainActor @Observable public final class LiveModeController {
    /// The Hub's voice floor as this iPhone sees it; every other Live mode iPhone sees `idle`.
    public enum VoiceState: String, Sendable { case idle, listening, thinking, speaking, followUp = "follow_up" }
    public static let onMessage = "Live mode on. Say “Jarvis” to talk."
    public static let textModeMessage = "Live mode needs Jarvis in voice mode on the Mac."
    static let offlineMessage = "Couldn't reach the Mac. Not changed."
    static let microphoneFailedMessage = "The microphone couldn't start. Try again."
    public private(set) var isOn = false
    public private(set) var message: String?
    public private(set) var voiceState = VoiceState.idle
    /// RMS of the last frame streamed, for the orb; read every frame, so it is not observed.
    @ObservationIgnored public private(set) var level = 0.0
    private var request: HubCommand?
    private var busy = false
    /// A Talk holds the microphone from its press until it is sent or discarded.
    private var talking = false
    private let capture: any LiveCaptureSource
    private let permission: @MainActor () async -> Bool
    private let sleep: @Sendable (Double) async throws -> Void
    private var send: @MainActor (HubCommand) async throws -> Void = { _ in throw HubProtocolError.notConnected }
    private var sendAudio: @Sendable (Data) async -> Void = { _ in }
    private var permissionAnswered: @MainActor (Bool) -> Void = { _ in }
    private var frames: AsyncStream<Data>.Continuation?
    private var streaming: Task<Void, Never>?
    private var notice: Task<Void, Never>?

    public init(capture: any LiveCaptureSource, permission: @escaping @MainActor () async -> Bool,
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }) {
        self.capture = capture; self.permission = permission; self.sleep = sleep
    }

    /// `send` delivers a `set_live` command whose result comes back through `receive`; `sendAudio` drops audio it cannot send.
    public func configure(send: @escaping @MainActor (HubCommand) async throws -> Void,
                          sendAudio: @escaping @Sendable (Data) async -> Void,
                          permissionAnswered: @escaping @MainActor (Bool) -> Void = { _ in }) {
        self.send = send; self.sendAudio = sendAudio; self.permissionAnswered = permissionAnswered
    }

    public func toggle() async {
        guard !busy, request == nil else { return }
        busy = true
        defer { busy = false }
        if isOn { await turnOff() } else { await turnOn() }
    }

    public func receive(_ result: CommandResult) {
        guard let request, result.id == request.id else { return }
        self.request = nil
        if result.ok, result.result?["on"] == .bool(true) {
            isOn = true
            if startCapture() { show(Self.onMessage, for: 2.5) }
        } else if result.error?.code == "voice_unavailable" { show(Self.textModeMessage) }
        else if !result.ok { show(Self.offlineMessage) }
    }

    public func receive(voiceState event: HubEvent) {
        guard isOn, let payload = try? event.decodePayload(VoiceStatePayload.self) else { return }
        voiceState = VoiceState(rawValue: payload.state) ?? .idle
    }

    public func dropped(commandID: String) {
        guard request?.id == commandID else { return }
        request = nil; show(Self.offlineMessage)
    }

    /// Pauses streaming while a Talk holds the microphone and resumes it once the Talk is sent or discarded.
    public func talkHoldsMicrophone(_ held: Bool) {
        guard talking != held else { return }
        talking = held
        if held { pauseCapture() } else { _ = startCapture() }
    }

    private func turnOn() async {
        show(nil)
        let allowed = await permission()
        permissionAnswered(allowed)
        guard allowed else { return }
        let command = HubCommand(type: "set_live", payload: ["on": .bool(true)])
        request = command
        do { try await send(command) }
        catch {
            guard request?.id == command.id else { return }
            request = nil; show(Self.offlineMessage)
        }
    }

    private func turnOff() async {
        isOn = false; voiceState = .idle; show(nil)
        pauseCapture()
        // The Hub closes the socket on audio after Live mode is off, so the last frame goes before this command.
        await streaming?.value
        try? await send(HubCommand(type: "set_live", payload: ["on": .bool(false)]))
    }

    /// Answers false when the microphone wouldn't start, which turns Live mode off.
    private func startCapture() -> Bool {
        guard isOn, !talking, frames == nil else { return true }
        // Newest frames win if the socket falls behind: late audio would only distort the Mac's turn timing.
        let (stream, continuation) = AsyncStream<Data>.makeStream(bufferingPolicy: .bufferingNewest(10))
        do { try capture.start { continuation.yield($0) } }
        catch {
            continuation.finish(); isOn = false; voiceState = .idle; show(Self.microphoneFailedMessage)
            Task { try? await send(HubCommand(type: "set_live", payload: ["on": .bool(false)])) }
            return false
        }
        frames = continuation
        let sendAudio = sendAudio, previous = streaming
        streaming = Task { [weak self] in
            // A paused stream may still be sending its last frame; this one goes after it.
            await previous?.value
            for await frame in stream {
                self?.level = Self.rms(frame)
                await sendAudio(frame)
            }
        }
        return true
    }

    /// Frames captured but not yet sent are dropped: the Talk or the end of Live mode replaces them.
    private func pauseCapture() {
        capture.stop(); frames?.finish(); frames = nil
        streaming?.cancel(); level = 0
    }

    nonisolated static func rms(_ pcm: Data) -> Double {
        let samples = pcm.count / 2
        guard samples > 0 else { return 0 }
        let energy = pcm.withUnsafeBytes { raw in
            (0..<samples).reduce(0.0) { sum, index in
                let sample = Double(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: index * 2, as: Int16.self))) / 32_768
                return sum + sample * sample
            }
        }
        return (energy / Double(samples)).squareRoot()
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
    public init(session: NativeTalkAudioSession) { self.session = session }
    public var speechOutput: (any SpeechOutput)? { capturing ? speech : nil }

    public func start(frames: @escaping @Sendable (Data) -> Void) throws {
        guard !capturing else { throw HubProtocolError.invalidFrame }
        let handler = try Self.makeTapHandler(frames: frames)
        if let engine {
            // The last capture's engine is still playing a reply, so capture resumes on it.
            speech?.whenStopped = nil
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: handler)
            capturing = true; return
        }
        try session.activateLive()
        let engine = AVAudioEngine()
        do {
            try engine.inputNode.setVoiceProcessingEnabled(true)
            speech = NativeSpeechOutput(sharing: engine)
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: handler)
            engine.prepare()
            try engine.start()
        } catch {
            engine.inputNode.removeTap(onBus: 0); engine.stop(); speech = nil; session.releaseLive()
            throw error
        }
        self.engine = engine; capturing = true
    }

    // Core Audio invokes this closure on its render thread, outside the main actor.
    nonisolated static func makeTapHandler(frames: @escaping @Sendable (Data) -> Void) throws -> AVAudioNodeTapBlock {
        let chunker = LiveFrameChunker(deliver: frames)
        let sink = try NativePCMConverter { chunker.append($0) }
        return { pcm, _ in sink.receive(pcm) }
    }

    public func stop() {
        guard capturing, let engine, let speech else { return }
        capturing = false
        engine.inputNode.removeTap(onBus: 0)
        // A reply playing on this engine keeps it running until the reply stops, so turning Live mode off does not cut it.
        if speech.playing { speech.whenStopped = { [weak self] in self?.shutDown() } } else { shutDown() }
    }

    private func shutDown() {
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
