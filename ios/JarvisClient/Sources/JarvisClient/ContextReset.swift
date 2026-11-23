/// Where Settings' Reset context control stands, and what its button and status line say.
public enum ContextReset: Equatable, Sendable {
    case idle, resetting, reset
    /// Nothing was sent: the Client had no connection to send it on.
    case notReset
    /// The Hub answered with a refusal that resets nothing, such as a stopping Hub or one too old to know the command.
    case refused
    /// The Hub cleared the messages and summary in memory but could not delete a saved file.
    case unfinished
    /// The Hub may have reset: it lost the outcome, or the send or the connection failed after the command went out.
    case unsure

    public static let dialogTitle = "Reset context?"
    public static let dialogMessage = "This cannot be undone. It deletes all the messages and the summary Jarvis keeps, while the 30-day conversation logs already saved are not deleted."
    public static let confirmTitle = "Reset context"

    public var buttonTitle: String { self == .resetting ? "Resetting" : "Reset context" }

    public var statusLine: String? {
        switch self {
        case .idle, .resetting: nil
        case .reset: "Context reset."
        case .notReset: "Could not reach the Mac. Nothing was reset."
        case .refused: "The Mac refused the reset. Nothing was reset."
        case .unfinished: "Could not finish the reset on the Mac. Try again."
        case .unsure: "Not sure the reset happened. Try again."
        }
    }

    init(_ result: CommandResult) {
        if result.ok { self = .reset; return }
        switch result.error?.code {
        case "outcome_unknown": self = .unsure
        case "reset_failed": self = .unfinished
        default: self = .refused
        }
    }
}
