import AVFoundation
import Foundation
import os

@MainActor public protocol TalkAudioSession: AnyObject {
    /// Claims the session for a Talk and returns at once. On the audio queue it activates the session and then runs `start`; if either throws, it deactivates there and `failed` runs on the main actor.
    func beginRecording(_ start: @escaping @Sendable () throws -> Void, failed: @escaping @MainActor @Sendable () -> Void) throws
    /// Ends the Talk's claim and returns at once. On the audio queue it runs `stop` and then deactivates.
    func endRecording(_ stop: @escaping @Sendable () -> Void)
    func activatePlayback() throws
    func releasePlayback()
}

/// The audio session setups the app uses; each maps to one category, mode and option set.
enum AudioSessionSetup: Equatable, Sendable { case recording, playback }

/// The seam to `AVAudioSession`, so tests can check which calls a press, a release and a reply make and where they run.
/// Every call runs on `NativeTalkAudioSession`'s audio queue, never on the main thread.
protocol AudioSessionDriver: Sendable {
    /// Sets `setup` and then activates the session, which starts the duck.
    func activate(_ setup: AudioSessionSetup) throws
    /// Deactivates with `notifyOthersOnDeactivation`, which ends the duck and lets an app the session interrupted resume.
    func deactivate()
}

/// Timings of the Talk audio path, for diagnosing a stall on a device in Console or Instruments (ADR 0033).
enum TalkAudioTrace {
    static let log = Logger(subsystem: "io.pumpkit.jarvis", category: "TalkAudio")
    static let signposter = OSSignposter(logger: log)
    static func milliseconds(since start: ContinuousClock.Instant) -> Int {
        let elapsed = ContinuousClock.now - start
        return Int(elapsed.components.seconds * 1_000 + elapsed.components.attoseconds / 1_000_000_000_000_000)
    }
}

final class NativeAudioSessionDriver: AudioSessionDriver {
    init() {
        #if os(iOS)
        TalkAudioTrace.observeSession()
        #endif
    }
    func activate(_ setup: AudioSessionSetup) throws {
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        TalkAudioTrace.log.notice("activate \(String(describing: setup), privacy: .public) from: \(TalkAudioTrace.describe(session), privacy: .public)")
        let started = ContinuousClock.now
        let interval = TalkAudioTrace.signposter.beginInterval("activate")
        defer { TalkAudioTrace.signposter.endInterval("activate", interval) }
        let rate = session.sampleRate, buffer = session.ioBufferDuration
        switch setup {
        case .recording: try session.setCategory(.playAndRecord, mode: .default, options: NativeTalkAudioSession.recordingOptions)
        case .playback: try session.setCategory(.playback, mode: .spokenAudio, options: NativeTalkAudioSession.playbackOptions)
        }
        // Asks for the format the hardware already runs, so joining it does not restart other apps' audio at a new one (ADR 0033).
        if rate > 0 { try? session.setPreferredSampleRate(rate) }
        if buffer > 0 { try? session.setPreferredIOBufferDuration(buffer) }
        try session.setActive(true)
        TalkAudioTrace.log.notice("""
            activate \(String(describing: setup), privacy: .public) took \(TalkAudioTrace.milliseconds(since: started)) ms; \
            now: \(TalkAudioTrace.describe(session), privacy: .public)
            """)
        #endif
    }
    func deactivate() {
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        TalkAudioTrace.log.notice("deactivate from: \(TalkAudioTrace.describe(session), privacy: .public)")
        let started = ContinuousClock.now
        let interval = TalkAudioTrace.signposter.beginInterval("deactivate")
        try? session.setActive(false, options: .notifyOthersOnDeactivation)
        TalkAudioTrace.signposter.endInterval("deactivate", interval)
        TalkAudioTrace.log.notice("deactivate took \(TalkAudioTrace.milliseconds(since: started)) ms; now: \(TalkAudioTrace.describe(session), privacy: .public)")
        #endif
    }
}

#if os(iOS)
extension TalkAudioTrace {
    /// Category, route and hardware format of the shared session; ports are logged by type only, since a port name can carry the owner's name.
    static func describe(_ session: AVAudioSession) -> String {
        let route = session.currentRoute
        func ports(_ list: [AVAudioSessionPortDescription]) -> String { list.map(\.portType.rawValue).joined(separator: ",") }
        return "\(session.category.rawValue)/\(session.mode.rawValue) options 0x\(String(session.categoryOptions.rawValue, radix: 16)); "
            + "out [\(ports(route.outputs))] in [\(ports(route.inputs))]; \(session.sampleRate) Hz, "
            + "IO \(session.ioBufferDuration * 1_000) ms, \(session.outputNumberOfChannels) out channels; "
            + "other audio \(session.isOtherAudioPlaying ? "playing" : "silent")"
    }

