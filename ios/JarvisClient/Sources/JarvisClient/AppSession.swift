import Foundation
import Observation

@MainActor @Observable public final class AppSession {
    public private(set) var credentials: PairingCredentials?
    public private(set) var connection: HubConnectionState = .unpaired
    public private(set) var loading = true
    public private(set) var pairing = false
    public private(set) var error: PairingError?
    public private(set) var client: HubClient?
    public private(set) var conversation = ConversationProjection()
    public var draft = ""
    public private(set) var typedLines: [TypedLine] = []
    public private(set) var inputMessage: String?
    public let playback: SpeechPlayer
    private var playbackDelivery: Task<Void, Never>?
    private var playbackRevision: UInt64 = 0
    private var pendingPlaybackDrain: (speechID: String, revision: UInt64)?
    private var backgroundSpeechID: String?
    public private(set) var replayMessages: [String: String] = [:]
    private var replayCommands: [String: HubCommand] = [:]
    public let talk: TalkController
    public private(set) var speechID: String?
    private var lastCompletedClip: Int64 = -1
    private let uploadTalk: TalkUpload
    private let stopPlayback: @MainActor () -> Void

    public func reportCompletedClip(speechID: String, index: Int64) {
        guard self.speechID == speechID else { return }
        if index == lastCompletedClip + 1 { lastCompletedClip = index }
    }

    public func playbackDrained(speechID: String) async {
        guard self.speechID == speechID else { return }
        self.speechID = nil; talk.speaking = false
        await client?.playbackDrained(speechID: speechID)
    }

    private func interruptSpeech() async {
        let speechID = playback.speechID ?? self.speechID
        let finished = max(lastCompletedClip, playback.lastFinishedIndex)
        playback.stop(reportStopped: false); stopPlayback()
        guard let speechID, let client else { return }
        self.speechID = nil; talk.speaking = false
        try? await sendCommand(client, HubCommand(type: "interrupt", payload: [
            "speech_id": .string(speechID), "last_clip_index": .integer(finished)
        ]))
    }

    public var draftValidationMessage: String? {
        draft.utf8.count > 16_384 ? "Your line is too long. Use at most 16,384 UTF-8 bytes." : nil
    }

    public var canSubmit: Bool {
        connection.canSend && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && draft.utf8.count <= 16_384 && !typedLines.contains {
                $0.text == draft && ($0.outcome == .unanswered || $0.outcome == .deliveryUncertain || $0.outcome == .outcomeUnknown)
            }
    }

    @discardableResult public func recoverTypedLine(id: String) -> Bool {
        guard let index = typedLines.firstIndex(where: { $0.id == id }),
              typedLines[index].canRecover,
              draft.isEmpty || draft == typedLines[index].text else { return false }
        draft = typedLines[index].text
        typedLines[index].outcome = .recovered
        return true
    }

    public func sendTypedLine() async {
        guard canSubmit, let client else {
            if let message = draftValidationMessage { inputMessage = message }
            return
        }
        let owner = generation
        let text = draft
        let command = HubCommand(type: "say_text", payload: ["text": .string(text)])
        typedLines.append(TypedLine(command: command, outcome: .unanswered)); inputMessage = nil
        do {
            try await sendCommand(client, command)
            guard generation == owner else { return }
            if draft == text && typedLines.first(where: { $0.id == command.id })?.outcome != .recovered { draft = "" }
        } catch {
            let delivery = await client.delivery(of: command.id)
            guard generation == owner, let index = typedLines.firstIndex(where: { $0.id == command.id }) else { return }
            if typedLines[index].outcome == .accepted {
                if draft == text { draft = "" }
                return
            }
            guard typedLines[index].outcome == .unanswered || typedLines[index].outcome == .deliveryUncertain else { return }
            switch delivery {
            case .pending: typedLines[index].outcome = .deliveryUncertain
            case .completed(let result):
                receive(.result(result))
                if result.ok && draft == text { draft = "" }
            case .absent:
                if draft == text {
                    typedLines.remove(at: index)
                    inputMessage = "Can't reach the Mac. Your line is still here to send."
                } else {
                    typedLines[index].outcome = .notSent
                }
            }
        }
    }

