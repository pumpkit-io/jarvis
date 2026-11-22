import Foundation
import Testing
@testable import JarvisClient

private let rome = TimeZone(identifier: "Europe/Rome")!
private let british = Locale(identifier: "en_GB")

@Test func everyScriptRowCarriesTheDateItsEventHappened() throws {
    var script = ConversationProjection()
    for event in try [
        conversationEvent(1, "heard", #"{"request_id":"a","text":"Hello","spoken":false}"#),
        conversationEvent(2, "reply_end", #"{"reply_id":"r","request_id":"a","text":"Hi.","reason":"complete"}"#),
        conversationEvent(3, "show", #"{"reply_id":null,"title":"Report","text":"standalone"}"#)
    ] { script.ingest(event) }
    #expect(script.rows.count == 3)
    #expect(script.rows.allSatisfy { $0.date == utcDate("2026-11-19T12:00:00Z") })
}

@Test func stampShowsLocalDayAndTimeAndAddsTheYearOnlyForAnotherYear() throws {
    let now = try #require(utcDate("2026-11-22T09:00:00Z"))
    let thisYear = MessageStamp(try #require(utcDate("2026-11-19T12:05:00Z")), now: now, locale: british, timeZone: rome)
    #expect(thisYear.day == "19 Nov")
    #expect(thisYear.time == "13:05")
    #expect(thisYear.text == "19 Nov 13:05")
    let lastYear = MessageStamp(try #require(utcDate("2025-12-31T23:30:00Z")), now: now, locale: british, timeZone: rome)
    #expect(lastYear.day == "1 Jan")
    let older = MessageStamp(try #require(utcDate("2025-06-01T08:00:00Z")), now: now, locale: british, timeZone: rome)
    #expect(older.day == "1 Jun 2025")
    #expect(older.text == "1 Jun 2025 10:00")
}

@Test func stampFollowsTheReadersLocale() throws {
    let now = try #require(utcDate("2026-11-22T09:00:00Z"))
    let stamp = MessageStamp(try #require(utcDate("2026-11-19T20:05:00Z")), now: now,
                             locale: Locale(identifier: "en_US"), timeZone: TimeZone(identifier: "America/New_York")!)
    #expect(stamp.day == "Nov 19")
    #expect(stamp.time == "3:05\u{202F}PM")
}