    static func describe(_ reason: AVAudioSession.RouteChangeReason) -> String {
        switch reason {
        case .newDeviceAvailable: "newDeviceAvailable"
        case .oldDeviceUnavailable: "oldDeviceUnavailable"
        case .categoryChange: "categoryChange"
        case .override: "override"
        case .wakeFromSleep: "wakeFromSleep"
        case .noSuitableRouteForCategory: "noSuitableRouteForCategory"
        case .routeConfigurationChange: "routeConfigurationChange"
        default: "unknown(\(reason.rawValue))"
        }
    }

    private static let observers: Void = {
        let center = NotificationCenter.default
        _ = center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: nil) { note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt ?? 0
            let reason = describe(AVAudioSession.RouteChangeReason(rawValue: raw) ?? .unknown)
            let previous = note.userInfo?[AVAudioSessionRouteChangePreviousRouteKey] as? AVAudioSessionRouteDescription
            let before = previous.map { "out [\($0.outputs.map(\.portType.rawValue).joined(separator: ","))] in [\($0.inputs.map(\.portType.rawValue).joined(separator: ","))]" } ?? "none"
            log.notice("route change \(reason, privacy: .public) from \(before, privacy: .public) to: \(describe(AVAudioSession.sharedInstance()), privacy: .public)")
        }
        _ = center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: nil) { note in
            let type = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let reason = note.userInfo?[AVAudioSessionInterruptionReasonKey] as? UInt
            let options = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt
            log.notice("interruption type \(type ?? 99) reason \(reason ?? 99) options \(options ?? 0)")
        }
        // Sent only while this app is in the foreground with an active session: another app's primary audio starting (1) or stopping (0).
        _ = center.addObserver(forName: AVAudioSession.silenceSecondaryAudioHintNotification, object: nil, queue: nil) { note in
            let type = note.userInfo?[AVAudioSessionSilenceSecondaryAudioHintTypeKey] as? UInt
            log.notice("other app's audio \(type == 1 ? "started" : "stopped", privacy: .public)")
        }
        _ = center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: nil) { _ in
            log.error("media services were reset")
        }
    }()

    /// Logs the session's route changes, interruptions and media resets for the app's lifetime, so a gap can be lined up with them in Console.
    static func observeSession() { _ = observers }
}
#endif

/// Whether the session is active, known only on the audio queue, where every driver call runs.
private final class AudioHardware: @unchecked Sendable {
    private let driver: any AudioSessionDriver
    private var active = false
    init(driver: any AudioSessionDriver) { self.driver = driver }
    func activate(_ setup: AudioSessionSetup) throws {
        try driver.activate(setup)
        active = true
    }
    /// A Talk released before the queue reached its start never activated, so there is nothing to deactivate.
    func deactivate() {
        guard active else { return }
        active = false
        driver.deactivate()
    }
}

/// One Talk's hold on the session; a release on the main actor can end it before the audio queue reaches its start.
private final class RecordingClaim: @unchecked Sendable {
    private let lock = NSLock()
    private var isEnded = false
    /// Read and written only on the audio queue: whether it ran the Talk's start, so its stop has something to stop.
    var started = false
    var ended: Bool { lock.withLock { isEnded } }
    func end() { lock.withLock { isEnded = true } }
}

/// Activates the session for each use and deactivates it after, because iOS ducks other audio only from activation to deactivation (ADR 0033).
/// Session and microphone calls wait for the audio hardware, so they run on a serial queue off the main thread; playback and Live mode wait on it, so no call overtakes another.
@MainActor public final class NativeTalkAudioSession: TalkAudioSession {
    private enum Owner { case recording, playback }
    private var owner: Owner?
    private var claim: RecordingClaim?
    /// While Live mode captures, Talks and replies use its active play-and-record session instead of their own.
    private var live = false
    private let hardware: AudioHardware
    private let queue = DispatchQueue(label: "io.pumpkit.jarvis.audio-session", qos: .userInitiated)
    #if os(iOS)
    /// Music ducks instead of stopping, AirPods stay in A2DP, and the iPhone's microphone records (ADR 0032).
    public nonisolated static let recordingOptions: AVAudioSession.CategoryOptions = [.mixWithOthers, .duckOthers, .defaultToSpeaker, .allowBluetoothA2DP]
    /// Music ducks under speech and a podcast pauses; music returns when the session deactivates, and the podcast resumes.
    public nonisolated static let playbackOptions: AVAudioSession.CategoryOptions = [.mixWithOthers, .duckOthers, .interruptSpokenAudioAndMixWithOthers]
    #endif
    public init() { hardware = AudioHardware(driver: NativeAudioSessionDriver()) }
    init(driver: any AudioSessionDriver) { hardware = AudioHardware(driver: driver) }

