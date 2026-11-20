import Foundation
import Testing
@testable import JarvisClient

@MainActor final class FakeOutboxTransport: OutboxTransport {
    var started: [OutboxDelivery] = []
    var live: Set<String> = []
    var cancelled = false
    private var handler: (@MainActor (String, OutboxResponse) -> Void)?
    func onResponse(_ handler: @escaping @MainActor (String, OutboxResponse) -> Void) { self.handler = handler }
    func start(_ delivery: OutboxDelivery) { started.append(delivery) }
    func inFlight() async -> Set<String> { live }
    func cancelAll() { cancelled = true }

    func answer(_ index: Int, status: Int = 200, ok: Bool = true, request: String = "request", code: String? = nil, retryAfter: Double? = nil) {
        let delivery = started[index]
        let commandID = delivery.request.value(forHTTPHeaderField: "Idempotency-Key")!
        let body = ok ? #"{"version":1,"type":"command_result","id":"\#(commandID)","ok":true,"result":{"request_id":"\#(request)"}}"#
            : #"{"version":1,"type":"command_result","id":"\#(commandID)","ok":false,"error":{"code":"\#(code!)","message":"Refused"}}"#
        handler?(delivery.itemID, .http(status: status, body: Data(body.utf8), retryAfter: retryAfter))
    }
    func fail(_ index: Int, _ response: OutboxResponse = .unreachable) { handler?(started[index].itemID, response) }
}

private let credentials = try! PairingCredentials(origin: URL(string: "https://mac.example.ts.net")!, clientID: "phone",
                                                  deviceToken: "token", macName: "Mac")

@MainActor private func makeOutbox(_ directory: URL, _ transport: FakeOutboxTransport, delays: SleepLog = SleepLog()) -> Outbox {
    Outbox(store: OutboxStore(directory: directory, credentials: credentials), transport: transport, credentials: credentials,
           now: { Date(timeIntervalSince1970: 1_000) }, sleep: { await delays.record($0) })
}

actor SleepLog {
    var delays: [Double] = []
    func record(_ delay: Double) { delays.append(delay) }
}

@MainActor private func outboxSettles(_ condition: () -> Bool) async throws {
    for _ in 0..<1000 { if condition() { return }; try await Task.sleep(for: .milliseconds(1)) }
    Issue.record("Condition did not become true")
}

private func scratch() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }

private extension OutboxDelivery {
    var commandID: String? { request.value(forHTTPHeaderField: "Idempotency-Key") }
    var path: String? { request.url?.path }
}

@MainActor @Test func messagesGoOutOneAtATimeInOrderWithExactRequests() async throws {
    let transport = FakeOutboxTransport()
    let outbox = makeOutbox(scratch(), transport)
    let pcm = Data([0, 128, 255, 127])
    outbox.enqueueTalk(talkID: "talk-1", pcm: pcm, recordedAt: Date(timeIntervalSince1970: 400))
    outbox.enqueueLine("Ciao", recordedAt: Date(timeIntervalSince1970: 400))
    #expect(transport.started.count == 1)
    let talk = transport.started[0]
    #expect(talk.path == "/v1/utterances/talk-1")
    #expect(talk.request.httpMethod == "POST")
    #expect(talk.request.value(forHTTPHeaderField: "Authorization") == "Bearer token")
    #expect(talk.request.value(forHTTPHeaderField: "X-Jarvis-Audio-Format") == "pcm_s16le_16000_mono")
    #expect(talk.request.value(forHTTPHeaderField: "X-Jarvis-Recorded-At") == "1970-01-01T00:06:40Z")
    #expect(try Data(contentsOf: talk.body) == pcm)
    #expect(outbox.items.map(\.status) == [.sending, .waiting])

    transport.answer(0, request: "r1")
    #expect(transport.started.count == 2)
    let line = transport.started[1]
    #expect(line.path == "/v1/lines")
    let body = try JSONSerialization.jsonObject(with: Data(contentsOf: line.body)) as? [String: Any]
    #expect(body?["text"] as? String == "Ciao" && body?["recorded_at"] as? String == "1970-01-01T00:06:40Z" && body?["version"] as? Int == 1)
    #expect(outbox.items.map(\.status) == [.sent(requestID: "r1"), .sending])
}

@MainActor @Test func lostConnectionRetriesTheSameIdsAfterBackoffAndNudgeSkipsTheWait() async throws {
    let transport = FakeOutboxTransport()
    let delays = SleepLog()
    let outbox = makeOutbox(scratch(), transport, delays: delays)
    outbox.enqueueLine("Hello", recordedAt: Date())
    transport.fail(0)
    try await outboxSettles { transport.started.count == 2 }
    #expect(await delays.delays == [1])
    #expect(transport.started[1].commandID == transport.started[0].commandID)
    transport.fail(1, .http(status: 502, body: Data("Bad Gateway".utf8), retryAfter: nil))
    try await outboxSettles { transport.started.count == 3 }
    #expect(await delays.delays == [1, 2])
    #expect(transport.started[2].commandID == transport.started[0].commandID)
    transport.fail(2)
    outbox.nudge()
    #expect(transport.started.count == 4)
}

