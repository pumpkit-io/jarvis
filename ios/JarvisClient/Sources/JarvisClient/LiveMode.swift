import AVFoundation
import Foundation
import Observation

@MainActor public protocol LiveCaptureSource: AnyObject {
    /// `frames` runs on the audio thread with 16 kHz mono PCM16LE, each a whole number of 10 ms frames and at most 100 ms.
    func start(frames: @escaping @Sendable (Data) -> Void) throws
    func stop()
}

/// Live mode on this iPhone: the Mac listens for the Wake word on the microphone audio streamed to it.
/// Off at every launch and never saved.
@MainActor @Observable public final class LiveModeController {
    public static let onMessage = "Live mode on. Say “Jarvis” to talk."
    public static let textModeMessage = "Live mode needs Jarvis in voice mode on the Mac."
    static let offlineMessage = "Couldn't reach the Mac. Not changed."
    static let microphoneFailedMessage = "The microphone couldn't start. Try again."
    public private(set) var isOn = false
    public private(set) var message: String?
    private var request: HubCommand?
    private var busy = false
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
        if result.ok, result.result?["on"] == .bool(true) { startStreaming() }
        else if result.error?.code == "voice_unavailable" { show(Self.textModeMessage) }
    }

    public func dropped(commandID: String) {
        guard request?.id == commandID else { return }
        request = nil; show(Self.offlineMessage)
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
        isOn = false; show(nil)
        await stopStreaming()
        // The Hub closes the socket on audio after Live mode is off, so the last frame goes before this command.
        try? await send(HubCommand(type: "set_live", payload: ["on": .bool(false)]))
    }

    private func startStreaming() {
        // Newest frames win if the socket falls behind: late audio would only distort the Mac's turn timing.
        let (stream, continuation) = AsyncStream<Data>.makeStream(bufferingPolicy: .bufferingNewest(10))
        do { try capture.start { continuation.yield($0) } }
        catch {
            continuation.finish(); show(Self.microphoneFailedMessage)
            Task { try? await send(HubCommand(type: "set_live", payload: ["on": .bool(false)])) }
            return
        }
        frames = continuation; isOn = true
        let sendAudio = sendAudio
        streaming = Task { for await frame in stream { await sendAudio(frame) } }
        show(Self.onMessage, for: 2.5)
    }

    private func stopStreaming() async {
        capture.stop(); frames?.finish(); frames = nil
        let streaming = streaming; self.streaming = nil
        streaming?.cancel(); await streaming?.value
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

/// Captures with iOS voice processing on, so Jarvis's own voice from the speaker is cancelled before the Mac hears it.
@MainActor public final class NativeLiveCaptureSource: LiveCaptureSource {
    private let session: NativeTalkAudioSession
    private var engine: AVAudioEngine?
    public init(session: NativeTalkAudioSession) { self.session = session }

    public func start(frames: @escaping @Sendable (Data) -> Void) throws {
        guard engine == nil else { throw HubProtocolError.invalidFrame }
        let handler = try Self.makeTapHandler(frames: frames)
        try session.activateLive()
        let engine = AVAudioEngine()
        do {
            try engine.inputNode.setVoiceProcessingEnabled(true)
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: handler)
            engine.prepare()
            try engine.start()
        } catch {
            engine.inputNode.removeTap(onBus: 0); engine.stop(); session.releaseLive()
            throw error
        }
        self.engine = engine
    }

    // Core Audio invokes this closure on its render thread, outside the main actor.
    nonisolated static func makeTapHandler(frames: @escaping @Sendable (Data) -> Void) throws -> AVAudioNodeTapBlock {
        let chunker = LiveFrameChunker(deliver: frames)
        let sink = try NativePCMConverter { chunker.append($0) }
        return { pcm, _ in sink.receive(pcm) }
    }

    public func stop() {
        guard let engine else { return }
        self.engine = nil
        engine.inputNode.removeTap(onBus: 0); engine.stop()
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
