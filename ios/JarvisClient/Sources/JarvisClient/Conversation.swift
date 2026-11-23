import Foundation

public struct ConversationRow: Identifiable, Equatable, Sendable {
    public struct Detail: Identifiable, Equatable, Sendable {
        public let id: Int64
        public let title: String
        public let text: String
    }
    public enum Speaker: Sendable { case user, jarvis, notice }
    public let id: String
    public let sequence: Int64
    public let time: String
    public let clientID: String?
    public let speaker: Speaker
    public var requestID: String?
    public var text: String
    public var streaming = false
    public var details: [Detail] = []
}

public struct ConversationProjection: Sendable {
    public private(set) var events: [HubEvent] = []
    public private(set) var rows: [ConversationRow] = []
    private var sequences: Set<Int64> = []
    private var positions: [String: Int] = [:]

    public init() {}

    public mutating func ingest(_ event: HubEvent) {
        guard event.replayable, sequences.insert(event.seq).inserted else { return }
        if let last = events.last, event.seq < last.seq {
            events.append(event); events.sort { $0.seq < $1.seq }
            rows = []; positions = [:]
            for event in events { apply(event) }
        } else { events.append(event); apply(event) }
    }

    public mutating func ingest(contentsOf events: [HubEvent]) {
        for event in events { ingest(event) }
    }

    private mutating func apply(_ event: HubEvent) {
        let id: String
        let speaker: ConversationRow.Speaker
        switch event.kind {
        case .heard: id = "request:\(event.string("request_id") ?? String(event.seq))"; speaker = .user
        case .replyDelta, .replyEnd: id = "reply:\(event.string("reply_id") ?? String(event.seq))"; speaker = .jarvis
        case .show:
            id = event.string("reply_id").map { "reply:\($0)" } ?? "show:\(event.seq)"; speaker = .jarvis
        case .notice: id = "notice:\(event.seq)"; speaker = .notice
        default: return
        }
        if positions[id] == nil {
            positions[id] = rows.count
            rows.append(ConversationRow(id: id, sequence: event.seq, time: event.time,
                                        clientID: event.client_id, speaker: speaker,
                                        requestID: event.string("request_id"), text: ""))
        }
        guard let index = positions[id] else { return }
        if let request = event.string("request_id") { rows[index].requestID = request }
        switch event.kind {
        case .replyDelta:
            rows[index].text += event.string("text") ?? ""; rows[index].streaming = true
        case .replyEnd, .heard, .notice:
            rows[index].text = event.string("text") ?? ""; rows[index].streaming = false
        case .show:
            rows[index].details.append(.init(id: event.seq, title: event.string("title") ?? "Details", text: event.string("text") ?? ""))
        default: break
        }
    }
}
