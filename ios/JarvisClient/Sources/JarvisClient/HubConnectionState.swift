public enum HubConnectionState: Equatable, Sendable {
    case unpaired
    case connecting
    case ready
    case unreachable
    case hubStopped
    case usageLimited(resetTime: String?)

    public var canSend: Bool {
        self == .ready
    }

    public var message: String {
        switch self {
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
