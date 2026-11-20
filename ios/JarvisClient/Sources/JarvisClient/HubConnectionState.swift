import Foundation

public enum HubConnectionState: Equatable, Sendable {
    case suspended
    case incompatibleProtocol
    case unpaired
    case connecting
    case ready
    case unreachable
    case hubStopped
    case usageLimited(resetTime: String?)

    public var canSend: Bool {
        self == .ready
    }

    public var indicator: ConnectionIndicator {
        indicator(timeZone: .current, locale: .current)
    }

    public func indicator(timeZone: TimeZone, locale: Locale) -> ConnectionIndicator {
        switch self {
        case .ready:
            ConnectionIndicator(tone: .green, label: nil)
        case .connecting:
            ConnectionIndicator(tone: .amber, label: "Connecting")
        case .suspended:
            ConnectionIndicator(tone: .amber, label: "Paused")
        case .unpaired:
            ConnectionIndicator(tone: .red, label: "Not paired")
        case .unreachable:
            ConnectionIndicator(tone: .red, label: "Mac unreachable")
        case .hubStopped:
            ConnectionIndicator(tone: .red, label: "Jarvis not running")
        case .incompatibleProtocol:
            ConnectionIndicator(tone: .red, label: "Update needed")
        case .usageLimited(let resetTime):
            ConnectionIndicator(tone: .red, label: resetTime.map { "Limit · resets \(Self.shortTime($0, timeZone: timeZone, locale: locale))" } ?? "Limit")
        }
    }

    private static func shortTime(_ value: String, timeZone: TimeZone, locale: Locale) -> String {
        guard let date = utcDate(value) else { return value }
        let format = DateFormatter()
        format.locale = locale
        format.timeZone = timeZone
        format.timeStyle = .short
        format.dateStyle = .none
        return format.string(from: date)
    }

    public var message: String {
        switch self {
        case .suspended:
            "Connection paused"
        case .incompatibleProtocol:
            "This Client needs a compatible Hub."
        case .unpaired:
            "Not paired yet"
        case .connecting:
            "Connecting…"
        case .ready:
            "Jarvis is ready"
        case .unreachable:
            "Can't reach the Mac. It may be asleep or offline."
        case .hubStopped:
            "The Mac is on, but Jarvis isn't running. Start ./jarvis there."
        case .usageLimited(let resetTime):
            if let resetTime {
                "Usage limit reached. Resets at \(resetTime)."
            } else {
                "Usage limit reached. Reset time is unknown."
            }
        }
    }
}

public struct ConnectionIndicator: Equatable, Sendable {
    public enum Tone: Equatable, Sendable { case green, amber, red }

    public let tone: Tone
    public let label: String?

    public init(tone: Tone, label: String?) {
        self.tone = tone
        self.label = label
    }
}
