import AVFoundation
import Foundation

@MainActor public protocol TalkAudioSession: AnyObject {
    func activate() throws
    func release()
    func activatePlayback() throws
    func releasePlayback()
}

public extension TalkAudioSession {
    func activatePlayback() throws { try activate() }
    func releasePlayback() { release() }
}

@MainActor public final class NativeTalkAudioSession: TalkAudioSession {
    private enum Owner { case recording, playback }
    private var owner: Owner?
    public init() {}
    public func activate() throws {
        guard owner == nil || owner == .recording else { throw HubProtocolError.invalidFrame }
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try session.setActive(true)
        #endif
        owner = .recording
    }
    public func activatePlayback() throws {
        if owner == .playback { return }
        guard owner == nil || owner == .playback else { throw HubProtocolError.invalidFrame }
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .spokenAudio, options: [])
        try session.setActive(true)
        #endif
        owner = .playback
    }
    public func releasePlayback() {
        guard owner == .playback else { return }
        deactivate()
    }
    public func release() {
        guard owner == .recording else { return }
        deactivate()
    }
    private func deactivate() {
        owner = nil
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }
}

/// The frame bound is independent of the UI timer and of scheduling delays on the main actor.
public final class TalkPCMBuffer: @unchecked Sendable {
    public static let maximumBytes = 9_600_000
    private let lock = NSLock()
    private var data = Data()
    private var closed = false
    public init() {}
    @discardableResult public func append(_ bytes: Data) -> Bool {
        lock.withLock {
            guard !closed else { return false }
            let count = min(Self.maximumBytes - data.count, bytes.count - bytes.count % 2)
            data.append(bytes.prefix(count))
            if data.count == Self.maximumBytes { closed = true; return true }
            return false
        }
    }
    public func finish() -> Data { lock.withLock { closed = true; defer { data = Data() }; return data } }
}

@MainActor public protocol TalkCaptureSource: AnyObject {
    func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws
    func stop()
}

@MainActor public final class NativeTalkRecorder: TalkRecorder {
    private let session: any TalkAudioSession
    private let source: any TalkCaptureSource
    private var capture: TalkPCMBuffer?
    public init(session: any TalkAudioSession = NativeTalkAudioSession(), source: any TalkCaptureSource = NativeTalkCaptureSource()) {
        self.session = session; self.source = source
    }
    public static func requestPermission() async -> Bool {
        #if os(iOS)
        return await AVAudioApplication.requestRecordPermission()
        #else
        return false
        #endif
    }
    public func start(limitReached: @escaping @MainActor @Sendable () -> Void) throws {
        guard capture == nil else { throw HubProtocolError.invalidFrame }
        let buffer = TalkPCMBuffer()
        do {
            try session.activate()
            try source.start(buffer: buffer, limitReached: limitReached)
            capture = buffer
        } catch { source.stop(); session.release(); throw error }
    }
    public func stop() -> Data {
        guard let capture else { return Data() }
        self.capture = nil
        source.stop()
        let bytes = capture.finish()
        session.release()
        return bytes
    }
}

@MainActor public final class NativeTalkCaptureSource: TalkCaptureSource {
    private var engine: AVAudioEngine?
    public init() {}
    public func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0,
              let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: format, to: target) else { throw HubProtocolError.invalidFrame }
        let sink = NativePCMConverter(converter: converter, target: target, buffer: buffer, limitReached: limitReached)
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { pcm, _ in sink.receive(pcm) }
        self.engine = engine
        engine.prepare()
        try engine.start()
    }
    public func stop() {
        guard let engine else { return }
        self.engine = nil
        engine.inputNode.removeTap(onBus: 0); engine.stop()
    }
}

private final class NativePCMConverter: @unchecked Sendable {
    let converter: AVAudioConverter
    let target: AVAudioFormat
    let buffer: TalkPCMBuffer
    let limitReached: @MainActor @Sendable () -> Void
    init(converter: AVAudioConverter, target: AVAudioFormat, buffer: TalkPCMBuffer,
         limitReached: @escaping @MainActor @Sendable () -> Void) {
        self.converter = converter; self.target = target; self.buffer = buffer; self.limitReached = limitReached
    }
    func receive(_ pcm: AVAudioPCMBuffer) {
        let capacity = AVAudioFrameCount(ceil(Double(pcm.frameLength) * 16_000 / pcm.format.sampleRate) + 16)
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true; status.pointee = .haveData; return pcm
        }
        guard error == nil, let samples = output.int16ChannelData?[0] else { return }
        let bytes = Data(bytes: samples, count: Int(output.frameLength) * 2)
        if buffer.append(bytes) { Task { @MainActor in limitReached() } }
    }
}
