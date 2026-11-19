import Foundation

public enum SocketMessage: Sendable { case text(Data), binary(Data) }

public protocol HubSocket: Sendable {
    func receive() async throws -> SocketMessage
    func send(_ data: Data) async throws
    func close() async
    func closeForProtocolError() async
}

public extension HubSocket {
    func closeForProtocolError() async { await close() }
}

public struct HubTransport: Sendable {
    public let connect: @Sendable (Int64) async throws -> any HubSocket
    public let request: @Sendable (String) async throws -> Data

    public init(connect: @escaping @Sendable (Int64) async throws -> any HubSocket,
                request: @escaping @Sendable (String) async throws -> Data = { _ in throw HubProtocolError.notConnected }) {
        self.connect = connect; self.request = request
    }
}

public enum HubTransportError: Error, Equatable, Sendable {
    case http(Int), unauthorized, incompatibleProtocol
}

public enum HubClientUpdate: Sendable {
    case state(HubConnectionState), event(HubEvent), audio(HubEvent, Data), result(CommandResult)
    case failure(String), replay(ReplayComplete), speechConnectionEnded(String)
}

public actor HubClient {
    public private(set) var state: HubConnectionState = .connecting
    public private(set) var cursor: Int64
    public private(set) var pendingCommands: [HubCommand] = []
    public private(set) var unansweredCommands: Set<String> = []
    public private(set) var results: [String: CommandResult] = [:]
    public nonisolated let updates: AsyncStream<HubClientUpdate>
    private let continuation: AsyncStream<HubClientUpdate>.Continuation
    private let transport: HubTransport
    private let sleep: @Sendable (Double) async throws -> Void
    private let acknowledgementSleep: @Sendable (Double) async throws -> Void
    private let lifecycleSleep: @Sendable (Double) async throws -> Void
    private let jitter: @Sendable () -> Double
    private let cache: @Sendable (HubEvent?, Int64) async throws -> Void
    private var socket: (any HubSocket)?
    private var retryTimers: [String: (UUID, Task<Void, Never>)] = [:]
    private var pendingData: [String: Data] = [:]
    private var task: Task<Void, Never>?
    private var generation: UInt64 = 0
    private var foreground = true
    private var running = false
    private var playing = false
    private var activeSpeechID: String?
    private var drainingSpeechID: String?
    private var pathTask: Task<Void, Never>?
    private var handover = false
    private var displayReady = false
    private var presenceDesired = false
    private var status: HubStatus?
    private var presenceID: String?
    private var presenceWaiter: CheckedContinuation<Void, Never>?
    private var presenceTimeout: Task<Void, Never>?
    private var finishingTalks: [String: CheckedContinuation<Bool, Never>] = [:]
    private var talkDeadlines: [String: Task<Void, Never>] = [:]

    /// The cache closure must finish durable storage before returning. A nil event commits a replay watermark.
    public init(transport: HubTransport, cursor: Int64 = 0,
                sleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                lifecycleSleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                acknowledgementSleep: @escaping @Sendable (Double) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
                jitter: @escaping @Sendable () -> Double = { Double.random(in: 0.8...1.2) },
                cache: @escaping @Sendable (HubEvent?, Int64) async throws -> Void = { _, _ in }) {
        self.transport = transport; self.cursor = max(0, cursor); self.sleep = sleep
        self.jitter = jitter; self.cache = cache; self.lifecycleSleep = lifecycleSleep; self.acknowledgementSleep = acknowledgementSleep
        (updates, continuation) = AsyncStream.makeStream()
    }

    public func start(paths: AsyncStream<Void>? = nil) async {
        running = true
        if let paths {
            pathTask?.cancel()
            pathTask = Task {
                for await _ in paths {
                    guard !Task.isCancelled else { return }
                    await self.pathChanged()
                }
            }
        }
        if foreground { await replace() }
    }

    public func stop() async {
        running = false
        pathTask?.cancel(); pathTask = nil
        let owner = await retire()
        if generation == owner { setState(.suspended) }
    }

    public func pathChanged() async {
        if running && foreground { await replace() }
    }

    /// Call before surrendering foreground time. Only already-playing audio may drain in the background.
    public func setForeground(_ value: Bool, playingSpeechID: String? = nil) async {
        let resumePlaying = value && !foreground && playing && drainingSpeechID == activeSpeechID && socket != nil && handover
        foreground = value
        playing = resumePlaying || playingSpeechID != nil && playingSpeechID == activeSpeechID
        drainingSpeechID = value ? nil : playingSpeechID
        if !value { cancelRetries(); setState(.suspended) }
        if value {
            if resumePlaying, let socket { await setPresence(true, socket: socket) }
            else if running { await replace() }
        } else if playing, displayReady, let socket {
            await setPresence(false, socket: socket)
        } else { await retire() }
    }

    private func setPresence(_ value: Bool, socket: any HubSocket) async {
        presenceTimeout?.cancel()
        presenceWaiter?.resume(); presenceWaiter = nil
        let command = HubCommand(type: "set_presence", payload: ["can_show_text": .bool(value)])
        presenceID = command.id; presenceDesired = value; displayReady = false
        let owner = generation
        await withCheckedContinuation { waiter in
            presenceWaiter = waiter
            startPresenceDeadline(command.id, owner: owner)
            Task {
                guard generation == owner, presenceID == command.id else { return }
                do { try await socket.send(JSONEncoder().encode(command)) }
                catch { await presenceExpired(command.id, owner: owner) }
            }
        }
    }

    public func playbackDrained(speechID: String) async {
        guard speechID == drainingSpeechID || foreground && speechID == activeSpeechID else { return }
        playing = false; drainingSpeechID = nil
        if !foreground { await retire() }
    }

    public func send(_ command: HubCommand) async throws {
        if command.type == "talk_cancel" {
            guard running else { throw HubProtocolError.notConnected }
            let data = try encodedCommand(command)
            try retain(command, data: data)
            refreshState()
            // Cancellation survives capture's socket, including a disconnect before local retirement.
            guard foreground, handover, displayReady, let socket else { return }
            let owner = generation
            do {
                try await socket.send(pendingData[command.id] ?? data)
                if generation == owner { startRetryDeadline(command.id, owner: owner) }
            } catch {
                if generation == owner { await socket.close() }
                throw error
            }
            return
        }
        guard running, foreground, handover, displayReady, state.canSend, let socket else { throw HubProtocolError.notConnected }
        let data = try encodedCommand(command)
        try retain(command, data: data)
        // A successful send only reaches the local kernel. Keep the original command until its result.
        let owner = generation
        do {
            try await socket.send(pendingData[command.id] ?? data)
            if generation == owner { startRetryDeadline(command.id, owner: owner) }
        } catch {
            if generation == owner { await socket.close() }
            throw error
        }
    }

    /// A released capture may await its existing write, but cannot replay a begin after reconnecting.
    public func finishTalkBegin(id: String) async -> Bool {
        if let result = results[id] { return result.ok }
        guard pendingCommands.contains(where: { $0.id == id && $0.type == "talk_begin" }), socket != nil, state.canSend else { return false }
        removeTalkRetry(id)
        return await withCheckedContinuation { waiter in
            finishingTalks[id] = waiter
            talkDeadlines[id] = Task {
                do { try await lifecycleSleep(2) } catch { return }
                await retireTalkBegin(id: id)
            }
        }
    }

    private func removeTalkRetry(_ id: String) {
        pendingCommands.removeAll { $0.id == id }
        pendingData.removeValue(forKey: id)
        retryTimers.removeValue(forKey: id)?.1.cancel()
        unansweredCommands.remove(id)
    }

    private func retireTalkBegins() {
        for command in pendingCommands where command.type == "talk_begin" { removeTalkRetry(command.id) }
        for timer in talkDeadlines.values { timer.cancel() }; talkDeadlines.removeAll()
        for waiter in finishingTalks.values { waiter.resume(returning: false) }; finishingTalks.removeAll()
    }

    /// End capture intent without allowing a lost begin acknowledgement to become a later Talk.
    public func retireTalkBegin(id: String) async {
        let uncertain = pendingCommands.contains(where: { $0.id == id && $0.type == "talk_begin" }) || finishingTalks[id] != nil
        removeTalkRetry(id)
        talkDeadlines.removeValue(forKey: id)?.cancel()
        finishingTalks.removeValue(forKey: id)?.resume(returning: false)
        // Closing also releases a floor admitted by the Hub whose acknowledgement was lost.
        if uncertain {
            if running && foreground { await replace() }
            else { await retire() }
        }
    }

    public func reportPlayback(_ command: HubCommand) async throws {
        guard command.type == "playback_report", handover, let socket,
              case .string(let speechID) = command.payload["speech_id"],
              foreground ? speechID == activeSpeechID : playing && speechID == drainingSpeechID else { throw HubProtocolError.notConnected }
        guard results[command.id] == nil else { return }
        let data = try encodedCommand(command)
        try retain(command, data: data)
        let owner = generation
        do {
            try await socket.send(pendingData[command.id] ?? data)
            if generation == owner { startRetryDeadline(command.id, owner: owner) }
        } catch {
            if generation == owner { await socket.close() }
            throw error
        }
    }

    private func startRetryDeadline(_ id: String, owner: UInt64) {
        guard pendingData[id] != nil else { return }
        retryTimers.removeValue(forKey: id)?.1.cancel()
        let token = UUID()
        let timer = Task {
            for attempt in 0..<3 {
                guard !Task.isCancelled else { return }
                do { try await acknowledgementSleep(5) } catch { return }
                guard await retryUnanswered(id, owner: owner, token: token, expired: attempt == 2) else { return }
            }
        }
        retryTimers[id] = (token, timer)
    }

    private func retryUnanswered(_ id: String, owner: UInt64, token: UUID, expired: Bool) async -> Bool {
        guard generation == owner, retryTimers[id]?.0 == token, let data = pendingData[id],
              handover, let socket, foreground || pendingCommands.contains(where: {
                  $0.id == id && $0.type == "playback_report" && playing
              }) else { return false }
        if expired {
            retryTimers.removeValue(forKey: id)
            unansweredCommands.insert(id)
            continuation.yield(.failure("Command \(id) has no acknowledgement. Its outcome is still unknown; check history before sending a new request."))
            return false
        }
        do { try await socket.send(data); return true }
        catch {
            if generation == owner { await socket.close() }
            return false
        }
    }

    private func cancelRetries() {
        for (_, timer) in retryTimers.values { timer.cancel() }
        retryTimers.removeAll()
    }

    private func encodedCommand(_ command: HubCommand) throws -> Data {
        guard command.version == 1, !command.id.isEmpty, command.id.utf8.count <= 256,
              command.id.unicodeScalars.allSatisfy({ $0.value >= 32 }),
              !command.type.isEmpty, command.type.utf8.count <= 128 else { throw HubProtocolError.invalidFrame }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(command)
        guard data.count <= 65_536 else { throw HubProtocolError.invalidFrame }
        return data
    }

    private func retain(_ command: HubCommand, data: Data) throws {
        guard results[command.id] == nil else { throw HubProtocolError.commandIDConflict }
        if let existing = pendingCommands.first(where: { $0.id == command.id }) {
            guard existing == command else { throw HubProtocolError.commandIDConflict }
        } else {
            pendingCommands.append(command)
            pendingData[command.id] = data
        }
    }

    private func startPresenceDeadline(_ id: String, owner: UInt64) {
        presenceTimeout?.cancel()
        presenceTimeout = Task {
            guard !Task.isCancelled else { return }
            do { try await lifecycleSleep(2) } catch { return }
            await presenceExpired(id, owner: owner)
        }
    }

    private func presenceExpired(_ id: String, owner: UInt64) async {
        guard presenceID == id, generation == owner else { return }
        if foreground, running, presenceDesired {
            presenceID = nil
            continuation.yield(.failure("The Hub did not acknowledge display presence. Reconnecting."))
            await socket?.close()
        } else { await retire() }
    }

    public func history(before: Int64? = nil, limit: Int = 100) async throws -> HistoryPage {
        let path = "/v1/history?limit=\(min(500, max(1, limit)))" + (before.map { "&before=\($0)" } ?? "")
        let data = try await transport.request(path)
        let page = try JSONDecoder().decode(HistoryPage.self, from: data)
        guard page.version == 1 else { throw HubProtocolError.unsupportedVersion }
        var prior: Int64 = -1
        for event in page.events {
            guard event.seq > prior, event.replayable, before.map({ event.seq < $0 }) ?? true else { throw HubProtocolError.invalidSequence }
            _ = try HubFrame.decode(JSONEncoder().encode(EventEncoding(event: event)))
            prior = event.seq
        }
        return page
    }

    @discardableResult
    private func retire() async -> UInt64 {
        generation &+= 1
        let owner = generation
        task?.cancel(); task = nil
        cancelRetries()
        retireTalkBegins()
        if let activeSpeechID { continuation.yield(.speechConnectionEnded(activeSpeechID)) }
        let old = socket; socket = nil; handover = false; displayReady = false; presenceID = nil; activeSpeechID = nil
        presenceTimeout?.cancel(); presenceTimeout = nil
        presenceWaiter?.resume(); presenceWaiter = nil
        await old?.close()
        return owner
    }

    private func replace() async {
        let owner = await retire()
        guard generation == owner, running, foreground else { return }
        setState(.connecting)
        task = Task { await self.run(owner) }
    }

    private func setState(_ value: HubConnectionState) {
        state = value; continuation.yield(.state(value))
    }

    private func run(_ owner: UInt64) async {
        var failures = 0
        while running, foreground, generation == owner, !Task.isCancelled {
            var current: (any HubSocket)?
            do {
                let opened = try await transport.connect(cursor)
                current = opened
                guard generation == owner, foreground, running else { await opened.close(); return }
                socket = opened; handover = false; displayReady = false; status = nil
                var header: HubEvent?
                var speechID: String?
                var speechReplyID: String?
                var nextClip: Int64 = 0
                while generation == owner, !Task.isCancelled {
                    let message = try await opened.receive()
                    guard generation == owner else { return }
                    if let clip = header {
                        guard case .binary(let bytes) = message, clip.integer("byte_length") == Int64(bytes.count), bytes.count.isMultiple(of: 2) else {
                            throw HubProtocolError.malformedAudio
                        }
                        header = nil
                        continuation.yield(.audio(clip, bytes))
                        try await commit(clip, owner: owner)
                        continue
                    }
                    guard case .text(let bytes) = message else { throw HubProtocolError.malformedAudio }
                    let frame = try HubFrame.decode(bytes)
                    switch frame {
                    case .commandResult(let result):
                        if let presenceID, result.id == presenceID {
                            self.presenceID = nil
                            presenceTimeout?.cancel(); presenceTimeout = nil
                            let accepted = result.ok && result.result?["can_show_text"] == .bool(presenceDesired)
                            if !accepted { await retire(); return }
                            presenceWaiter?.resume(); presenceWaiter = nil
                            if presenceDesired {
                                guard foreground else { await retire(); return }
                                failures = 0
                                var resent: Set<String> = []
                                while let command = pendingCommands.first(where: { !resent.contains($0.id) }) {
                                    guard generation == owner, foreground else { return }
                                    guard let data = pendingData[command.id] else { throw HubProtocolError.invalidFrame }
                                    try await opened.send(data)
                                    resent.insert(command.id)
                                    if generation == owner { startRetryDeadline(command.id, owner: owner) }
                                }
                                guard generation == owner, foreground else { return }
                                displayReady = true; refreshState()
                            }
                        } else if let id = result.id, let waiter = finishingTalks.removeValue(forKey: id) {
                            talkDeadlines.removeValue(forKey: id)?.cancel()
                            results[id] = result; continuation.yield(.result(result)); waiter.resume(returning: result.ok)
                        } else if let id = result.id, pendingCommands.contains(where: { $0.id == id }) {
                            pendingCommands.removeAll { $0.id == id }
                            pendingData.removeValue(forKey: id)
                            retryTimers.removeValue(forKey: id)?.1.cancel()
                            unansweredCommands.remove(id)
                            results[id] = result
                            continuation.yield(.result(result))
                            refreshState()
                        }
                    case .error(let error): continuation.yield(.failure(error.message))
                    case .replayComplete(let replay):
                        guard !handover, replay.through_seq >= cursor else { throw HubProtocolError.invalidSequence }
                        try await cache(nil, replay.through_seq)
                        guard generation == owner else { return }
                        cursor = replay.through_seq; handover = true
                        continuation.yield(.replay(replay))
                        guard foreground else { await retire(); return }
                        let presence = HubCommand(type: "set_presence", payload: ["can_show_text": .bool(true)])
                        presenceID = presence.id; presenceDesired = true
                        startPresenceDeadline(presence.id, owner: owner)
                        try await opened.send(JSONEncoder().encode(presence))
                    case .event(let event):
                        guard event.seq > cursor else { continue }
                        if !event.replayable && !handover { throw HubProtocolError.invalidFrame }
                        switch event.kind {
                        case .speechBegin:
                            guard event.integer("sample_rate") == 24_000, event.integer("channels") == 1,
                                  event.integer("sample_width") == 2, let id = event.string("speech_id"),
                                  let reply = event.string("reply_id") else { throw HubProtocolError.malformedAudio }
                            if !foreground, id != drainingSpeechID { await retire(); return }
                            speechID = id; speechReplyID = reply; activeSpeechID = id; nextClip = 0
                        case .speechClip:
                            guard event.string("speech_id") == speechID, event.string("reply_id") == speechReplyID, event.integer("index") == nextClip,
                                  let length = event.integer("byte_length"), length >= 0,
                                  length.isMultiple(of: 2) else { throw HubProtocolError.malformedAudio }
                            nextClip += 1; header = event; continue
                        case .speechEnd:
                            guard event.string("speech_id") == speechID, event.string("reply_id") == speechReplyID, event.integer("last_clip_index") == nextClip - 1 else { throw HubProtocolError.malformedAudio }
                        case .speechStop:
                            guard event.string("speech_id") == speechID, event.string("reply_id") == speechReplyID else { throw HubProtocolError.malformedAudio }
                            speechID = nil; speechReplyID = nil
                        default: break
                        }
                        try await commit(event, owner: owner)
                        guard generation == owner else { return }
                        if event.kind == .status { status = try event.decodePayload(HubStatus.self); if handover { refreshState() } }
                        continuation.yield(.event(event))
                    }
                }
            } catch {
                guard generation == owner else { await current?.close(); return }
                cancelRetries()
                retireTalkBegins()
                if let activeSpeechID { continuation.yield(.speechConnectionEnded(activeSpeechID)) }
                socket = nil; handover = false; displayReady = false; presenceID = nil; activeSpeechID = nil
                presenceTimeout?.cancel(); presenceTimeout = nil
                presenceWaiter?.resume(); presenceWaiter = nil
                if error is DecodingError || (error as? HubProtocolError) == .malformedAudio {
                    await current?.closeForProtocolError()
                } else { await current?.close() }
                guard generation == owner else { return }
                if case HubTransportError.unauthorized = error {
                    running = false; setState(.unpaired); return
                }
                if (error as? HubProtocolError) == .unsupportedVersion || (error as? HubTransportError) == .incompatibleProtocol {
                    running = false; setState(.incompatibleProtocol); return
                }
                guard foreground else { setState(.suspended); return }
                if case HubTransportError.http(let code) = error, [502, 503, 504].contains(code) {
                    setState(.hubStopped)
                } else { setState(.unreachable) }
                continuation.yield(.failure("The connection ended. Unanswered commands retain their original IDs."))
                guard foreground, running else { return }
                let base = min(15.0, pow(2, Double(min(failures, 4))))
                failures += 1
                do { try await sleep(base * max(0.8, min(1.2, jitter()))) }
                catch { return }
            }
        }
    }

    private func commit(_ event: HubEvent, owner: UInt64) async throws {
        try await cache(event.replayable ? event : nil, event.seq)
        if generation == owner { cursor = max(cursor, event.seq) }
    }

    private func refreshState() {
        guard foreground else { setState(.suspended); return }
        guard displayReady else { return }
        guard !pendingCommands.contains(where: { $0.type == "talk_cancel" }) else { setState(.connecting); return }
        if let status {
            if status.usage_limit.limited { setState(.usageLimited(resetTime: status.usage_limit.resets_at)) }
            else { setState(status.ready ? .ready : .connecting) }
        } else { setState(.connecting) }
    }
}

private struct EventEncoding: Encodable {
    let event: HubEvent
    func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: Keys.self)
        try c.encode(1, forKey: .version); try c.encode(event.seq, forKey: .seq)
        try c.encode(event.time, forKey: .time); try c.encode(event.type, forKey: .type)
        try c.encodeIfPresent(event.client_id, forKey: .client_id); try c.encode(event.payload, forKey: .payload)
    }
    enum Keys: CodingKey { case version, seq, time, type, client_id, payload }
}
