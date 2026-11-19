import Foundation
import Testing
import JarvisClient

@MainActor final class FakeTalkRecorder: TalkRecorder {
    var starts = 0
    var stops = 0
    var active = false
    var pcm = Data([1, 0, 2, 0])
    func start(limitReached: @escaping @MainActor @Sendable () -> Void) throws { starts += 1; active = true }
    func stop() -> Data { stops += 1; active = false; return pcm }
}

@MainActor final class TalkWire {
    var commands: [HubCommand] = []
    var uploads: [(String, String, Data)] = []
    var retired: [String] = []
    func send(_ command: HubCommand) async throws { commands.append(command) }
    func upload(_ talk: String, _ id: String, _ data: Data) async throws -> CommandResult {
        uploads.append((talk, id, data))
        return try JSONDecoder().decode(CommandResult.self, from: Data("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(id)\",\"ok\":true,\"result\":{\"request_id\":\"r1\",\"talk_id\":\"\(talk)\"}}".utf8))
    }
}

@MainActor @Test func releaseStopsCaptureBeforeUploadAndKeepsTalkIdentity() async throws {
    let recorder = FakeTalkRecorder(), wire = TalkWire()
    let talk = TalkController(recorder: recorder, permission: { true })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { wire.retired.append($0) },
                   upload: { try await wire.upload($0, $1, $2) })
    await talk.press()
    #expect(recorder.active)
    #expect(talk.state == .recording)
    let begin = try #require(wire.commands.first)
    #expect(begin.type == "talk_begin")
    await talk.release()
    #expect(!recorder.active)
    #expect(wire.uploads.count == 1)
    #expect(begin.payload["talk_id"] == wire.uploads.first.map { .string($0.0) })
    #expect(talk.state == .thinking)
}

@MainActor final class HeldMicrophonePermission {
    var waiter: CheckedContinuation<Bool, Never>?
    func request() async -> Bool { await withCheckedContinuation { waiter = $0 } }
    func grant() { waiter?.resume(returning: true); waiter = nil }
}

@MainActor @Test func delayedMicrophoneGrantCannotReviveReleasedCancelledOrDisconnectedHold() async throws {
    for action in ["release", "cancel", "disconnect"] {
        let recorder = FakeTalkRecorder(), wire = TalkWire(), permission = HeldMicrophonePermission()
        let talk = TalkController(recorder: recorder, permission: { await permission.request() })
        talk.configure(ready: true, send: { try await wire.send($0) }, retire: { wire.retired.append($0) },
                       upload: { try await wire.upload($0, $1, $2) })
        let press = Task { await talk.press() }
        try await eventuallySession { permission.waiter != nil }
        switch action {
        case "release": await talk.release()
        case "cancel": await talk.cancel()
        default: await talk.setReady(false)
        }
        permission.grant(); await press.value
        #expect(recorder.starts == 0)
        #expect(wire.commands.isEmpty)
        #expect(wire.uploads.isEmpty)
    }
}

@MainActor @Test func slideCancellationStopsRecorderAndNeverUploads() async throws {
    let recorder = FakeTalkRecorder(), wire = TalkWire()
    let talk = TalkController(recorder: recorder, permission: { true })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { wire.retired.append($0) },
                   upload: { try await wire.upload($0, $1, $2) })
    await talk.press(); await talk.cancel(); await talk.release()
    #expect(!recorder.active)
    #expect(recorder.stops == 1)
    #expect(wire.commands.map(\.type) == ["talk_begin", "talk_cancel"])
    #expect(wire.uploads.isEmpty)
    #expect(talk.state == .cancelled)
    await talk.setReady(false); await talk.press()
    #expect(recorder.starts == 1)
}

