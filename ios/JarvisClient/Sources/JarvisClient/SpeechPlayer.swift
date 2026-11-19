import AVFoundation
import Foundation
import Observation

@MainActor public protocol SpeechOutput: AnyObject {
    func play(_ pcm: Data, finished: @escaping @MainActor @Sendable () -> Void) throws
    func stop()
}

@MainActor @Observable public final class SpeechPlayer {
    public private(set) var speechID: String?
    public private(set) var replyID: String?
    public private(set) var sentence: String?
    public private(set) var completedSentences: [String] = []
    public private(set) var lastFinishedIndex: Int64 = -1
    public private(set) var messages: [String: String] = [:]
    public private(set) var missedReplies: Set<String> = []
    public var playingSpeechID: String? { sentence == nil ? nil : speechID }
    private struct Clip { let index: Int64; let text: String; let pcm: Data }
    private let session: any TalkAudioSession
    private let output: any SpeechOutput
    private let wait: @Sendable () async throws -> Void
    private var heardReplies: Set<String> = []
    private var queue: [Clip] = []
    private var finishedCallbacks: Set<Int64> = []
    private var nextIndex: Int64 = 0
    private var ended = false
    private var generation: UInt64 = 0
    private var watchdog: Task<Void, Never>?
    private var report: @MainActor (HubCommand) -> Void = { _ in }
    private var drained: @MainActor (String) -> Void = { _ in }
    public init(session: any TalkAudioSession, output: any SpeechOutput = NativeSpeechOutput(),
                wait: @escaping @Sendable () async throws -> Void = { try await Task.sleep(for: .seconds(2)) }) {
        self.session = session; self.output = output; self.wait = wait
    }
    public func configure(report: @escaping @MainActor (HubCommand) -> Void, drained: @escaping @MainActor (String) -> Void) {
        self.report = report; self.drained = drained
    }
    public func begin(speechID: String, replyID: String) {
        stop(reportStopped: false)
        generation &+= 1
        self.speechID = speechID; self.replyID = replyID
        nextIndex = 0; lastFinishedIndex = -1; completedSentences = []; finishedCallbacks = []; ended = false
        missedReplies.insert(replyID); messages[replyID] = nil
    }
    public func append(speechID: String, replyID: String, index: Int64, text: String, pcm: Data) {
        guard self.speechID == speechID, self.replyID == replyID, !ended,
              index == nextIndex, pcm.count % 2 == 0 else { return }
        guard !pcm.isEmpty else {
            messages[replyID] = "No audio arrived for this reply. Tap Play to try again."
            stop(); return
        }
        nextIndex += 1; watchdog?.cancel(); watchdog = nil
        let clip = Clip(index: index, text: text, pcm: pcm), owner = generation
        do {
            try session.activatePlayback()
            queue.append(clip)
            try output.play(clip.pcm) { [weak self] in
                guard let self, self.generation == owner, self.speechID == speechID, index > self.lastFinishedIndex else { return }
                self.finishedCallbacks.insert(index)
                self.completeReadyClips(owner: owner)
            }
            if queue.count == 1 { sentence = clip.text; emit("started", index: clip.index) }
        } catch { messages[replyID] = "Could not play this reply. Tap Play to try again."; stop() }
    }

    public func end(speechID: String, replyID: String, lastIndex: Int64) {
        guard self.speechID == speechID, self.replyID == replyID, lastIndex == nextIndex - 1 else { return }
        ended = true; watchdog?.cancel(); watchdog = nil
        if sentence == nil && queue.isEmpty { finish() }
    }
    public func stop(speechID: String? = nil, reportStopped: Bool = true) {
        if let speechID, self.speechID != speechID { return }
        guard let id = self.speechID else { return }
        if reportStopped { emit("stopped", index: lastFinishedIndex) }
        generation &+= 1; watchdog?.cancel(); watchdog = nil
        output.stop(); session.releasePlayback(); queue = []; sentence = nil
        self.speechID = nil; replyID = nil
        drained(id)
    }
    public func reset() {
        stop(reportStopped: false)
        missedReplies = []; heardReplies = []; completedSentences = []; messages = [:]
    }
    public func markMissed(replyID: String) { if !heardReplies.contains(replyID) { missedReplies.insert(replyID) } }
    private func emit(_ state: String, index: Int64) {
        guard let speechID else { return }
        report(HubCommand(type: "playback_report", payload: ["speech_id": .string(speechID),
            "clip_index": .integer(index), "state": .string(state)]))
    }
    private func completeReadyClips(owner: UInt64) {
        while let clip = queue.first, finishedCallbacks.remove(clip.index) != nil {
            queue.removeFirst(); completedSentences.append(clip.text)
            lastFinishedIndex = clip.index; emit("finished", index: clip.index)
            sentence = queue.first?.text
            if let next = queue.first { emit("started", index: next.index) }
        }
        if queue.isEmpty {
            if ended { finish() }
            else { watchForNext(owner: owner) }
        }
    }
    private func watchForNext(owner: UInt64) {
        watchdog?.cancel()
        watchdog = Task { [weak self, wait] in
            do { try await wait() } catch { return }
            guard !Task.isCancelled, let self, self.generation == owner, self.sentence == nil else { return }
            self.output.stop(); self.session.releasePlayback()
            if let id = self.speechID { self.drained(id) }
        }
    }
    private func finish() {
        guard let id = speechID else { return }
        if let replyID, nextIndex > 0 { missedReplies.remove(replyID); heardReplies.insert(replyID) }
        output.stop(); session.releasePlayback(); speechID = nil; replyID = nil; sentence = nil
        drained(id)
    }
}

@MainActor public final class NativeSpeechOutput: SpeechOutput {
    private var engine: AVAudioEngine?
    private var node: AVAudioPlayerNode?
    public init() {}
    public func play(_ pcm: Data, finished: @escaping @MainActor @Sendable () -> Void) throws {
        guard let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24_000, channels: 1, interleaved: false),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(pcm.count / 2)),
              let samples = buffer.floatChannelData?[0] else { throw HubProtocolError.malformedAudio }
        buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) {
            let value = UInt16(pcm[index * 2]) | UInt16(pcm[index * 2 + 1]) << 8
            samples[index] = Float(Int16(bitPattern: value)) / 32768
        }
        if engine == nil {
            let engine = AVAudioEngine(), node = AVAudioPlayerNode()
            engine.attach(node); engine.connect(node, to: engine.mainMixerNode, format: format)
            self.engine = engine; self.node = node
            try engine.start(); node.play()
        }
        node?.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in
            Task { @MainActor in finished() }
        }
    }
    public func stop() { node?.stop(); engine?.stop(); node = nil; engine = nil }
}