    /// Waits until the audio queue has run everything given to it so far.
    func drain() { queue.sync {} }

    public func activateLive() throws {
        guard owner != .recording else { throw HubProtocolError.invalidFrame }
        try queue.sync { [hardware] in try hardware.activate(.recording) }
        live = true
    }
    public func releaseLive() {
        guard live else { return }
        live = false
        if owner == nil { deactivate() }
    }
    public func beginRecording(_ start: @escaping @Sendable () throws -> Void, failed: @escaping @MainActor @Sendable () -> Void) throws {
        guard owner == nil else { throw HubProtocolError.invalidFrame }
        let claim = RecordingClaim(), activates = !live, hardware = hardware
        owner = .recording; self.claim = claim
        let pressed = ContinuousClock.now
        queue.async { [weak self] in
            guard !claim.ended else { return }
            claim.started = true
            TalkAudioTrace.log.notice("Talk start waited \(TalkAudioTrace.milliseconds(since: pressed)) ms for the audio queue")
            do {
                if activates { try hardware.activate(.recording) }
                try start()
            } catch {
                if activates { hardware.deactivate() }
                Task { @MainActor in
                    guard let self, self.claim === claim else { return }
                    self.claim = nil; self.owner = nil
                    failed()
                }
            }
        }
    }
    public func endRecording(_ stop: @escaping @Sendable () -> Void) {
        guard owner == .recording, let claim else { return }
        claim.end()
        self.claim = nil; owner = nil
        TalkAudioTrace.log.notice("Talk released or cancelled")
        let deactivates = !live, hardware = hardware
        queue.async {
            guard claim.started else { return }
            stop()
            if deactivates { hardware.deactivate() }
        }
    }
    public func activatePlayback() throws {
        if owner == .playback { return }
        guard owner == nil else { throw HubProtocolError.invalidFrame }
        // Speech output starts on the main actor right after this, so it waits for the activation and for any Talk still stopping.
        if !live { try queue.sync { [hardware] in try hardware.activate(.playback) } }
        owner = .playback
    }
    public func releasePlayback() {
        guard owner == .playback else { return }
        owner = nil
        if !live { deactivate() }
    }
    private func deactivate() {
        queue.async { [hardware] in hardware.deactivate() }
    }
}