@Test func abandonedUnacknowledgedTalkBeginIsNotReplayedOnReconnect() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventually { await client.state == .ready }
    try await client.send(HubCommand(id: "begin", type: "talk_begin", payload: ["talk_id": .string("abandoned")]))
    try await client.send(HubCommand(id: "other", type: "take_focus"))
    await client.retireTalkBegin(id: "begin")
    try await eventually { await transport.socket(1) != nil }
    let replacement = try #require(await transport.socket(1))
    await replacement.ready(through: 2)
    try await eventually { await replacement.sent.contains(where: { $0.id == "other" }) }
    #expect(await replacement.sent.contains(where: { $0.id == "begin" }) == false)
    #expect(await socket.closed)
    await client.stop()
}

@MainActor final class FakeTalkSession: TalkAudioSession {
    var active = false
    var activations = 0
    var releases = 0
    func activate() throws { active = true; activations += 1 }
    func release() { active = false; releases += 1 }
}

@MainActor final class FakeCaptureSource: TalkCaptureSource {
    var buffer: TalkPCMBuffer?
    var limit: (@MainActor @Sendable () -> Void)?
    var active = false
    var fails = false
    func start(buffer: TalkPCMBuffer, limitReached: @escaping @MainActor @Sendable () -> Void) throws {
        if fails { throw HubProtocolError.invalidFrame }
        self.buffer = buffer; limit = limitReached; active = true
    }
    func stop() { active = false }
    func frames(_ bytes: Data) { if buffer?.append(bytes) == true { limit?() } }
}

@MainActor @Test func recorderReleasesAudioSessionBeforeReturningPcmAndBoundsDelayedTimer() throws {
    let session = FakeTalkSession(), source = FakeCaptureSource()
    let recorder = NativeTalkRecorder(session: session, source: source)
    var reached = 0
    try recorder.start { reached += 1 }
    #expect(session.active && source.active)
    source.frames(Data(repeating: 9, count: 9_600_008))
    source.frames(Data(repeating: 8, count: 100))
    #expect(reached == 1)
    let bytes = recorder.stop()
    #expect(bytes.count == 9_600_000)
    #expect(!session.active && !source.active)
    #expect(session.releases == 1)
    #expect(recorder.stop().isEmpty)
    source.fails = true
    #expect(throws: HubProtocolError.self) { try recorder.start {} }
    #expect(!session.active)
    #expect(session.releases == 2)
}

@MainActor @Test func fastReleaseAwaitsOnlyExistingHealthyBeginAcknowledgement() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    await client.start()
    try await eventuallySession { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready()
    try await eventuallySession { await client.state == .ready }
    let recorder = FakeTalkRecorder(), wire = TalkWire()
    let talk = TalkController(recorder: recorder, permission: { true })
    talk.configure(ready: true, send: { try await client.send($0) }, retire: { await client.retireTalkBegin(id: $0) },
                   upload: { id, command, pcm in
                       #expect(!recorder.active)
                       return try await wire.upload(id, command, pcm)
                   }, admission: { await client.finishTalkBegin(id: $0) })
    await talk.press()
    let begin = try #require(await socket.sent.first(where: { $0.type == "talk_begin" }))
    let release = Task { await talk.release() }
    try await eventuallySession { !recorder.active }
    #expect(talk.state == .uploading)
    #expect(wire.uploads.isEmpty)
    #expect(await socket.closed == false)
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(begin.id)\",\"ok\":true,\"result\":{\"talk_id\":\"t\"}}")
    await release.value
    #expect(talk.state == .thinking)
    #expect(wire.uploads.count == 1)
    #expect(await socket.closed == false)
    await client.stop()
}

