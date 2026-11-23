import Foundation
import Observation
import Testing
@testable import JarvisClient

/// Counts the main-actor turns in which the script changed: SwiftUI redraws the conversation once for each.
@MainActor private final class ScriptRedraws {
    var count = 0
    func watch(_ session: AppSession) {
        withObservationTracking { _ = session.conversation.rows } onChange: {
            Task { @MainActor in self.count += 1; self.watch(session) }
        }
    }
}

/// A month of conversation as the Hub replays it to a newly paired phone: each turn is a line, three fragments and the reply.
private func monthOfTurns(_ turns: Int) -> [String] {
    var frames: [String] = [], seq = 0
    func next(_ type: String, _ payload: String) { seq += 1; frames.append(event(seq, type, payload)) }
    for turn in 0..<turns {
        next("heard", #"{"request_id":"request-\#(turn)","text":"Line \#(turn)","spoken":false}"#)
        for part in 0..<3 { next("reply_delta", #"{"reply_id":"reply-\#(turn)","request_id":"request-\#(turn)","text":"Part \#(part). "}"#) }
        next("reply_end", #"{"reply_id":"reply-\#(turn)","request_id":"request-\#(turn)","text":"Part 0. Part 1. Part 2.","reason":"complete"}"#)
    }
    next("status", #"{"ready":true,"usage_limit":{"limited":false,"resets_at":null},"quiet":false,"focus_client_id":null,"mac_voice_on":true,"silence":false}"#)
    frames.append(#"{"version":1,"type":"replay_complete","through_seq":\#(seq)}"#)
    return frames
}

@MainActor @Test func fullReplayReachesTheScreenInOneRedrawAndThenAcceptsMessages() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil })
    let redraws = ScriptRedraws(); redraws.watch(session)
    for frame in monthOfTurns(300) { await socket.text(frame) }
    await session.launch()
    try await eventuallySession { session.connection.canSend }
    #expect(session.conversation.rows.count == 600)
    #expect(session.conversation.rows.last?.text == "Part 0. Part 1. Part 2.")
    #expect(redraws.count <= 2)
    await session.unpair()
}

@Test func replayArrivesAsOneBatchBeforeLiveEventsArriveOneByOne() async throws {
    let socket = FakeSocket()
    let client = HubClient(transport: HubTransport(connect: { _ in socket }))
    await client.start()
    for frame in monthOfTurns(20) { await socket.text(frame) }
    await socket.text(event(102, "notice", #"{"text":"Live news."}"#))
    var batches: [[Int64]] = [], single: [Int64] = [], replayed = false
    for await update in client.updates {
        switch update {
        case .events(let events): batches.append(events.map(\.seq))
        case .event(let event): single.append(event.seq)
        case .replay: replayed = true; #expect(single.isEmpty)
        default: break
        }
        if single.contains(102) { break }
    }
    #expect(replayed)
    #expect(batches == [Array(1...101)])
    #expect(single == [102])
    await client.stop()
}