@MainActor @Test func hubRefusalsRenewIdsUncertainOutcomesStopAndContentFailuresWait() async throws {
    let transport = FakeOutboxTransport()
    let outbox = makeOutbox(scratch(), transport)
    outbox.enqueueTalk(talkID: "live", pcm: Data([1, 0]), recordedAt: Date())
    outbox.enqueueTalk(talkID: "second", pcm: Data([2, 0]), recordedAt: Date())
    outbox.enqueueLine("Third", recordedAt: Date())
    transport.answer(0, status: 409, ok: false, code: "talk_not_active")
    try await outboxSettles { transport.started.count == 2 }
    #expect(transport.started[1].path != "/v1/utterances/live")
    #expect(transport.started[1].commandID != transport.started[0].commandID)
    #expect(try Data(contentsOf: transport.started[1].body) == Data([1, 0]))
    transport.answer(1, status: 409, ok: false, code: "outcome_unknown")
    #expect(outbox.items[0].status == .uncertain)
    #expect(transport.started[2].path == "/v1/utterances/second")
    transport.answer(2, status: 422, ok: false, code: "empty_transcript")
    #expect(outbox.items[1].status == .failed("Jarvis heard no words in this recording."))
    #expect(transport.started[3].path == "/v1/lines")
    transport.answer(3, request: "r3")
    outbox.retry(outbox.items[1].id)
    #expect(transport.started.count == 5)
    #expect(transport.started[4].path != "/v1/utterances/second")
    outbox.delete(outbox.items[0].id)
    #expect(outbox.items.count == 2)
}

@MainActor @Test func repeatedRefusalsEndInAVisibleFailure() async throws {
    let transport = FakeOutboxTransport()
    let outbox = makeOutbox(scratch(), transport)
    outbox.enqueueLine("Hello", recordedAt: Date())
    for attempt in 0..<Outbox.maxAttempts {
        try await outboxSettles { transport.started.count == attempt + 1 }
        transport.answer(attempt, status: 503, ok: false, code: "transcription_failed", retryAfter: 0)
    }
    #expect(outbox.items[0].status == .failed("Jarvis couldn't take this message. Try again."))
    #expect(Set(transport.started.compactMap(\.commandID)).count == Outbox.maxAttempts)
}

@MainActor @Test func queueSurvivesRestartAndOnlyInterruptedUploadsGoBackInLine() async throws {
    let directory = scratch()
    let first = FakeOutboxTransport()
    let outbox = makeOutbox(directory, first)
    outbox.enqueueLine("One", recordedAt: Date())
    outbox.enqueueLine("Two", recordedAt: Date())
    let commandID = first.started[0].commandID

    let interrupted = FakeOutboxTransport()
    let restored = makeOutbox(directory, interrupted)
    #expect(restored.items.map(\.status) == [.sending, .waiting])
    await restored.resume()
    #expect(interrupted.started.map(\.commandID) == [commandID])

    let surviving = FakeOutboxTransport()
    surviving.live = [restored.items[0].id]
    let again = makeOutbox(directory, surviving)
    await again.resume()
    #expect(surviving.started.isEmpty)
    #expect(again.items.map(\.status) == [.sending, .waiting])
}

@MainActor @Test func sentMessagesStayVisibleUntilHeardInEitherOrder() async throws {
    let transport = FakeOutboxTransport()
    let outbox = makeOutbox(scratch(), transport)
    outbox.enqueueLine("One", recordedAt: Date())
    transport.answer(0, request: "r1")
    #expect(outbox.items.map(\.status) == [.sent(requestID: "r1")])
    outbox.heard(requestID: "r1")
    #expect(outbox.items.isEmpty)
    outbox.heard(requestID: "r2")
    outbox.enqueueLine("Two", recordedAt: Date())
    transport.answer(1, request: "r2")
    #expect(outbox.items.isEmpty)
}

@MainActor @Test func backgroundHandsTheNextMessageToTheSystemAndUnpairClearsEverything() async throws {
    let directory = scratch()
    let transport = FakeOutboxTransport()
    let outbox = makeOutbox(directory, transport, delays: SleepLog())
    outbox.enqueueLine("One", recordedAt: Date())
    transport.fail(0)
    outbox.prepareForBackground()
    let handed = try #require(transport.started.last)
    #expect(transport.started.count == 2 && handed.notBefore == Date(timeIntervalSince1970: 1_001))
    outbox.retire()
    #expect(transport.cancelled && outbox.items.isEmpty)
    #expect(makeOutbox(directory, FakeOutboxTransport()).items.isEmpty)
}
