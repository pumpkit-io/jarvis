import Foundation
import Observation

@MainActor public protocol TalkRecorder: AnyObject {
    var level: Double { get }
    /// May return before the microphone is on; `failed` runs if it then could not start.
    func start(limitReached: @escaping @MainActor @Sendable () -> Void, failed: @escaping @MainActor @Sendable () -> Void) throws
    func stop() -> Data
}

public enum TalkOutcome: Equatable, Sendable { case tooShort, discarded }

public enum TalkState: Equatable, Sendable {
    case idle, authorizing, recording, paused, refused, failed, cancelled
}

/// Loudness bars for showing a recorded Talk, each between 0 and 1 relative to the loudest.
public enum TalkWaveform {
    public static func levels(_ pcm: Data, bars: Int) -> [Double] {
        guard bars > 0 else { return [] }
        let samples = pcm.count / 2
        guard samples > 0 else { return Array(repeating: 0, count: bars) }
        let rms = pcm.withUnsafeBytes { raw in
            (0..<bars).map { bar -> Double in
                let lower = bar * samples / bars, upper = max(lower + 1, (bar + 1) * samples / bars)
                var sum = 0.0
                for index in lower..<min(upper, samples) {
                    let value = Double(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: index * 2, as: Int16.self))) / 32_768
                    sum += value * value
                }
                return (sum / Double(upper - lower)).squareRoot()
            }
        }
        let peak = rms.max() ?? 0
        return peak > 0 ? rms.map { $0 / peak } : rms
    }
}

/// Records a Talk and hands it to the Outbox. Nothing here waits on the connection.
@MainActor @Observable public final class TalkController {
    public private(set) var state: TalkState = .idle {
        didSet { if capturing != Self.capturing(oldValue) { microphoneHeld(capturing) } }
    }
    /// Told true at a press, before the microphone opens, and false once the Talk is sent or discarded.
    @ObservationIgnored public var microphoneHeld: @MainActor (Bool) -> Void = { _ in }
    public private(set) var elapsed = 0
    public private(set) var talkID: String?
    public private(set) var microphoneRefused = false
    public private(set) var outcome: TalkOutcome?
    /// Counts the User's own discards; lifecycle retirements don't count.
    public private(set) var discards = 0
    private var confirmedDiscards = 0
    private var interaction: UUID?
    private var touch: UUID?
    private var touchCapture: UUID?
    private var captureControl: UUID?
    private var pendingPress: Task<Void, Never>?
    public private(set) var isLocked = false
    public private(set) var lockProgress = 0.0
    public private(set) var discardProgress = 0.0
    public static let lockDistance = 60.0
    public static let discardDistance = 120.0
    public private(set) var holdCancelled = false
    /// The audio of the segments recorded before a pause; the segment being recorded joins it on send.
    public private(set) var recorded = Data()
    private var recordedSeconds = 0.0
    /// Seconds recorded so far, without the time spent paused.
    public var duration: Double { recordedSeconds + (state == .recording ? max(0, now() - started) : 0) }
    public var speaking = false
    public var level: Double { state == .recording ? recorder.level : 0 }
    public var label: String { label(for: state) }
    public func label(for state: TalkState) -> String {
        switch state {
        case .recording: isLocked ? "Send Talk" : "Release to send. Slide up to lock or left to cancel."
        case .paused: "Send Talk"
        case .authorizing: "Allow the microphone"
        default: speaking ? "Hold to interrupt" : "Hold to talk"
        }
    }
    public var message: String? {
        if microphoneRefused, state == .idle || state == .cancelled || state == .refused {
            return "Jarvis needs the microphone to hear you. Allow it in Settings."
        }
        return switch state {
        case .refused: "Jarvis needs the microphone to hear you. Allow it in Settings."
        case .failed: "The microphone couldn't start. Try again."
        default: nil
        }
    }
    /// Live mode asks for the same microphone, so its answer drives this permission message too.
    public func microphonePermission(granted: Bool) { microphoneRefused = !granted }
    public var canPress: Bool { state != .authorizing && state != .recording && state != .paused }
    private var capturing: Bool { Self.capturing(state) }
    private static func capturing(_ state: TalkState) -> Bool { state == .authorizing || state == .recording || state == .paused }
    /// Audio shorter than 0.3 seconds is a slip of the finger, not something said.
    static let shortestBytes = TalkAudio.bytesPerSecond * 3 / 10
    private let recorder: any TalkRecorder
    private let permission: @MainActor () async -> Bool
    private let now: @MainActor () -> Double
    private let clock: @MainActor () -> Date
    private let sleep: @Sendable (Double) async throws -> Void
    private var begin: @MainActor (String) async -> Bool = { _ in false }
    private var cancelBegun: @MainActor (String) async -> Void = { _ in }
    private var deliver: (@MainActor (String, Data, Date) -> Void)?
    private var stopSpeech: @MainActor () async -> Void = {}
    private var generation: UInt64 = 0
    private var beginning: Task<Bool, Never>?
    private var started = 0.0
    private var timer: Task<Void, Never>?

