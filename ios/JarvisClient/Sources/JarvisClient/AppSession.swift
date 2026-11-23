import Foundation
import Observation

@MainActor @Observable public final class AppSession {
    public private(set) var credentials: PairingCredentials?
    public private(set) var connection: HubConnectionState = .unpaired
    /// What the header says: a working connection that drops for a moment keeps saying ready.
    public private(set) var shownConnection: HubConnectionState = .unpaired
    private var connectionNotice: Task<Void, Never>?
    public private(set) var loading = true
    public private(set) var pairing = false
    public private(set) var error: PairingError?
    public private(set) var client: HubClient?
    public private(set) var conversation = ConversationProjection()
    private var tasksNeedRefresh = true
    private var tasksReplaying = true
    public var updatingTasks: Bool { tasksNeedRefresh && (connection == .connecting || connection.canSend) }
    public var openTasks: [OpenTask] { OpenTask.current(in: conversation.events, now: historyNow()) }
    public private(set) var loadingHistory = false
    public private(set) var hasOlderHistory = true
    public private(set) var historyMessage: String?
    public private(set) var focusHere = false
    public private(set) var focusPending = false
    public private(set) var focusMessage: String?
    private var focusCommand: HubCommand?
    public private(set) var contextReset: ContextReset = .idle
    private var resetCommand: HubCommand?
    public var canResetContext: Bool { connection.canSend && contextReset != .resetting }
    public private(set) var voiceOn = true
    public private(set) var voicePending = false
    public private(set) var voiceMessage: String?
    private let voiceStore: VoiceSettingStore?
    private var explicitSilence: Bool?
    private var voicePersistenceFailed = false
    private var voiceRevision: UInt64 = 0
    private var history: HistoryStore?
    private var historyBefore: Int64?
    public var draft = ""
    public private(set) var inputMessage: String?
    public private(set) var outbox: Outbox?
    private let makeOutboxTransport: @MainActor () -> any OutboxTransport
    private let outboxDirectory: URL
    private let noticeSleep: @Sendable (Double) async throws -> Void
    public let orb = OrbModel()
    private let orbNow: @MainActor () -> Double
    private let orbSleep: @Sendable (Double) async throws -> Void
    private var orbFrames: Task<Void, Never>?
    private var streamingReply: (id: String, started: Double)?
    public let playback: SpeechPlayer
    private var playbackDelivery: Task<Void, Never>?
    private var playbackRevision: UInt64 = 0
    private var pendingPlaybackDrain: (speechID: String, revision: UInt64)?
    private var backgroundSpeechID: String?
    public private(set) var replayMessages: [String: String] = [:]
    private var replayCommands: [String: HubCommand] = [:]
    public let talk: TalkController
    public let liveMode: LiveModeController
    public private(set) var speechID: String?
    private var lastCompletedClip: Int64 = -1
    private let stopPlayback: @MainActor () -> Void