func pcm16Level(_ pcm: Data) -> Double {
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

/// The frame bound is independent of the UI timer and of scheduling delays on the main actor.
public final class TalkPCMBuffer: @unchecked Sendable {
    public static let maximumBytes = 9_600_000
    private let lock = NSLock()
    private var data = Data()
    private var closed = false
    private var currentLevel = 0.0
    public var level: Double { lock.withLock { currentLevel } }
    public init() {}
    @discardableResult public func append(_ bytes: Data) -> Bool {
        lock.withLock {
            guard !closed else { return false }
            let count = min(Self.maximumBytes - data.count, bytes.count - bytes.count % 2)
            data.append(bytes.prefix(count))
            if count > 0 { currentLevel = pcm16Level(bytes.prefix(count)) }
            if data.count == Self.maximumBytes { closed = true; return true }
            return false
        }
    }
    public func finish() -> Data { lock.withLock { closed = true; currentLevel = 0; defer { data = Data() }; return data } }
}

/// Runs only on the audio queue, never on the main thread.
public protocol TalkCaptureSource: AnyObject, Sendable {
    func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws
    func stop()
}

@MainActor public final class NativeTalkRecorder: TalkRecorder {
    private let session: any TalkAudioSession
    private let source: any TalkCaptureSource
    private var capture: TalkPCMBuffer?
    public var level: Double { capture?.level ?? 0 }
    public init(session: any TalkAudioSession = NativeTalkAudioSession(), source: any TalkCaptureSource = NativeTalkCaptureSource()) {
        self.session = session; self.source = source
    }
    public static func requestPermission() async -> Bool {
        #if os(iOS)
        if AVAudioApplication.shared.recordPermission == .granted { return true }
        return await AVAudioApplication.requestRecordPermission()
        #else
        return false
        #endif
    }
    /// Returns before the microphone is on; the audio queue turns it on, and `failed` runs if it could not.
    public func start(limitReached: @escaping @MainActor @Sendable () -> Void, failed: @escaping @MainActor @Sendable () -> Void) throws {
        guard capture == nil else { throw HubProtocolError.invalidFrame }
        let buffer = TalkPCMBuffer(), source = source
        capture = buffer
        do {
            try session.beginRecording({
                do { try source.start(buffer: buffer, limitReached: limitReached) } catch { source.stop(); throw error }
            }, failed: { [weak self] in
                guard let self, self.capture === buffer else { return }
                self.capture = nil
                _ = buffer.finish()
                failed()
            })
        } catch { capture = nil; throw error }
    }
    /// Returns what was recorded at once; the microphone stops and the session deactivates on the audio queue after.
    public func stop() -> Data {
        guard let capture else { return Data() }
        self.capture = nil
        let bytes = capture.finish(), source = source
        session.endRecording { source.stop() }
        return bytes
    }
}

/// Its engine is created, started and stopped only on the audio queue.
public final class NativeTalkCaptureSource: TalkCaptureSource, @unchecked Sendable {
    private var engine: AVAudioEngine?
    private var configurationObserver: (any NSObjectProtocol)?
    public init() {}
    public func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws {
        let started = ContinuousClock.now
        let interval = TalkAudioTrace.signposter.beginInterval("microphone start")
        defer { TalkAudioTrace.signposter.endInterval("microphone start", interval) }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let handler = try Self.makeTapHandler(buffer: buffer, limitReached: limitReached, started: started)
        input.installTap(onBus: 0, bufferSize: 2048, format: nil, block: handler)
        self.engine = engine
        // A configuration change means the hardware changed under a running microphone, a candidate for a music gap.
        configurationObserver = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in
            TalkAudioTrace.log.notice("microphone engine configuration changed")
        }
        engine.prepare()
        try engine.start()
        let format = input.inputFormat(forBus: 0)
        TalkAudioTrace.log.notice("""
            microphone engine started in \(TalkAudioTrace.milliseconds(since: started)) ms; input \(format.sampleRate) Hz \
            \(format.channelCount) channels, voice processing \(input.isVoiceProcessingEnabled ? "on" : "off", privacy: .public)
            """)
    }
    // Core Audio invokes this closure on its render thread, outside the main actor.
    static func makeTapHandler(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void,
                               started: ContinuousClock.Instant = .now) throws -> AVAudioNodeTapBlock {
        let sink = try NativePCMConverter { bytes in
            if buffer.append(bytes) { Task { @MainActor in limitReached() } }
        }
        let first = FirstBufferTrace(started: started)
        return { pcm, _ in first.mark(); sink.receive(pcm) }
    }
    public func stop() {
        if let configurationObserver { NotificationCenter.default.removeObserver(configurationObserver) }
        configurationObserver = nil
        guard let engine else { return }
        self.engine = nil
        let started = ContinuousClock.now
        engine.inputNode.removeTap(onBus: 0); engine.stop()
        TalkAudioTrace.log.notice("microphone engine stopped in \(TalkAudioTrace.milliseconds(since: started)) ms")
    }
}

/// Logs once how long after the microphone start the first buffer arrived; the render thread calls it, one call at a time.
private final class FirstBufferTrace: @unchecked Sendable {
    private let started: ContinuousClock.Instant
    private var marked = false
    init(started: ContinuousClock.Instant) { self.started = started }
    func mark() {
        guard !marked else { return }
        marked = true
        TalkAudioTrace.log.notice("first microphone buffer \(TalkAudioTrace.milliseconds(since: self.started)) ms after the microphone start")
    }
}

/// Converts the microphone's hardware format to 16 kHz mono PCM16LE on the render thread.
final class NativePCMConverter: @unchecked Sendable {
    private var converter: AVAudioConverter?
    let target: AVAudioFormat
    let deliver: @Sendable (Data) -> Void
    init(deliver: @escaping @Sendable (Data) -> Void) throws {
        guard let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                         channels: 1, interleaved: true) else { throw HubProtocolError.invalidFrame }
        self.target = target; self.deliver = deliver
    }
    func receive(_ pcm: AVAudioPCMBuffer) {
        if converter == nil {
            guard pcm.format.sampleRate > 0, pcm.format.channelCount > 0 else { return }
            converter = AVAudioConverter(from: pcm.format, to: target)
        }
        guard let converter else { return }
        let capacity = AVAudioFrameCount(ceil(Double(pcm.frameLength) * 16_000 / pcm.format.sampleRate) + 16)
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true; status.pointee = .haveData; return pcm
        }
        guard error == nil, let samples = output.int16ChannelData?[0] else { return }
        deliver(Data(bytes: samples, count: Int(output.frameLength) * 2))
    }
}