@MainActor @Test func canonicalReplyEndBeforeUploadResponseDoesNotLeaveTalkThinking() async throws {
    let recorder = FakeTalkRecorder(), wire = TalkWire()
    let talk = TalkController(recorder: recorder, permission: { true })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { wire.retired.append($0) },
                   upload: { id, command, bytes in
                       let frame = try HubFrame.decode(Data(event(2, "reply_end", #"{"reply_id":"reply","request_id":"r1","text":"Done","reason":"complete"}"#).utf8))
                       if case .event(let event) = frame { talk.receive(event) }
                       return try await wire.upload(id, command, bytes)
                   })
    await talk.press(); await talk.release()
    #expect(talk.requestID == "r1")
    #expect(talk.state == .idle)
}

@MainActor @Test func fiveMinuteTimerSendsOnceAndPermissionRefusalDoesNotCapture() async throws {
    let sleeper = FakeSleeper()
    let recorder = FakeTalkRecorder(), wire = TalkWire()
    let clock = TalkTestClock()
    let talk = TalkController(recorder: recorder, permission: { true }, now: { clock.time }, sleep: { try await sleeper.sleep($0) })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { wire.retired.append($0) },
                   upload: { try await wire.upload($0, $1, $2) })
    await talk.press()
    try await eventuallySession { await sleeper.pending == 1 }
    clock.time = 301
    await sleeper.advance()
    try await eventuallySession { wire.uploads.count == 1 }
    await talk.release()
    #expect(recorder.stops == 1)
    #expect(talk.elapsed == 300)
    let refused = TalkController(recorder: recorder, permission: { false })
    refused.configure(ready: true, send: { try await wire.send($0) }, retire: { _ in }, upload: { try await wire.upload($0, $1, $2) })
    await refused.press()
    #expect(refused.state == .refused)
    #expect(refused.message == "Jarvis needs the microphone to hear you. Allow it in Settings.")
    #expect(recorder.starts == 1)
}

@MainActor final class TalkTestClock { var time = 0.0 }

@MainActor @Test func uploadRetriesExactIdentityAndBytesThenFailsAtThirtySeconds() async throws {
    let sleeper = FakeSleeper(), recorder = FakeTalkRecorder(), wire = TalkWire(), clock = TalkTestClock()
    var attempts: [(String, String, Data)] = []
    let talk = TalkController(recorder: recorder, permission: { true }, now: { clock.time }, sleep: { try await sleeper.sleep($0) })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { _ in }, upload: { id, command, pcm in
        #expect(!recorder.active)
        attempts.append((id, command, pcm))
        throw URLError(.networkConnectionLost)
    })
    await talk.press()
    let release = Task { await talk.release() }
    try await eventuallySession { let pending = await sleeper.pending; return attempts.count == 1 && pending == 2 }
    // The retry deadline and retry delay are independently controllable.
    await sleeper.advance()
    await release.value
    #expect(talk.state == .failed)
    #expect(talk.message == "That didn't reach Jarvis. Hold to talk again.")
    #expect(recorder.stops == 1)
}

@MainActor @Test func retriesPreserveUploadIDAndPcmAndSuppressLateResponseAfterCancel() async throws {
    let sleeper = FakeSleeper(), recorder = FakeTalkRecorder(), wire = TalkWire()
    var attempts: [(String, String, Data)] = []
    var response: CheckedContinuation<CommandResult, any Error>?
    let talk = TalkController(recorder: recorder, permission: { true }, sleep: { seconds in
        if seconds == 30 { try await Task.sleep(for: .seconds(30)) }
        else { try await sleeper.sleep(seconds) }
    })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { _ in }, upload: { id, command, pcm in
        attempts.append((id, command, pcm))
        if attempts.count == 1 { throw URLError(.networkConnectionLost) }
        return try await withCheckedThrowingContinuation { response = $0 }
    })
    await talk.press()
    let release = Task { await talk.release() }
    try await eventuallySession { let pending = await sleeper.pending; return attempts.count == 1 && pending == 1 }
    await sleeper.advance()
    try await eventuallySession { response != nil }
    #expect(attempts.count == 2)
    #expect(attempts[0].0 == attempts[1].0)
    #expect(attempts[0].1 == attempts[1].1)
    #expect(attempts[0].2 == attempts[1].2)
    await talk.cancel()
    let result = try await wire.upload(attempts[1].0, attempts[1].1, attempts[1].2)
    response?.resume(returning: result)
    await release.value
    #expect(talk.state == .cancelled)
    #expect(talk.requestID == nil)
}

