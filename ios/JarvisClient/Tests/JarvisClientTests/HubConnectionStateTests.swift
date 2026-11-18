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
