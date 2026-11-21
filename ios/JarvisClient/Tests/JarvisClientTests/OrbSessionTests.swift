import Foundation
import Testing
@testable import JarvisClient

@MainActor private final class OrbTestTime { var now = 10.0 }

@MainActor @Test func orbIdlesAtRestAndWhileASentLineWaits() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    try await eventuallySession { await frames.pending == 1 }
    #expect(session.orb.speaker == .idle && session.orb.level == 0)
    session.draft = "A queued question"
    await session.sendTypedLine()
    await frames.advance()
    try await eventuallySession { await frames.pending == 1 }
    #expect(session.orb.speaker == .idle && session.orb.level == 0)
    await socket.text(event(2, "reply_delta", #"{"reply_id":"r","text":"Waiting for voice"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "Waiting for voice" } }
    await frames.advance()
    try await eventuallySession { await frames.pending == 1 }
    #expect(session.orb.speaker == .idle)
    await session.unpair()
}

@MainActor @Test func orbFollowsPlayingOutputThenIdlesWhenItDrains() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper(), output = FakeSpeechOutput()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: SpeechSession(), output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([0, 64])))
    try await eventuallySession { output.bytes.count == 1 }
    output.level = 0.5
    try await eventuallySession { await frames.pending == 1 }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .jarvis }
    #expect(abs(session.orb.level - 0.1) < 0.000001)
    output.level = 1
    await frames.advance()
    try await eventuallySession { session.orb.level > 0.1 }
    #expect(abs(session.orb.level - 0.28) < 0.000001)
    await socket.text(event(4, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":0}"#))
    await socket.text(event(5, "reply_end", #"{"reply_id":"r","text":"One.","reason":"complete"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "One." } }
    output.callbacks[0]()
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .idle }
    await session.unpair()
}

@MainActor @Test func silencedStreamingReplyMovesTheOrbUntilItsEnd() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper()
    store.record = try testPairingCredentials()
    let time = OrbTestTime()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, orbNow: { time.now }, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.setVoiceOn(false)
    await socket.text(event(2, "reply_delta", #"{"reply_id":"r","text":"Still writing"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "Still writing" } }
    time.now = 10.5
    try await eventuallySession { await frames.pending == 1 }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .jarvis }
    #expect(abs(session.orb.level - 0.04956333428234101) < 0.000001)
    await socket.text(event(3, "reply_end", #"{"reply_id":"r","text":"Still writing after Silence.","reason":"complete"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "Still writing after Silence." } }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .idle }
    #expect(session.playback.playingSpeechID == nil)
    await session.unpair()
}

@MainActor @Test func turningVoiceOffDuringPlaybackKeepsStreamingTextAndOrb() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper(), output = FakeSpeechOutput()
    store.record = try testPairingCredentials()
    let time = OrbTestTime()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: SpeechPlayer(session: SpeechSession(), output: output),
                             orbNow: { time.now }, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "reply_delta", #"{"reply_id":"r","text":"The reply"}"#))
    await socket.text(event(3, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(4, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"The reply","byte_length":2}"#))
    socket.input.yield(.binary(Data([0, 64])))
    try await eventuallySession { output.bytes.count == 1 }
    await session.setVoiceOn(false)
    #expect(session.playback.playingSpeechID == nil && output.stops > 0)
    await socket.text(event(5, "reply_delta", #"{"reply_id":"r","text":" keeps arriving"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "The reply keeps arriving" } }
    time.now = 10.5
    try await eventuallySession { await frames.pending == 1 }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .jarvis }
    #expect(abs(session.orb.level - 0.04956333428234101) < 0.000001)
    await session.unpair()
    #expect(session.orb.speaker == .idle && session.orb.level == 0)
}

@MainActor @Test func orbResumesAfterUnpairingAndPairingInTheForeground() async throws {
    let store = MemoryPairingStore(), first = FakeSocket(), second = FakeSocket(), frames = FakeSleeper()
    store.record = try testPairingCredentials()
    let service = PairingService { _ in
        (Data(#"{"version":1,"client_id":"iphone-1","device_token":"secret","mac_name":"Mac","origin":"https://mac.example"}"#.utf8), 201)
    }
    var connections = 0
    let session = AppSession(store: store, service: service, makeClient: { _ in
        connections += 1
        let socket = connections == 1 ? first : second
        return HubClient(transport: HubTransport(connect: { _ in socket }))
    }, makePaths: { nil }, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await first.ready()
    try await eventuallySession { session.connection.canSend }
    await session.unpair()
    try await eventuallySession { await frames.pending == 0 }
    await session.pair(try PairingInput(origin: "https://mac.example", code: "001234"))
    await second.ready()
    try await eventuallySession { session.connection.canSend }
    await session.setVoiceOn(false)
    await second.text(event(2, "reply_delta", #"{"reply_id":"r","text":"A new pairing"}"#))
    try await eventuallySession { session.conversation.rows.contains { $0.text == "A new pairing" } }
    try await eventuallySession { await frames.pending == 1 }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .jarvis }
    await session.unpair()
}

@MainActor @Test func orbFollowsTheUsersTalkAndIdlesAfterSendingOrCancelling() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper(), recorder = FakeTalkRecorder()
    store.record = try testPairingCredentials()
    let talk = TalkController(recorder: recorder, permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    recorder.level = 0.5
    await talk.press()
    try await eventuallySession { await frames.pending == 1 }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .user }
    #expect(abs(session.orb.level - 0.1) < 0.000001)
    recorder.level = 1
    await frames.advance()
    try await eventuallySession { session.orb.level > 0.1 }
    #expect(abs(session.orb.level - 0.28) < 0.000001)
    await talk.release()
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .idle }
    await talk.press()
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .user }
    await talk.cancel()
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .idle }
    await session.unpair()
}

@MainActor @Test func startingATalkMovesTheOrbFromJarvisToTheUser() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper()
    let recorder = FakeTalkRecorder(), output = FakeSpeechOutput()
    store.record = try testPairingCredentials()
    let talk = TalkController(recorder: recorder, permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk, playback: SpeechPlayer(session: SpeechSession(), output: output),
                             orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([0, 64])))
    try await eventuallySession { output.bytes.count == 1 }
    output.level = 0.5
    try await eventuallySession { await frames.pending == 1 }
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .jarvis }
    recorder.level = 1
    await talk.press()
    #expect(session.playback.playingSpeechID == nil && output.stops > 0)
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .user }
    #expect(abs(session.orb.level - 0.28) < 0.000001)
    await talk.cancel()
    await frames.advance()
    try await eventuallySession { session.orb.speaker == .idle }
    await session.unpair()
}

@MainActor @Test func unpairStopsOrbFramesAfterAReentrantForegroundTransition() async throws {
    let store = MemoryPairingStore(), socket = FakeSocket(), frames = FakeSleeper()
    let talk = TalkController(recorder: FakeTalkRecorder(), permission: { true })
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk, orbSleep: { try await frames.sleep($0) })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    try await eventuallySession { await frames.pending == 1 }
    var begun: CheckedContinuation<Bool, Never>?
    talk.configure(begin: { _ in await withCheckedContinuation { begun = $0 } }, cancel: { _ in }, deliver: { _, _, _ in })
    await talk.press()
    try await eventuallySession { begun != nil }
    let unpairing = Task { await session.unpair() }
    try await eventuallySession { talk.state == .cancelled }
    await session.setForeground(true)
    begun?.resume(returning: true); begun = nil
    await unpairing.value
    #expect(session.credentials == nil && session.orb.speaker == .idle)
    try await eventuallySession { await frames.pending == 0 }
}