@Test func uploadUsesAuthenticatedExactPcmProtocolWithoutWavOrRedirects() async throws {
    let credentials = try testPairingCredentials()
    let upload = TalkUpload { request in
        #expect(request.url?.absoluteString == "https://mac.example/v1/utterances/talk-1")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
        #expect(request.value(forHTTPHeaderField: "Idempotency-Key") == "upload-1")
        #expect(request.value(forHTTPHeaderField: "X-Jarvis-Audio-Format") == "pcm_s16le_16000_mono")
        #expect(request.httpBody == Data([1, 0, 2, 0]))
        return (Data(#"{"version":1,"type":"command_result","id":"upload-1","ok":true,"result":{"talk_id":"talk-1","request_id":"r"}}"#.utf8), 200)
    }
    #expect(try await upload.send(credentials: credentials, talkID: "talk-1", commandID: "upload-1", pcm: Data([1, 0, 2, 0])).ok)
}

@MainActor @Test func backgroundAndUnpairInvalidateDelayedPermissionThroughAppSession() async throws {
    for transition in ["background", "unpair"] {
        let socket = FakeSocket(), recorder = FakeTalkRecorder(), permission = HeldMicrophonePermission()
        let controller = TalkController(recorder: recorder, permission: { await permission.request() })
        let store = MemoryPairingStore(); store.record = try testPairingCredentials()
        let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                                 makePaths: { nil }, talk: controller)
        await session.launch(); await socket.ready()
        try await eventuallySession { session.talk.canPress }
        let press = Task { await session.talk.press() }
        try await eventuallySession { permission.waiter != nil }
        if transition == "background" { await session.setForeground(false) }
        else { await session.unpair() }
        permission.grant(); await press.value
        #expect(recorder.starts == 0)
        #expect(await socket.sent.contains(where: { $0.type == "talk_begin" }) == false)
        await session.unpair()
    }
}

@MainActor @Test func sessionInterruptCarriesOnlyCompletedClipThenTalkTranscriptAndReplySettle() async throws {
    let socket = FakeSocket(), recorder = FakeTalkRecorder(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let controller = TalkController(recorder: recorder, permission: { true })
    var stops = 0
    let response = HeldUploadResponse()
    let service = TalkUpload { request in try await response.post(request) }
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: controller, uploadTalk: service, stopPlayback: { stops += 1 })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.talk.canPress }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"older","reply_id":"older-reply","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventuallySession { session.speechID == "older" }
    session.reportCompletedClip(speechID: "older", index: 0)
    session.reportCompletedClip(speechID: "stale", index: 50)
    await session.talk.press()
    let commands = await socket.sent
    let interrupt = try #require(commands.first(where: { $0.type == "interrupt" }))
    #expect(interrupt.payload == ["speech_id": .string("older"), "last_clip_index": .integer(0)])
    #expect(stops == 1)
    let begin = try #require(commands.first(where: { $0.type == "talk_begin" }))
    #expect(commands.firstIndex(of: interrupt)! < commands.firstIndex(of: begin)!)
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(begin.id)\",\"ok\":true,\"result\":{}}")
    let released = Task { await session.talk.release() }
    try await eventuallySession { await response.waiting }
    await socket.text(event(4, "heard", #"{"request_id":"new-request","text":"check the tests","spoken":true}"#))
    await socket.text(event(5, "reply_end", #"{"reply_id":"new-reply","request_id":"new-request","text":"They pass.","reason":"complete"}"#))
    try await eventuallySession { session.conversation.rows.last?.text == "They pass." }
    await response.finish()
    await released.value
    #expect(session.talk.state == .idle)
    #expect(session.conversation.rows.map(\.text) == ["check the tests", "They pass."])
    #expect(!recorder.active)
    await session.unpair()
}

@Test func disconnectRetiresBeginBeforeReconnectEvenWhenItsWriteNeverReachedHub() async throws {
    let transport = FakeTransport()
    let sleeper = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { try await sleeper.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await client.state == .ready }
    try await client.send(HubCommand(id: "lost-begin", type: "talk_begin", payload: ["talk_id": .string("lost")] ))
    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await sleeper.pending == 1 }
    #expect(await client.pendingCommands.contains(where: { $0.id == "lost-begin" }) == false)
    await sleeper.advance()
    try await eventually { await transport.socket(1) != nil }
    let next = try #require(await transport.socket(1))
    await next.ready(through: 2)
    try await eventually { await client.state == .ready }
    #expect(await next.sent.contains(where: { $0.type == "talk_begin" }) == false)
    await client.stop()
}

@MainActor @Test func lateBeginAdmissionCannotUploadCancelledTalkOrReplaceNewHold() async throws {
    let recorder = FakeTalkRecorder(), wire = TalkWire()
    var admission: CheckedContinuation<Bool, Never>?
    let talk = TalkController(recorder: recorder, permission: { true })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { _ in },
                   upload: { try await wire.upload($0, $1, $2) }, admission: { _ in
                       await withCheckedContinuation { admission = $0 }
                   })
    await talk.press()
    let oldID = talk.talkID
    let release = Task { await talk.release() }
    try await eventuallySession { admission != nil }
    #expect(!recorder.active)
    await talk.cancel()
    await talk.press()
    #expect(talk.talkID != oldID)
    admission?.resume(returning: true)
    await release.value
    #expect(wire.uploads.isEmpty)
    #expect(talk.state == .recording)
    #expect(recorder.active)
    await talk.cancel()
}

@Test func healthyLostBeginAckExpiresWithoutEverReplayingBegin() async throws {
    let transport = FakeTransport(), deadline = FakeSleeper()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }),
                           sleep: { _ in }, lifecycleSleep: { try await deadline.sleep($0) })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await client.state == .ready }
    try await client.send(HubCommand(id: "begin-no-ack", type: "talk_begin", payload: ["talk_id": .string("t")]))
    let finish = Task { await client.finishTalkBegin(id: "begin-no-ack") }
    try await eventually { await deadline.pending == 1 }
    await deadline.advance()
    #expect(await finish.value == false)
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 2)
    try await eventually { await client.state == .ready }
    #expect(await second.sent.contains(where: { $0.type == "talk_begin" }) == false)
    await client.stop()
}

