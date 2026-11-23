import Foundation

public enum JSONValue: Codable, Equatable, Sendable {
    case null, bool(Bool), integer(Int64), number(Double), string(String)
    case array([JSONValue]), object([String: JSONValue])

    public init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int64.self) { self = .integer(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .integer(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

public struct HubError: Codable, Equatable, Sendable, Error {
    public let code: String
    public let message: String
    public let retry_after_seconds: Int?
}

public enum HubProtocolError: Error, Equatable, Sendable {
    case invalidFrame, unsupportedVersion, invalidSequence, invalidTime, malformedAudio
    case notConnected, commandIDConflict, invalidOrigin, authentication, handshake
}

public struct HubCommand: Codable, Equatable, Sendable {
    public let version: Int
    public let id: String
    public let type: String
    public let payload: [String: JSONValue]

    public init(id: String = UUID().uuidString, type: String, payload: [String: JSONValue] = [:]) {
        self.version = 1; self.id = id; self.type = type; self.payload = payload
    }
}

public struct CommandResult: Decodable, Equatable, Sendable {
    public let id: String?
    public let ok: Bool
    public let result: [String: JSONValue]?
    public let error: HubError?
}

public struct ReplayComplete: Decodable, Equatable, Sendable {
    public let through_seq: Int64
    public let oldest_seq: Int64?
    public let history_pruned: Bool?
}

public struct HubStatus: Decodable, Equatable, Sendable {
    public struct UsageLimit: Decodable, Equatable, Sendable {
        public let limited: Bool
        public let resets_at: String?
    }
    public let ready: Bool
    public let usage_limit: UsageLimit
    public let quiet: Bool
    public let focus_client_id: String?
    public let mac_voice_on: Bool
    public let silence: Bool
}

public struct TaskSnapshot: Decodable, Equatable, Sendable {
    public struct Task: Decodable, Equatable, Sendable {
        public let id: String
        public let title: String
        public let state: String
        public let last_report: String?
        public let approval_question: String?
    }
    public let tasks: [Task]
}

public struct PendingApprovals: Decodable, Equatable, Sendable {
    public struct Approval: Decodable, Equatable, Sendable {
        public let id: String
        public let task_id: String
        public let question: String
        public let expires_at: String
    }
    public let approvals: [Approval]
}

public struct Heard: Decodable, Equatable, Sendable {
    public let request_id: String
    public let text: String
    public let spoken: Bool
}

public struct Reply: Decodable, Equatable, Sendable {
    public let reply_id: String
    public let request_id: String?
    public let text: String
    public let reason: String?
}

public struct Notice: Decodable, Equatable, Sendable {
    public let text: String
    public let count: Int?
    public let task_ids: [String]?
}

public struct ShownText: Decodable, Equatable, Sendable {
    public let reply_id: String?
    public let title: String
    public let text: String
}

/// A reply's speech names its `reply_id`; a Live mode cue, the chime or the closing sound, names its `cue` instead.
public struct SpeechBegin: Decodable, Equatable, Sendable {
    public let speech_id: String
    public let reply_id: String?
    public let cue: String?
    public let sample_rate: Int
    public let channels: Int
    public let sample_width: Int
}

public struct SpeechClip: Decodable, Equatable, Sendable {
    public let speech_id: String
    public let reply_id: String?
    public let cue: String?
    public let index: Int64
    public let text: String
    public let byte_length: Int64
}

public struct SpeechEnd: Decodable, Equatable, Sendable {
    public let speech_id: String
    public let reply_id: String?
    public let cue: String?
    public let last_clip_index: Int64
}

public struct SpeechStop: Decodable, Equatable, Sendable {
    public let speech_id: String
    public let reply_id: String
    public let reason: String
}

/// A state this release doesn't know reads as `idle`, so a newer Hub cannot close the connection with one.
public struct VoiceStatePayload: Decodable, Equatable, Sendable {
    public let state: String
}

public struct HubEvent: Codable, Equatable, Sendable {
    public enum Kind: Equatable, Sendable {
        case heard, replyDelta, replyEnd, notice, show, taskSnapshot, pendingApproval, status
        case speechBegin, speechClip, speechEnd, speechStop, voiceState, unknown(String)
    }
    public let seq: Int64
    public let time: String
    public let type: String
    public let client_id: String?
    public let payload: [String: JSONValue]

    public var kind: Kind {
        switch type {
        case "heard": .heard
        case "reply_delta": .replyDelta
        case "reply_end": .replyEnd
        case "notice": .notice
        case "show": .show
        case "task_snapshot": .taskSnapshot
        case "pending_approval": .pendingApproval
        case "status": .status
        case "speech_begin": .speechBegin
        case "speech_clip": .speechClip
        case "speech_end": .speechEnd
        case "speech_stop": .speechStop
        case "voice_state": .voiceState
        default: .unknown(type)
        }
    }

    public var replayable: Bool {
        !["speech_begin", "speech_clip", "speech_end", "speech_stop", "voice_state"].contains(type)
    }

    public func decodePayload<T: Decodable>(_ type: T.Type) throws -> T {
        try JSONDecoder().decode(type, from: JSONEncoder().encode(payload))
    }

    public func string(_ key: String) -> String? {
        if case .string(let value) = payload[key] { return value }; return nil
    }

    public func integer(_ key: String) -> Int64? {
        if case .integer(let value) = payload[key] { return value }; return nil
    }
}

public enum HubFrame: Equatable, Sendable {
    case event(HubEvent), replayComplete(ReplayComplete), commandResult(CommandResult), error(HubError)

    public static func decode(_ data: Data) throws -> HubFrame {
        struct Envelope: Decodable { let version: Int; let type: String?; let error: HubError? }
        let decoder = JSONDecoder()
        let envelope = try decoder.decode(Envelope.self, from: data)
        guard envelope.version == 1 else { throw HubProtocolError.unsupportedVersion }
        switch envelope.type {
        case "replay_complete":
            let replay = try decoder.decode(ReplayComplete.self, from: data)
            guard replay.through_seq >= 0, replay.oldest_seq.map({ $0 >= 0 }) ?? true else { throw HubProtocolError.invalidSequence }
            return .replayComplete(replay)
        case "command_result":
            let result = try decoder.decode(CommandResult.self, from: data)
            guard result.ok ? result.result != nil : result.error != nil else { throw HubProtocolError.invalidFrame }
            return .commandResult(result)
        case nil:
            guard let error = envelope.error else { throw HubProtocolError.invalidFrame }
            return .error(error)
        default:
            let event = try decoder.decode(HubEvent.self, from: data)
            guard event.seq >= 0 else { throw HubProtocolError.invalidSequence }
            guard utcDate(event.time) != nil else { throw HubProtocolError.invalidTime }
            switch event.kind {
            case .heard: _ = try event.decodePayload(Heard.self)
            case .replyDelta: _ = try event.decodePayload(Reply.self)
            case .replyEnd:
                let reply = try event.decodePayload(Reply.self)
                guard reply.reason != nil else { throw HubProtocolError.invalidFrame }
            case .notice: _ = try event.decodePayload(Notice.self)
            case .show: _ = try event.decodePayload(ShownText.self)
            case .speechBegin:
                let begin = try event.decodePayload(SpeechBegin.self)
                guard (begin.reply_id == nil) != (begin.cue == nil) else { throw HubProtocolError.invalidFrame }
            case .speechClip: _ = try event.decodePayload(SpeechClip.self)
            case .speechEnd: _ = try event.decodePayload(SpeechEnd.self)
            case .speechStop: _ = try event.decodePayload(SpeechStop.self)
            case .voiceState: _ = try event.decodePayload(VoiceStatePayload.self)
            case .status:
                let status = try event.decodePayload(HubStatus.self)
                if let time = status.usage_limit.resets_at, utcDate(time) == nil { throw HubProtocolError.invalidTime }
            case .taskSnapshot: _ = try event.decodePayload(TaskSnapshot.self)
            case .pendingApproval:
                let approvals = try event.decodePayload(PendingApprovals.self)
                guard approvals.approvals.allSatisfy({ utcDate($0.expires_at) != nil }) else { throw HubProtocolError.invalidTime }
            default: break
            }
            return .event(event)
        }
    }
}

/// Reads the Hub's UTC times, with or without fractional seconds.
public func utcDate(_ string: String) -> Date? {
    guard string.hasSuffix("Z") || string.hasSuffix("+00:00") else { return nil }
    return try? utcTimeStyle.parse(string)
}

// A format style parses in under a microsecond; a new ISO8601DateFormatter per call costs about 0.25 ms.
private let utcTimeStyle = Date.ISO8601FormatStyle(includingFractionalSeconds: true)

public struct HistoryPage: Decodable, Sendable {
    public let version: Int
    public let events: [HubEvent]
    public let next_before: Int64?
    public let has_more: Bool
}
