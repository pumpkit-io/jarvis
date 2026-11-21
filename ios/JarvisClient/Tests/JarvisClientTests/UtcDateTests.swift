import Foundation
import Testing
@testable import JarvisClient

@Test func utcDateReadsEveryHubTimeFormAndRefusesOtherZones() {
    #expect(abs((utcDate("2026-11-21T08:19:41.701103Z")?.timeIntervalSince1970 ?? 0) - 1_795_249_181.701103) < 0.00001)
    #expect(utcDate("2026-11-21T08:19:41Z")?.timeIntervalSince1970 == 1_795_249_181)
    #expect(utcDate("2026-11-21T08:19:41+00:00")?.timeIntervalSince1970 == 1_795_249_181)
    #expect(utcDate("2026-11-21T08:19:41+01:00") == nil)
    #expect(utcDate("yesterday") == nil)
}

@Test func utcDateParsesACatchUpWorthOfTimesQuickly() {
    let times = (0..<10_000).map { String(format: "2026-11-21T08:%02d:%02d.701103Z", $0 / 60 % 60, $0 % 60) }
    let start = Date()
    let parsed = times.compactMap(utcDate)
    #expect(parsed.count == times.count)
    #expect(Date().timeIntervalSince(start) < 0.5)
}