    private func receive(_ update: HubClientUpdate) {
        switch update {
        case .event(let event):
            conversation.ingest(event); talk.receive(event)
            switch event.kind {
            case .speechBegin:
                if let id = event.string("speech_id"), let reply = event.string("reply_id"), foreground {
                    playback.begin(speechID: id, replyID: reply)
                    speechID = id; lastCompletedClip = -1
                }
            case .speechEnd:
                if let id = event.string("speech_id"), let reply = event.string("reply_id"), let last = event.integer("last_clip_index") {
                    playback.end(speechID: id, replyID: reply, lastIndex: last)
                }
            case .speechStop:
                if let id = event.string("speech_id") { playback.stop(speechID: id, reportStopped: false) }
            case .replyEnd:
                if let reply = event.string("reply_id"), playback.replyID != reply { playback.markMissed(replyID: reply) }
            default: break
            }
        case .audio(let event, let pcm):
            if foreground || backgroundSpeechID == playback.speechID && playback.activeSpeechID != nil,
               let id = event.string("speech_id"), let reply = event.string("reply_id"),
               let index = event.integer("index"), let text = event.string("text") {
                playback.append(speechID: id, replyID: reply, index: index, text: text, pcm: pcm)
                talk.speaking = playback.playingSpeechID != nil
            }
        case .result(let result):
            if let entry = replayCommands.first(where: { $0.value.id == result.id }) {
                replayCommands.removeValue(forKey: entry.key)
                replayMessages[entry.key] = result.ok ? nil : result.error?.message ?? "Jarvis could not play this reply."
            }
            guard let index = typedLines.firstIndex(where: { $0.id == result.id }) else { return }
            if result.ok { typedLines[index].outcome = .accepted }
            else if result.error?.code == "outcome_unknown" { typedLines[index].outcome = .outcomeUnknown }
            else { typedLines[index].outcome = .rejected(result.error?.message ?? "Jarvis could not accept your line.") }
        case .speechConnectionEnded(let id):
            playback.stop(speechID: id, reportStopped: false)
        case .failure:
            for index in typedLines.indices where typedLines[index].outcome == .unanswered {
                typedLines[index].outcome = .deliveryUncertain
            }
        default: break
        }
    }

    private let store: any PairingStore
    private let service: PairingService
    private let makeClient: (PairingCredentials) throws -> HubClient
    private let makePaths: () -> HubNetworkPaths?
    private let sendCommand: @Sendable (HubClient, HubCommand) async throws -> Void
    private var paths: HubNetworkPaths?
    private var updates: Task<Void, Never>?
    private var generation: UInt64 = 0
    private var foreground = true
    private var foregroundGeneration: UInt64 = 0
    private var launched = false

    public init(store: any PairingStore, service: PairingService = .urlSession,
                makeClient: @escaping (PairingCredentials) throws -> HubClient = {
                    HubClient(transport: try .urlSession(origin: $0.origin, token: $0.deviceToken))
                }, makePaths: @escaping () -> HubNetworkPaths? = { HubNetworkPaths() },
                talk: TalkController? = nil, playback: SpeechPlayer? = nil, uploadTalk: TalkUpload = TalkUpload(),
                stopPlayback: @escaping @MainActor () -> Void = {},
                sendCommand: @escaping @Sendable (HubClient, HubCommand) async throws -> Void = { try await $0.send($1) }) {
        self.store = store; self.service = service; self.makeClient = makeClient; self.makePaths = makePaths
        self.sendCommand = sendCommand
        let audioSession = NativeTalkAudioSession()
        self.playback = playback ?? SpeechPlayer(session: audioSession)
        self.talk = talk ?? TalkController(recorder: NativeTalkRecorder(session: audioSession), permission: { await NativeTalkRecorder.requestPermission() })
        self.uploadTalk = uploadTalk; self.stopPlayback = stopPlayback
        self.playback.configure(report: { [weak self] command in
            guard let self, let client = self.client else { return }
            self.playbackRevision &+= 1
            let previous = self.playbackDelivery
            self.playbackDelivery = Task {
                await previous?.value
                guard !Task.isCancelled else { return }
                try? await client.reportPlayback(command)
            }
        }, drained: { [weak self] id in
            guard let self else { return }
            self.talk.speaking = false
            if self.speechID == id { self.speechID = nil }
            guard let client = self.client else { return }
            let previous = self.playbackDelivery, owner = self.generation, revision = self.playbackRevision
            self.pendingPlaybackDrain = (id, revision)
            self.playbackDelivery = Task {
                await previous?.value
                // A newer clip can resume and finish while an older receipt is still being written.
                guard !Task.isCancelled, self.generation == owner, self.client === client,
                      self.playbackRevision == revision else { return }
                await client.playbackDrained(speechID: id)
                guard self.generation == owner, self.client === client, self.playbackRevision == revision else { return }
                if self.pendingPlaybackDrain?.revision == revision { self.pendingPlaybackDrain = nil }
                if self.backgroundSpeechID == id { self.backgroundSpeechID = nil }
            }
        })
    }

    public func launch() async {
        guard !launched else { return }
        launched = true
        let owner = generation
        do {
            let saved = try store.load()
            loading = false
            if let saved, generation == owner { try await connect(saved, owner: owner) }
        } catch { loading = false; self.error = .storage }
    }

    public func pair(_ input: PairingInput) async {
        guard !pairing, credentials == nil else { return }
        generation &+= 1
        let owner = generation
        pairing = true; error = nil
        do {
            let result = try await service.pair(input)
            guard generation == owner else { return }
            do { try store.save(result) } catch { throw PairingError.storage }
            pairing = false
            try await connect(result, owner: owner)
        } catch {
            guard generation == owner else { return }
            pairing = false
            if !(error is CancellationError) { self.error = error as? PairingError ?? .invalidResponse }
        }
    }

