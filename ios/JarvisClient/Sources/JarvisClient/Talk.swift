import Foundation
import Observation

@MainActor public protocol TalkRecorder: AnyObject {
    var level: Double { get }
    func start(limitReached: @escaping @MainActor @Sendable () -> Void) throws
    func stop() -> Data
}

public enum TalkOutcome: Equatable, Sendable { case tooShort, discarded }

public enum TalkState: Equatable, Sendable {
    case idle, authorizing, recording, refused, failed, cancelled
}

/// Records a Talk and hands it to the Outbox. Nothing here waits on the connection.
@MainActor @Observable public final class TalkController {
    public private(set) var state: TalkState = .idle
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
    public var speaking = false
    public var level: Double { state == .recording ? recorder.level : 0 }
    public var label: String { label(for: state) }
    public func label(for state: TalkState) -> String {
        switch state {
        case .recording: isLocked ? "Send Talk" : "Release to send. Slide up to lock or left to cancel."
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
    public var canPress: Bool { state != .authorizing && state != .recording }
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
            if state == .authorizing || state == .recording || pendingPress != nil { discards += 1 }
            return cancelTouchCapture()
        }
        // Locked controls replace the held mic, so the touch no longer owns capture.
        if state == .recording, verticalTranslation < -Self.lockDistance { isLocked = true; touch = nil }
        return Task {}
    }

    public func send() async { await release() }
    public func discard() async {
        guard state == .authorizing || state == .recording else { return }
        discards += 1
        await cancel()
    }

    /// The accessibility Cancel Talk action: a discard, retiring a press that hasn't started too.
    public func cancel(interaction id: UUID) async {
        guard state == .authorizing || state == .recording || pendingPress != nil else { return }
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
        if state == .recording || state == .authorizing || pendingPress != nil {
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
        guard interaction == id, state == .authorizing || state == .recording else { return }
        await cancel()
    }

    public func press(interaction id: UUID = UUID()) async {
        guard !Task.isCancelled, canPress, deliver != nil else { return }
        interaction = id
        generation &+= 1
        let owner = generation
        state = .authorizing; elapsed = 0; outcome = nil; isLocked = false; lockProgress = 0; discardProgress = 0
        await stopSpeech()
        guard !Task.isCancelled, generation == owner, state == .authorizing else { return }
        let allowed = await permission()
        // Permission belongs to the app; a grant still cannot revive the old capture intent.
        microphoneRefused = !allowed
        guard !Task.isCancelled, generation == owner, state == .authorizing else { return }
        guard allowed else { state = .refused; holdCancelled = true; return }
        let id = UUID().uuidString
        do {
            try recorder.start { [weak self] in
                Task {
                    guard let self, self.generation == owner, self.state == .recording else { return }
                    await self.release()
                }
            }
        } catch { state = .failed; return }
        talkID = id
        state = .recording; started = now()
        timer = Task { [weak self] in
            while !Task.isCancelled {
                do { try await self?.sleep(0.2) } catch { return }
                guard let self, self.generation == owner, self.state == .recording else { return }
                self.elapsed = min(300, Int(max(0, self.now() - self.started)))
                if self.elapsed >= 300 { await self.release(); return }
            }
        }
        let begin = begin
        beginning = Task { await begin(id) }
    }

    public func release() async {
        if state == .authorizing { generation &+= 1; state = .idle; return }
        guard state == .recording, let id = talkID, let deliver else { return }
        generation &+= 1
        isLocked = false; lockProgress = 0; discardProgress = 0
        let pcm = recorder.stop()
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
        state = .cancelled; holdCancelled = true; talkID = nil; outcome = .discarded
        isLocked = false; lockProgress = 0; discardProgress = 0
        if let id { await endBegun(id) }
    }

    private func endBegun(_ id: String) async {
        let beginning = beginning; self.beginning = nil
        if await beginning?.value == true { await cancelBegun(id) }
    }
}