    public init(recorder: any TalkRecorder, permission: @escaping @MainActor () async -> Bool,
                now: @escaping @MainActor () -> Double = { ProcessInfo.processInfo.systemUptime },
                clock: @escaping @MainActor () -> Date = { Date() },
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }) {
        self.recorder = recorder; self.permission = permission; self.now = now; self.clock = clock; self.sleep = sleep
    }

    /// `begin` tells a connected Hub that the User is speaking and answers whether it was sent; `deliver` queues the finished Talk.
    public func configure(begin: @escaping @MainActor (String) async -> Bool,
                          cancel: @escaping @MainActor (String) async -> Void,
                          deliver: @escaping @MainActor (String, Data, Date) -> Void,
                          stopSpeech: @escaping @MainActor () async -> Void = {}) {
        self.begin = begin; cancelBegun = cancel; self.deliver = deliver; self.stopSpeech = stopSpeech
    }

    public func holdBegan(interaction id: UUID) {
        guard touch == nil else { return }
        touch = id; holdCancelled = false
        guard canPress else { holdCancelled = true; return }
        let capture = UUID(); touchCapture = capture
        startInteraction(capture, control: id)
    }

    @discardableResult public func holdMoved(horizontalTranslation: Double = 0, verticalTranslation: Double, interaction id: UUID) -> Task<Void, Never> {
        guard touch == id, !holdCancelled, !isLocked else { return Task {} }
        lockProgress = min(1, max(0, -verticalTranslation / Self.lockDistance))
        discardProgress = min(1, max(0, -horizontalTranslation / Self.discardDistance))
        if horizontalTranslation < -Self.discardDistance {
            holdCancelled = true
            if capturing || pendingPress != nil { discards += 1 }
            return cancelTouchCapture()
        }
        // Locked controls replace the held mic, so the touch no longer owns capture.
        if state == .recording, verticalTranslation < -Self.lockDistance { isLocked = true; touch = nil }
        return Task {}
    }

    public func send() async { await release() }
    public func discard() async {
        guard capturing else { return }
        discards += 1
        await cancel()
    }

    /// The accessibility Cancel Talk action: a discard, retiring a press that hasn't started too.
    public func cancel(interaction id: UUID) async {
        guard capturing || pendingPress != nil else { return }
        discards += 1
        await controlDisappeared(interaction: id)
    }

    /// Answers true once per discard, so only one of the screens' input bars confirms it.
    public func takeDiscardConfirmation() -> Bool {
        guard confirmedDiscards < discards else { return false }
        confirmedDiscards = discards
        return true
    }

    @discardableResult public func holdEnded(interaction id: UUID) -> Task<Void, Never> {
        guard touch == id else { return Task {} }
        touch = nil
        guard !holdCancelled, !isLocked else { return Task {} }
        guard let capture = touchCapture else { return Task {} }
        touchCapture = nil
        let task = pendingPress; pendingPress = nil
        return releaseInteraction(capture, pendingPress: task)
    }

    @discardableResult public func holdInterrupted(interaction id: UUID) -> Task<Void, Never> {
        guard touch == id else { return Task {} }
        touch = nil; holdCancelled = true
        return cancelTouchCapture()
    }

    public func controlDisappeared(interaction id: UUID) async {
        if touch == id { await holdInterrupted(interaction: id).value }
        else if captureControl == id {
            pendingPress?.cancel(); pendingPress = nil
            if let capture = touchCapture ?? interaction {
                touchCapture = nil
                await cancelInteraction(capture)
            }
        }
    }

    @discardableResult public func activate(interaction control: UUID) -> Task<Void, Never> {
        if capturing || pendingPress != nil {
            guard let capture = touchCapture ?? interaction else { return Task {} }
            let task = pendingPress; pendingPress = nil
            touchCapture = nil
            return releaseInteraction(capture, pendingPress: task)
        }
        guard canPress else { return Task {} }
        let capture = UUID(); touchCapture = capture
        startInteraction(capture, control: control)
        return pendingPress ?? Task {}
    }

    private func startInteraction(_ id: UUID, control: UUID) {
        captureControl = control
        pendingPress = Task {
            await self.press(interaction: id)
            if self.interaction == id { self.pendingPress = nil }
        }
    }

    private func cancelTouchCapture() -> Task<Void, Never> {
        pendingPress?.cancel(); pendingPress = nil
        let capture = touchCapture; touchCapture = nil
        return Task {
            if let capture { await self.cancelInteraction(capture) }
        }
    }

    @discardableResult public func releaseInteraction(_ id: UUID, pendingPress: Task<Void, Never>? = nil) -> Task<Void, Never> {
        if interaction != id || state != .recording { pendingPress?.cancel() }
        return Task {
            guard interaction == id else { return }
            await release()
        }
    }

    public func cancelInteraction(_ id: UUID) async {
        guard interaction == id, capturing else { return }
        await cancel()
    }

    public func press(interaction id: UUID = UUID()) async {
        guard !Task.isCancelled, canPress, deliver != nil else { return }
        interaction = id
        generation &+= 1
        let owner = generation
        state = .authorizing; elapsed = 0; outcome = nil; isLocked = false; lockProgress = 0; discardProgress = 0
        recorded = Data(); recordedSeconds = 0
        await stopSpeech()
        guard !Task.isCancelled, generation == owner, state == .authorizing else { return }
        let allowed = await permission()
        // Permission belongs to the app; a grant still cannot revive the old capture intent.
        microphoneRefused = !allowed
        guard !Task.isCancelled, generation == owner, state == .authorizing else { return }
        guard allowed else { state = .refused; holdCancelled = true; return }
        let id = UUID().uuidString
        do { try startSegment(owner: owner, resuming: false) } catch { state = .failed; return }
        talkID = id
        state = .recording
        runClock(owner: owner)
        let begin = begin
        beginning = Task { await begin(id) }
    }

    /// Stops the microphone and keeps what was said, so a locked Talk can be resumed or sent later.
    public func pause() {
        guard state == .recording, isLocked else { return }
        generation &+= 1
        recordedSeconds = duration
        recorded.append(recorder.stop())
        timer?.cancel(); timer = nil
        state = .paused
    }

    public func resume() async {
        guard state == .paused else { return }
        generation &+= 1
        let owner = generation
        // Speech that started while paused holds the audio session the microphone needs.
        await stopSpeech()
        guard generation == owner, state == .paused else { return }
        // A microphone that won't restart leaves the paused Talk as it was, still ready to send.
        do { try startSegment(owner: owner, resuming: true) } catch { return }
        state = .recording
        runClock(owner: owner)
    }

    private func startSegment(owner: UInt64, resuming: Bool) throws {
        try recorder.start(limitReached: { [weak self] in
            Task {
                guard let self, self.generation == owner, self.state == .recording else { return }
                await self.release()
            }
        }, failed: { [weak self] in
            Task { await self?.segmentFailed(owner: owner, resuming: resuming) }
        })
    }

    /// The microphone failed after the press already showed recording; a resumed Talk goes back to paused with what it had.
    private func segmentFailed(owner: UInt64, resuming: Bool) async {
        guard generation == owner, state == .recording else { return }
        generation &+= 1
        timer?.cancel(); timer = nil
        if resuming { state = .paused; return }
        let id = talkID
        talkID = nil; recorded = Data(); recordedSeconds = 0
        isLocked = false; lockProgress = 0; discardProgress = 0
        state = .failed; holdCancelled = true
        if let id { await endBegun(id) }
    }

    private func runClock(owner: UInt64) {
        started = now()
        timer = Task { [weak self] in
            while !Task.isCancelled {
                do { try await self?.sleep(0.2) } catch { return }
                guard let self, self.generation == owner, self.state == .recording else { return }
                self.elapsed = min(300, Int(self.duration))
                if self.elapsed >= 300 { await self.release(); return }
            }
        }
    }

    public func release() async {
        if state == .authorizing { generation &+= 1; state = .idle; return }
        guard state == .recording || state == .paused, let id = talkID, let deliver else { return }
        generation &+= 1
        isLocked = false; lockProgress = 0; discardProgress = 0
        var pcm = recorded
        if state == .recording { pcm.append(recorder.stop()) }
        // Each resumed segment has its own buffer cap, so the joined Talk is held to the single-Talk limit here.
        if pcm.count > TalkAudio.maxBytes { pcm = pcm.prefix(TalkAudio.maxBytes) }
        recorded = Data(); recordedSeconds = 0
        timer?.cancel(); timer = nil
        talkID = nil
        guard pcm.count >= Self.shortestBytes, pcm.count <= TalkAudio.maxBytes, pcm.count.isMultiple(of: 2) else {
            state = .cancelled; holdCancelled = true
            outcome = pcm.count < Self.shortestBytes ? .tooShort : .discarded
            await endBegun(id)
            return
        }
        state = .idle
        deliver(id, pcm, clock())
    }

    public func cancel() async {
        generation &+= 1
        let id = talkID
        if state == .recording { _ = recorder.stop() }
        timer?.cancel(); timer = nil
        recorded = Data(); recordedSeconds = 0
        state = .cancelled; holdCancelled = true; talkID = nil; outcome = .discarded
        isLocked = false; lockProgress = 0; discardProgress = 0
        if let id { await endBegun(id) }
    }

    private func endBegun(_ id: String) async {
        let beginning = beginning; self.beginning = nil
        if await beginning?.value == true { await cancelBegun(id) }
    }
}
