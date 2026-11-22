import Foundation

extension ConversationRow {
    /// When the row's event happened, or nil for a time the Hub did not send in UTC.
    public var date: Date? { utcDate(time) }
}

/// The date and time shown on every message, in the reader's locale and time zone.
public struct MessageStamp: Equatable, Sendable {
    public let day: String
    public let time: String
    public let text: String

    public init(_ date: Date, now: Date = Date(), locale: Locale = .autoupdatingCurrent,
                timeZone: TimeZone = .autoupdatingCurrent) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let showsYear = calendar.component(.year, from: date) != calendar.component(.year, from: now)
        var day = Date.FormatStyle(locale: locale, calendar: calendar, timeZone: timeZone).day().month(.abbreviated)
        if showsYear { day = day.year() }
        let time = Date.FormatStyle(locale: locale, calendar: calendar, timeZone: timeZone).hour().minute()
        self.day = date.formatted(day)
        self.time = date.formatted(time)
        text = "\(self.day) \(self.time)"
    }
}