    public func silencePlayback() { playback.stop(reportStopped: false); stopPlayback(); talk.speaking = false }

    public func replay(replyID: String) async {
        guard connection.canSend, let client else { return }
        let owner = generation
        let command = replayCommands[replyID] ?? HubCommand(type: "replay_speech", payload: ["reply_id": .string(replyID)])
        replayCommands[replyID] = command; replayMessages[replyID] = nil
        do { try await sendCommand(client, command) }
        catch {
            guard generation == owner, self.client === client, replayCommands[replyID]?.id == command.id else { return }
            replayMessages[replyID] = "Playback may not have reached the Mac. Tap Play to retry the same request."
        }
    }

    public func showError(_ error: PairingError) { self.error = error }

    /// Invalidates in-flight pairing before clearing the sole durable credential record.
    public func unpair() async {
        generation &+= 1
        let owner = generation, old = client
        updates?.cancel(); updates = nil; paths?.cancel(); paths = nil
        connection = .unpaired; speechID = nil; playback.stop(reportStopped: false); stopPlayback()
        playbackDelivery?.cancel(); playbackDelivery = nil; pendingPlaybackDrain = nil
        await talk.setReady(false)
        guard generation == owner, client === old else { await old?.stop(); return }
        await talk.cancel()
        guard generation == owner, client === old else { await old?.stop(); return }
        client = nil; credentials = nil; pairing = false; loading = false
        do { try store.clear(); error = nil } catch { self.error = .storage }
        conversation = ConversationProjection(); typedLines = []; draft = ""; inputMessage = nil
        playback.reset(); replayCommands = [:]; replayMessages = [:]
        await old?.stop()
    }

    public func setForeground(_ value: Bool) async {
        foreground = value
        foregroundGeneration &+= 1
        let transition = foregroundGeneration, owner = generation, client = client
        if !value { await talk.cancel() }
        guard foregroundGeneration == transition, generation == owner, self.client === client else { return }
        let playing = playback.activeSpeechID ?? pendingPlaybackDrain?.speechID ?? backgroundSpeechID
        backgroundSpeechID = value ? nil : playing
        if !value && playing == nil { playback.stop(reportStopped: false) }
        await client?.setForeground(value, playingSpeechID: value ? nil : playing)
    }

    private func connect(_ credentials: PairingCredentials, owner: UInt64) async throws {
        guard generation == owner else { return }
        let client = try makeClient(credentials)
        self.credentials = credentials; self.client = client; connection = .connecting
        talk.configure(ready: false, send: { [weak self] command in
            guard let self, self.client === client,
                  self.generation == owner || command.type == "talk_cancel" else { throw HubProtocolError.notConnected }
            try await self.sendCommand(client, command)
        }, retire: { await client.retireTalkBegin(id: $0) }, upload: { [weak self] id, commandID, pcm in
            guard let self, self.generation == owner else { throw CancellationError() }
            return try await self.uploadTalk.send(credentials: credentials, talkID: id, commandID: commandID, pcm: pcm)
        }, admission: { await client.finishTalkBegin(id: $0) }, stopSpeech: { [weak self] in await self?.interruptSpeech() })
        paths = makePaths()
        updates = Task { [weak self] in
            for await update in client.updates {
                guard !Task.isCancelled, let self, self.generation == owner, self.client === client else { return }
                if case .state(let state) = update {
                    if state == .unpaired { await self.unpair(); return }
                    self.connection = state
                    await self.talk.setReady(state.canSend)
                } else {
                    if case .result(let result) = update { await self.talk.receive(result) }
                    guard self.generation == owner, self.client === client else { return }
                    self.receive(update)
                }
            }
        }
        if !foreground { await client.setForeground(false) }
        guard generation == owner else { await client.stop(); return }
        await client.start(paths: paths?.changes)
        if generation != owner { await client.stop() }
    }
}

private enum CommandDelivery: Sendable {
    case pending, completed(CommandResult), absent
}

private extension HubClient {
    // Read retention and results in one actor turn so an arriving result cannot fall between two snapshots.
    func delivery(of id: String) -> CommandDelivery {
        if let result = results[id] { return .completed(result) }
        return pendingCommands.contains { $0.id == id } ? .pending : .absent
    }
}

public enum CameraPermission: Sendable { case unknown, allowed, refused }

@MainActor @Observable public final class PairingCamera {
    public private(set) var permission: CameraPermission
    private let request: @Sendable () async -> Bool
    public init(permission: CameraPermission = .unknown, request: @escaping @Sendable () async -> Bool) {
        self.permission = permission; self.request = request
    }
    public func authorize() async -> Bool {
        if permission == .unknown { permission = await request() ? .allowed : .refused }
        return permission == .allowed
    }
    public func refresh(_ permission: CameraPermission) { self.permission = permission }
}
