import Foundation
import Observation

@MainActor public protocol TalkRecorder: AnyObject {
    func start(limitReached: @escaping @MainActor @Sendable () -> Void) throws
    func stop() -> Data
}

public enum TalkState: Equatable, Sendable {
    case idle, authorizing, recording, uploading, thinking, refused, failed, cancelled
}

@MainActor @Observable public final class TalkController {
    public private(set) var state: TalkState = .idle
    public private(set) var elapsed = 0
    public private(set) var ready = false
    public private(set) var talkID: String?
    public private(set) var requestID: String?
    public private(set) var microphoneRefused = false
    public var speaking = false
    public var label: String {
        switch state {
        case .recording: "Release to send. Slide up to cancel."
        case .uploading, .thinking: "Jarvis is thinking"
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
        case .failed: "That didn't reach Jarvis. Hold to talk again."
        case .cancelled: "Talk cancelled."
        default: nil
        }
    }
    public var canPress: Bool { ready && ![.authorizing, .recording, .uploading, .thinking].contains(state) }
    private let recorder: any TalkRecorder
    private let permission: @MainActor () async -> Bool
    private let now: @MainActor () -> Double
    private let sleep: @Sendable (Double) async throws -> Void
    private var send: (@MainActor (HubCommand) async throws -> Void)?
    private var retire: (@MainActor (String) async -> Void)?
    private var upload: (@MainActor (String, String, Data) async throws -> CommandResult)?
    private var admission: @MainActor (String) async -> Bool = { _ in true }
    private var completedRequests: [String] = []
    private var stopSpeech: @MainActor () async -> Void = {}
    private var generation: UInt64 = 0
    private var beginID: String?
    private var started = 0.0
    private var timer: Task<Void, Never>?
    private var operation: Task<Void, Never>?
    private var deadline: Task<Void, Never>?

    public init(recorder: any TalkRecorder, permission: @escaping @MainActor () async -> Bool,
                now: @escaping @MainActor () -> Double = { ProcessInfo.processInfo.systemUptime },
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) }) {
        self.recorder = recorder; self.permission = permission; self.now = now; self.sleep = sleep
    }

    public func configure(ready: Bool, send: @escaping @MainActor (HubCommand) async throws -> Void,
                          retire: @escaping @MainActor (String) async -> Void,
                          upload: @escaping @MainActor (String, String, Data) async throws -> CommandResult,
                          admission: @escaping @MainActor (String) async -> Bool = { _ in true },
                          stopSpeech: @escaping @MainActor () async -> Void = {}) {
        self.ready = ready; self.send = send; self.retire = retire; self.upload = upload; self.admission = admission; self.stopSpeech = stopSpeech
    }

    public func setReady(_ value: Bool) async {
        ready = value
        if !value, state == .authorizing || state == .recording { await cancel() }
    }

    public func press() async {
        guard canPress, send != nil else { return }
        generation &+= 1
        let owner = generation
        state = .authorizing; requestID = nil; elapsed = 0
        await stopSpeech()
        guard generation == owner, state == .authorizing, ready else { return }
        let allowed = await permission()
        // Permission belongs to the app; a grant still cannot revive the old capture intent.
        microphoneRefused = !allowed
        guard generation == owner, state == .authorizing, ready else { return }
        guard allowed else { state = .refused; return }
        let id = UUID().uuidString
        let begin = HubCommand(type: "talk_begin", payload: ["talk_id": .string(id)])
        talkID = id; beginID = begin.id
        do {
            try recorder.start { [weak self] in
                Task {
                    guard let self, self.generation == owner, self.state == .recording else { return }
                    await self.release()
                }
            }
            state = .recording; started = now()
            timer = Task { [weak self] in
                while !Task.isCancelled {
                    do { try await self?.sleep(0.2) } catch { return }
                    guard let self, self.generation == owner, self.state == .recording else { return }
                    self.elapsed = min(300, Int(max(0, self.now() - self.started)))
                    if self.elapsed >= 300 { await self.release(); return }
                }
            }
            try await send?(begin)
            guard generation == owner else { return }
        } catch {
            guard generation == owner, state == .recording else { return }
            await cancel(failed: true)
        }
    }

    public func release() async {
        if state == .authorizing { generation &+= 1; state = .idle; return }
        guard state == .recording, let id = talkID, let beginID, let upload else { return }
        let owner = generation
        let pcm = recorder.stop()
        timer?.cancel(); timer = nil
        state = .uploading
        let admitted = await admission(beginID)
        guard generation == owner, state == .uploading else { return }
        guard admitted else { await cancel(failed: true); return }
        guard !pcm.isEmpty, pcm.count <= 9_600_000, pcm.count.isMultiple(of: 2) else { await cancel(failed: true); return }
        let uploadID = UUID().uuidString
        let firstAttempt = now()
        deadline = Task { [weak self] in
            do { try await self?.sleep(30) } catch { return }
            guard let self, self.generation == owner, self.state == .uploading else { return }
            await self.cancel(failed: true)
        }
        let task = Task { [weak self] in
            guard let self else { return }
            while self.generation == owner, !Task.isCancelled {
                do {
                    let result = try await upload(id, uploadID, pcm)
                    guard self.generation == owner, self.state == .uploading else { return }
                    guard result.id == uploadID, result.ok, case .string(let request) = result.result?["request_id"] else {
                        await self.cancel(failed: true); return
                    }
                    self.requestID = request; self.state = self.completedRequests.contains(request) || self.speaking ? .idle : .thinking
                    self.deadline?.cancel(); self.deadline = nil
                    return
                } catch {
                    guard self.generation == owner, !Task.isCancelled else { return }
                    if let error = error as? HubTransportError, case .http(let code) = error, code < 500, code != 429 {
                        await self.cancel(failed: true); return
                    }
                    guard self.now() - firstAttempt < 30 else { await self.cancel(failed: true); return }
                    do { try await self.sleep(1) } catch { return }
                }
            }
        }
        operation = task
        await task.value
    }

    public func cancel(failed: Bool = false) async {
        generation &+= 1
        let id = talkID, begin = beginID
        let retire = retire, send = send
        if state == .recording { _ = recorder.stop() }
        timer?.cancel(); timer = nil; operation?.cancel(); operation = nil; deadline?.cancel(); deadline = nil
        state = failed ? .failed : .cancelled
        talkID = nil; beginID = nil; requestID = nil
        if let begin { await retire?(begin) }
        if let id { try? await send?(HubCommand(type: "talk_cancel", payload: ["talk_id": .string(id)])) }
    }

    public func receive(_ result: CommandResult) async {
        if result.id == beginID, !result.ok, state == .recording || state == .uploading { await cancel(failed: true) }
    }

    public func receive(_ event: HubEvent) {
        if event.kind == .speechBegin, state == .thinking { state = .idle }
        if event.kind == .replyEnd, let request = event.string("request_id") {
            completedRequests.append(request)
            if completedRequests.count > 128 { completedRequests.removeFirst() }
            if request == requestID, state == .thinking { state = .idle }
        }
    }
}
