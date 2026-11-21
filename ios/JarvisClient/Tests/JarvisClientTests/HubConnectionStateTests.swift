import Foundation
import Testing
import JarvisClient

@Test func requestsRequireAReadyConnection() {
    #expect(!HubConnectionState.unpaired.canSend)
    #expect(HubConnectionState.ready.canSend)
    #expect(!HubConnectionState.connecting.canSend)
    #expect(!HubConnectionState.unreachable.canSend)
    #expect(!HubConnectionState.hubStopped.canSend)
    #expect(!HubConnectionState.usageLimited(resetTime: "11:30 PM").canSend)
}

@Test func usageLimitIncludesTheKnownResetTime() {
    #expect(HubConnectionState.usageLimited(resetTime: "11:30 PM").message ==
        "Usage limit reached. Resets at 11:30 PM.")
    #expect(HubConnectionState.usageLimited(resetTime: nil).message ==
        "Usage limit reached. Reset time is unknown.")
}

@Test func readyConnectionIndicatorIsQuietAndGreen() {
    #expect(HubConnectionState.ready.indicator == ConnectionIndicator(tone: .green, label: nil))
}

@Test func connectionIndicatorExplainsEveryNonReadyState() {
    let cases: [(HubConnectionState, ConnectionIndicator.Tone, String)] = [
        (.connecting, .amber, "Connecting"),
        (.suspended, .amber, "Paused"),
        (.unpaired, .red, "Not paired"),
        (.unreachable, .red, "Mac unreachable"),
        (.hubStopped, .red, "Jarvis not running"),
        (.incompatibleProtocol, .red, "Update needed"),
        (.usageLimited(resetTime: "14:30"), .red, "Limit · resets 14:30"),
        (.usageLimited(resetTime: nil), .red, "Limit")
    ]
    for (state, tone, label) in cases {
        #expect(state.indicator == ConnectionIndicator(tone: tone, label: label))
    }
}

@Test func connectionIndicatorFormatsTheResetInTheUsersTimeZone() {
    let state = HubConnectionState.usageLimited(resetTime: "2026-11-21T13:30:00Z")
    #expect(state.indicator(timeZone: TimeZone(secondsFromGMT: 3600)!, locale: Locale(identifier: "en_GB")) ==
        ConnectionIndicator(tone: .red, label: "Limit · resets 14:30"))
}