    var playbackDrainPending: Bool { pendingPlaybackDrain != nil }

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
        // A cue is not speech the Hub can interrupt; it only stops here.
        let speechID = playback.playingCue ? self.speechID : playback.speechID ?? self.speechID
        let finished = playback.playingCue ? lastCompletedClip : max(lastCompletedClip, playback.lastFinishedIndex)
        playback.stop(reportStopped: false); stopPlayback()
        guard let speechID, let client else { return }
        self.speechID = nil; talk.speaking = false
        let command = HubCommand(type: "interrupt", payload: [
            "speech_id": .string(speechID), "last_clip_index": .integer(finished)
        ])
        // Queued ahead of the Talk's begin on the same socket, so the Hub learns what was heard first, without holding up recording.
        _ = await client.sendLive(command)
    }

    public var draftValidationMessage: String? {
        draft.utf8.count > 16_384 ? "Your line is too long. Use at most 16,384 UTF-8 bytes." : nil
    }

    public var canSubmit: Bool {
        outbox != nil && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && draft.utf8.count <= 16_384
    }

    public func sendTypedLine() async {
        guard canSubmit, let outbox else {
            if let message = draftValidationMessage { inputMessage = message }
            return
        }
        outbox.enqueueLine(draft, recordedAt: historyNow())
        draft = ""; inputMessage = nil
    }

    private func receive(_ update: HubClientUpdate) {
        switch update {
        case .event(let event):
            conversation.ingest(event)
            react(to: event)
        case .events(let events):
            conversation.ingest(contentsOf: events)
            for event in events { react(to: event) }
        default: receiveControl(update)
        }
    }

    private func react(to event: HubEvent) {
        if event.kind == .taskSnapshot && !tasksReplaying { tasksNeedRefresh = false }
        if event.kind == .heard, let request = event.string("request_id") { outbox?.heard(requestID: request) }
        switch event.kind {
        case .status:
            if let status = try? event.decodePayload(HubStatus.self) {
                focusHere = status.focus_client_id == credentials?.clientID
            }
        case .speechBegin:
            guard foreground || liveMode.isOn, let id = event.string("speech_id") else { break }
            // A cue is how a locked phone tells the User it heard the Wake word or stopped listening, so Silence keeps it.
            if event.string("cue") != nil { playback.begin(speechID: id, replyID: nil) }
            else if voiceOn, let reply = event.string("reply_id") {
                playback.begin(speechID: id, replyID: reply)
                speechID = id; lastCompletedClip = -1
            }
        case .speechEnd:
            if let id = event.string("speech_id"), let last = event.integer("last_clip_index") {
                playback.end(speechID: id, replyID: event.string("reply_id"), lastIndex: last)
            }
        case .speechStop:
            if let id = event.string("speech_id") { playback.stop(speechID: id, reportStopped: false) }
        case .replyDelta:
            if foreground, !tasksReplaying, let id = event.string("reply_id"), streamingReply?.id != id {
                streamingReply = (id, orbNow())
            }
        case .replyEnd:
            if streamingReply?.id == event.string("reply_id") { streamingReply = nil }
            if let reply = event.string("reply_id"), playback.replyID != reply { playback.markMissed(replyID: reply) }
        case .voiceState: liveMode.receive(voiceState: event)
        default: break
        }
    }

    private func receiveControl(_ update: HubClientUpdate) {
        switch update {
        case .audio(let event, let pcm):
            let cue = event.string("cue") != nil
            if voiceOn || cue, foreground || liveMode.isOn || backgroundSpeechID == playback.speechID && playback.activeSpeechID != nil,
               let id = event.string("speech_id"), let index = event.integer("index"), let text = event.string("text") {
                playback.append(speechID: id, replyID: event.string("reply_id"), index: index, text: text, pcm: pcm)
                if !cue { talk.speaking = playback.playingSpeechID != nil }
            }
        case .result(let result):
            if focusCommand?.id == result.id {
                focusCommand = nil; focusPending = false
                focusMessage = result.ok ? nil : result.error?.code == "outcome_unknown"
                    ? "Focus is not confirmed. Check the current status before trying again."
                    : "Couldn't reach the Mac. Not changed."
            }
            if resetCommand?.id == result.id {
                resetCommand = nil; contextReset = ContextReset(result)
            }
            liveMode.receive(result)
            if let entry = replayCommands.first(where: { $0.value.id == result.id }) {
                replayCommands.removeValue(forKey: entry.key)
                replayMessages[entry.key] = result.ok ? nil : result.error?.message ?? "Jarvis could not play this reply."
            }
        case .silence(let silence, let pending, let revision):
            guard revision == voiceRevision, explicitSilence == nil || explicitSilence == silence else { return }
            voiceOn = !silence; voicePending = pending
            voiceMessage = voicePersistenceFailed ? "Voice changed for this session, but could not be saved. Try again." : nil
            if silence { silencePlayback() }
        case .replayStarted:
            tasksNeedRefresh = true; tasksReplaying = true
        case .replay(let replay):
            tasksReplaying = false
            if replay.history_pruned == true { historyMessage = "Some older history is no longer available on the Mac." }
        case .commandDropped(let id):
            if resetCommand?.id == id { resetCommand = nil; contextReset = .unsure }
            liveMode.dropped(commandID: id)
        case .speechConnectionEnded(let id):
            playback.stop(speechID: id, reportStopped: false)
        case .failure:
            if focusPending { focusMessage = "Focus is not confirmed. Waiting for the Mac to reconcile it." }
        default: break
        }
    }

    private let store: any PairingStore
    private let service: PairingService
    private let makeClient: ((PairingCredentials) throws -> HubClient)?
    private let historyDirectory: URL?
    private let historyNow: @Sendable () -> Date
    private let makeTransport: (PairingCredentials) throws -> HubTransport
    private let makePaths: () -> HubNetworkPaths?
    private let sendCommand: @Sendable (HubClient, HubCommand) async throws -> Void
    private var paths: HubNetworkPaths?
    private var updates: Task<Void, Never>?
    private var generation: UInt64 = 0
    private var foreground = true
    private var foregroundGeneration: UInt64 = 0
    private var launched = false

    public init(store: any PairingStore, service: PairingService = .urlSession,
                makeClient: ((PairingCredentials) throws -> HubClient)? = nil,
                voiceStore: VoiceSettingStore? = nil,
                historyDirectory: URL? = nil, historyNow: @escaping @Sendable () -> Date = { Date() },
                makeTransport: @escaping (PairingCredentials) throws -> HubTransport = {
                    try .urlSession(origin: $0.origin, token: $0.deviceToken)
                }, makePaths: @escaping () -> HubNetworkPaths? = { HubNetworkPaths() },
                talk: TalkController? = nil, playback: SpeechPlayer? = nil, liveMode: LiveModeController? = nil,
                makeOutboxTransport: (@MainActor () -> any OutboxTransport)? = nil,
                outboxDirectory: URL? = nil,
                noticeSleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                orbNow: @escaping @MainActor () -> Double = { ProcessInfo.processInfo.systemUptime },
                orbSleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                stopPlayback: @escaping @MainActor () -> Void = {},
                sendCommand: @escaping @Sendable (HubClient, HubCommand) async throws -> Void = { try await $0.send($1) }) {
        self.voiceStore = voiceStore
            ?? historyDirectory.map { VoiceSettingStore(directory: $0.appendingPathComponent("Voice")) }
            ?? (makeClient == nil ? .applicationStore : nil)
        self.store = store; self.service = service; self.makeClient = makeClient; self.makePaths = makePaths
        self.sendCommand = sendCommand
        self.historyDirectory = historyDirectory; self.historyNow = historyNow; self.makeTransport = makeTransport
        let audioSession = NativeTalkAudioSession()
        let liveCapture = NativeLiveCaptureSource(session: audioSession)
        self.playback = playback ?? SpeechPlayer(session: audioSession,
                                                 output: LiveSpeechRoute(standard: NativeSpeechOutput(), capture: liveCapture))
        self.talk = talk ?? TalkController(recorder: NativeTalkRecorder(session: audioSession), permission: { await NativeTalkRecorder.requestPermission() })
        self.liveMode = liveMode ?? LiveModeController(capture: liveCapture,
                                                       permission: { await NativeTalkRecorder.requestPermission() }, sleep: noticeSleep)
        // An injected Client means a test: its Outbox must never reach a real network or the app's folders.
        let background: @MainActor () -> any OutboxTransport = { BackgroundOutboxTransport.shared }
        let held: @MainActor () -> any OutboxTransport = { HeldOutboxTransport() }
        self.makeOutboxTransport = makeOutboxTransport ?? (makeClient == nil ? background : held)
        self.outboxDirectory = outboxDirectory ?? historyDirectory?.appendingPathComponent("Outbox")
            ?? (makeClient == nil ? OutboxStore.applicationDirectory
                : FileManager.default.temporaryDirectory.appendingPathComponent("JarvisOutbox-\(UUID().uuidString)"))
        self.noticeSleep = noticeSleep
        self.orbNow = orbNow; self.orbSleep = orbSleep
        self.stopPlayback = stopPlayback
        self.talk.microphoneHeld = { [weak liveMode = self.liveMode] in liveMode?.talkHoldsMicrophone($0) }
        self.playback.configure(report: { [weak self] command in
            guard let self, let client = self.client else { return }
            if command.payload["state"] == .string("started"), command.payload["speech_id"] == self.speechID.map(JSONValue.string) {
                self.talk.speaking = true
            }
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

    private func startOrbFrames() {
        guard orbFrames == nil, foreground, credentials != nil else { return }
        orbFrames = Task { [weak self, orbSleep] in
            while !Task.isCancelled {
                do { try await orbSleep(1.0 / 60) } catch { return }
                guard !Task.isCancelled else { return }
                guard let self else { return }
                self.sampleOrb()
            }
        }
    }

    private func sampleOrb() {
        let live = liveMode.isOn ? liveMode.voiceState : nil
        if talk.state == .recording {
            orb.update(speaker: .user, target: OrbModel.loudness(rms: talk.level))
        } else if live == .listening || live == .followUp {
            orb.update(speaker: .user, target: OrbModel.loudness(rms: liveMode.level))
        } else if playback.playingSpeechID != nil && !playback.playingCue || live == .speaking {
            orb.update(speaker: .jarvis, target: OrbModel.loudness(rms: playback.level))
        } else if live != nil {
            orb.update(speaker: .idle, target: 0)
        } else if !voiceOn, let reply = streamingReply {
            let time = max(0, orbNow() - reply.started)
            let target = abs(sin(7 * time) * sin(2.3 * time)) * (0.6 + 0.4 * abs(sin(0.9 * time)))
            orb.update(speaker: .jarvis, target: target)
        } else {
            orb.update(speaker: .idle, target: 0)
        }
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

    public func takeFocus() async {
        guard !focusPending else { return }
        guard connection.canSend, let client else { focusMessage = "Couldn't reach the Mac. Not changed."; return }
        let owner = generation, command = HubCommand(type: "take_focus")
        focusCommand = command; focusPending = true; focusMessage = "Waiting for the Mac to confirm."
        do { try await sendCommand(client, command) }
        catch {
            let delivery = await client.delivery(of: command.id)
            guard generation == owner, self.client === client, focusCommand?.id == command.id else { return }
            switch delivery {
            case .pending: focusMessage = "Focus is not confirmed. Waiting for the Mac to reconcile it."
            case .completed(let result): receive(.result(result))
            case .absent:
                focusCommand = nil; focusPending = false
                focusMessage = "Couldn't reach the Mac. Not changed."
            }
        }
    }

    public func resetContext() async {
        guard contextReset != .resetting, connection.canSend, let client else { return }
        let command = HubCommand(type: "reset_context")
        resetCommand = command; contextReset = .resetting
        let owner = generation
        do { try await sendCommand(client, command) }
        catch {
            let delivery = await client.delivery(of: command.id)
            guard generation == owner, self.client === client, resetCommand?.id == command.id else { return }
            switch delivery {
            case .pending: break
            case .completed(let result): receive(.result(result))
            case .absent:
                resetCommand = nil
                contextReset = Self.neverSent(error) ? .notReset : .unsure
            }
        }
    }

    /// Whether a send failed before writing anything; a failure once the write began may have reached the Hub.
    private static func neverSent(_ error: any Error) -> Bool {
        guard let error = error as? HubProtocolError else { return false }
        return [.notConnected, .invalidFrame, .commandIDConflict].contains(error)
    }

    /// A reset's status line lasts until the User leaves Settings; one still waiting keeps its result for the next visit.
    public func leaveSettings() {
        if contextReset != .resetting { contextReset = .idle }
    }

    public func setVoiceOn(_ value: Bool) async {
        guard let credentials, let client else { return }
        let silence = !value, owner = generation
        voiceRevision &+= 1
        let revision = voiceRevision
        voiceOn = value; explicitSilence = silence; voicePending = true
        if silence { silencePlayback() }
        voicePersistenceFailed = false
        do { try voiceStore?.save(silence, for: credentials) }
        catch { voicePersistenceFailed = true; voiceMessage = "Voice changed for this session, but could not be saved. Try again." }
        guard generation == owner, self.client === client else { return }
        await client.setSilence(silence, revision: revision)
    }

    /// Silence stops speech only; a cue playing goes on.
    public func silencePlayback() {
        let cue = playback.playingCue
        playback.silence()
        if !cue { stopPlayback(); talk.speaking = false }
    }

    public func replay(replyID: String) async {
        guard voiceOn, connection.canSend, let client else { return }
        let owner = generation
        let command = replayCommands[replyID] ?? HubCommand(type: "replay_speech", payload: ["reply_id": .string(replyID)])
        replayCommands[replyID] = command; replayMessages[replyID] = nil
        do { try await sendCommand(client, command) }
        catch {
            guard generation == owner, self.client === client, replayCommands[replyID]?.id == command.id else { return }
            replayMessages[replyID] = "Playback may not have reached the Mac. Tap Play to retry the same request."
        }
    }

    public func loadOlderHistory() async {
        guard !loadingHistory, hasOlderHistory, connection.canSend, let client else { return }
        let owner = generation, history = history
        loadingHistory = true; historyMessage = nil
        defer { if generation == owner, self.client === client { loadingHistory = false } }
        do {
            let page = try await client.history(before: historyBefore)
            guard generation == owner, self.client === client else { return }
            if page.has_more {
                guard let next = page.next_before, let first = page.events.first,
                      next <= first.seq, historyBefore.map({ next < $0 }) ?? true else {
                    throw HubProtocolError.invalidSequence
                }
            }
            try await history?.merge(page.events)
            guard generation == owner, self.client === client else { return }
            conversation.ingest(contentsOf: page.events)
            historyBefore = page.next_before
            hasOlderHistory = page.has_more
        } catch {
            guard generation == owner, self.client === client else { return }
            historyMessage = "Couldn't load older history. Try again when the Mac is reachable."
        }
    }

    public func showError(_ error: PairingError) { self.error = error }

    public func audioInterrupted(_ began: Bool) {
        liveMode.audioInterrupted(began)
        if began, liveMode.isOn {
            playback.stop(); stopPlayback(); talk.speaking = false
            let owner = generation
            Task { if generation == owner { await talk.cancel() } }
        }
    }

    /// Invalidates in-flight pairing before clearing the sole durable credential record.
    public func unpair() async {
        generation &+= 1
        liveMode.unpair()
        let owner = generation, old = client, oldHistory = history
        let oldCredentials = credentials
        history = nil
        updates?.cancel(); updates = nil; paths?.cancel(); paths = nil
        orbFrames?.cancel(); orbFrames = nil; orb.reset(); streamingReply = nil
        setConnection(.unpaired); speechID = nil; playback.stop(reportStopped: false); stopPlayback()
        playbackDelivery?.cancel(); playbackDelivery = nil; pendingPlaybackDrain = nil
        outbox?.retire(); outbox = nil
        var historyClearFailed = false
        do { try await oldHistory?.clear() } catch { historyClearFailed = true }
        guard generation == owner, client === old else { await old?.stop(); return }
        await talk.cancel()
        guard generation == owner, client === old else { await old?.stop(); return }
        // Foreground can restart metering while Talk cancellation waits.
        orbFrames?.cancel(); orbFrames = nil
        client = nil; credentials = nil; pairing = false; loading = false
        do {
            if let oldCredentials { try voiceStore?.clear(oldCredentials) }
        } catch { historyClearFailed = true }
        focusHere = false; focusPending = false; focusCommand = nil; focusMessage = nil
        contextReset = .idle; resetCommand = nil
        voiceOn = true; explicitSilence = nil; voicePending = false; voiceMessage = nil; voicePersistenceFailed = false; voiceRevision = 0
        do { try store.clear(); error = historyClearFailed ? .storage : nil } catch { self.error = .storage }
        tasksNeedRefresh = true; tasksReplaying = true
        conversation = ConversationProjection(); historyBefore = nil; hasOlderHistory = true; loadingHistory = false; historyMessage = nil; draft = ""; inputMessage = nil
        playback.reset(); replayCommands = [:]; replayMessages = [:]
        await old?.stop()
    }

    private func setConnection(_ state: HubConnectionState) {
        connection = state
        if !state.canSend { streamingReply = nil }
        let blip: [HubConnectionState] = [.connecting, .unreachable, .hubStopped, .suspended]
        guard shownConnection == .ready, blip.contains(state) else {
            connectionNotice?.cancel(); connectionNotice = nil
            shownConnection = state
            return
        }
        // One timer for the whole outage: retries switching between connecting and unreachable must not restart it.
        guard connectionNotice == nil else { return }
        connectionNotice = Task { [weak self, noticeSleep] in
            do { try await noticeSleep(3) } catch { return }
            guard let self, !Task.isCancelled else { return }
            self.connectionNotice = nil
            if self.connection != .ready { self.shownConnection = self.connection }
        }
    }

    public func setForeground(_ value: Bool) async {
        foreground = value
        liveMode.setForeground(value)
        if value { startOrbFrames() }
        else { orbFrames?.cancel(); orbFrames = nil; orb.reset(); streamingReply = nil }
        foregroundGeneration &+= 1
        let transition = foregroundGeneration, owner = generation, client = client
        if !value {
            await talk.cancel(); outbox?.prepareForBackground()
            // A failed save is kept by the store, so the next commit retries it.
            try? await history?.flush()
        } else { outbox?.nudge() }
        guard foregroundGeneration == transition, generation == owner, self.client === client else { return }
        let playing = playback.activeSpeechID ?? pendingPlaybackDrain?.speechID ?? backgroundSpeechID
        backgroundSpeechID = value ? nil : playing
        if !value && playing == nil { playback.stop(reportStopped: false) }
        await client?.setForeground(value, playingSpeechID: value ? nil : playing, keepListening: liveMode.isOn)
    }

    private func connect(_ credentials: PairingCredentials, owner: UInt64) async throws {
        guard generation == owner else { return }
        var cached = CachedHistory()
        var history: HistoryStore?
        if makeClient == nil || historyDirectory != nil {
            let directory = historyDirectory ?? HistoryStore.applicationDirectory
            let candidate = HistoryStore(directory: directory, credentials: credentials, now: historyNow)
            self.history = candidate
            do { cached = try await candidate.load(); history = candidate }
            catch {
                guard generation == owner else { return }
                historyMessage = "Saved history could not be read. Fetching it again from the Mac."
                try await candidate.clear()
                history = HistoryStore(directory: directory, credentials: credentials, now: historyNow)
            }
            guard generation == owner else { return }
        }
        explicitSilence = try voiceStore?.load(credentials)
        voiceOn = !(explicitSilence ?? false); voicePending = explicitSilence != nil
        let client: HubClient
        if let makeClient { client = try makeClient(credentials) }
        else {
            let cache = history
            client = HubClient(transport: try makeTransport(credentials), cursor: cached.cursor,
                               cache: { event, cursor in try await cache?.commit(event, through: cursor) })
        }
        if let explicitSilence { await client.setSilence(explicitSilence, revision: voiceRevision) }
        guard generation == owner else { await client.stop(); return }
        self.history = history
        conversation.ingest(contentsOf: cached.events)
        historyBefore = cached.events.first?.seq
        for row in conversation.rows where row.id.hasPrefix("reply:") {
            playback.markMissed(replyID: String(row.id.dropFirst(6)))
        }
        self.credentials = credentials; self.client = client; setConnection(.connecting)
        startOrbFrames()
        let outbox = Outbox(store: OutboxStore(directory: outboxDirectory, credentials: credentials),
                            transport: makeOutboxTransport(), credentials: credentials, now: historyNow)
        self.outbox = outbox
        talk.configure(begin: { [weak self] id in
            guard let self, self.client === client, self.generation == owner else { return false }
            return await client.sendLive(HubCommand(type: "talk_begin", payload: ["talk_id": .string(id)]))
        }, cancel: { id in
            _ = await client.sendLive(HubCommand(type: "talk_cancel", payload: ["talk_id": .string(id)]))
        }, deliver: { [weak self] id, pcm, recordedAt in
            guard let self, self.generation == owner else { return }
            guard outbox.hasPending else { return outbox.enqueueTalk(talkID: id, pcm: pcm, recordedAt: recordedAt) }
            // Waiting behind older messages, this Talk would hold the Hub's floor and the Mac's microphone; it goes as a queued Talk.
            Task { _ = await client.sendLive(HubCommand(type: "talk_cancel", payload: ["talk_id": .string(id)])) }
            outbox.enqueueTalk(talkID: UUID().uuidString, pcm: pcm, recordedAt: recordedAt)
        }, stopSpeech: { [weak self] in await self?.interruptSpeech() })
        liveMode.configure(send: { [weak self] command in
            guard let self, self.client === client, self.generation == owner else { throw HubProtocolError.notConnected }
            try await self.sendCommand(client, command)
        }, sendAudio: { await client.sendAudio($0) }, permissionAnswered: { [weak self] in self?.talk.microphonePermission(granted: $0) },
        captureFailed: { [weak self] in
            guard let self, self.client === client, self.generation == owner else { return }
            self.playback.stop(reportStopped: false); self.stopPlayback()
            self.speechID = nil; self.talk.speaking = false
        })
        await outbox.resume()
        guard generation == owner else { await client.stop(); return }
        paths = makePaths()
        updates = Task { [weak self] in
            for await update in client.updates {
                guard !Task.isCancelled, let self, self.generation == owner, self.client === client else { return }
                if case .state(let state) = update {
                    if state == .unpaired { await self.unpair(); return }
                    self.setConnection(state)
                    await self.liveMode.connectionChanged(ready: state.canSend)
                    if state.canSend { self.outbox?.nudge() }
                } else {
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