actor HeldUploadResponse {
    var waiter: CheckedContinuation<(Data, Int), any Error>?
    var request: URLRequest?
    var waiting: Bool { waiter != nil }
    func post(_ request: URLRequest) async throws -> (Data, Int) {
        self.request = request
        return try await withCheckedThrowingContinuation { waiter = $0 }
    }
    func finish() {
        let id = request!.value(forHTTPHeaderField: "Idempotency-Key")!
        let talk = request!.url!.lastPathComponent
        waiter?.resume(returning: (Data("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(id)\",\"ok\":true,\"result\":{\"talk_id\":\"\(talk)\",\"request_id\":\"new-request\"}}".utf8), 200))
        waiter = nil
    }
}

@MainActor @Test func permissionRefusalRemainsVisibleAfterPermissionDialogEndsHold() async throws {
    let permission = HeldMicrophonePermission(), recorder = FakeTalkRecorder(), wire = TalkWire()
    let talk = TalkController(recorder: recorder, permission: { await permission.request() })
    talk.configure(ready: true, send: { try await wire.send($0) }, retire: { _ in }, upload: { try await wire.upload($0, $1, $2) })
    let press = Task { await talk.press() }
    try await eventuallySession { permission.waiter != nil }
    await talk.cancel()
    permission.waiter?.resume(returning: false); permission.waiter = nil
    await press.value
    #expect(talk.microphoneRefused)
    #expect(talk.message == "Jarvis needs the microphone to hear you. Allow it in Settings.")
    #expect(recorder.starts == 0)
    #expect(wire.commands.isEmpty)
}
