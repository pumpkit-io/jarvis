import Foundation
import Testing
@testable import JarvisClient

@MainActor final class FakeSpeechOutput: SpeechOutput {
    var level: Double = 0
    var bytes: [Data] = []
    var callbacks: [@MainActor @Sendable () -> Void] = []
    var stops = 0
    func play(_ pcm: Data, finished: @escaping @MainActor @Sendable () -> Void) throws {
        bytes.append(pcm); callbacks.append(finished)
    }
    func stop() { stops += 1 }
}

@MainActor final class SpeechSession: TalkAudioSession {
    var active = false
    var activations = 0
    func activate() throws { active = true; activations += 1 }
    func release() { active = false }
}

@MainActor @Test func playbackReportsOnlyFinishedContiguousClipsAndDrainsAfterOutput() throws {
    let output = FakeSpeechOutput(), audio = SpeechSession()
    let player = SpeechPlayer(session: audio, output: output)
    var reports: [HubCommand] = []
    var drains: [String] = []
    player.configure(report: { reports.append($0) }, drained: { drains.append($0) })
    player.begin(speechID: "s", replyID: "r")
    #expect(!audio.active)
    player.append(speechID: "s", replyID: "r", index: 0, text: "One.", pcm: Data([1, 0]))
    player.append(speechID: "s", replyID: "r", index: 1, text: "Two.", pcm: Data([2, 0]))
    #expect(output.bytes == [Data([1, 0]), Data([2, 0])])
    player.end(speechID: "s", replyID: "r", lastIndex: 1)
    #expect(audio.active && drains.isEmpty)
    #expect(player.sentence == "One.")
    #expect(player.lastFinishedIndex == -1)
    output.callbacks[0]()
    #expect(output.bytes == [Data([1, 0]), Data([2, 0])])
    #expect(player.lastFinishedIndex == 0)
    output.callbacks[1]()
    #expect(!audio.active && drains == ["s"])
    #expect(reports.map { $0.payload["state"] } == [.string("started"), .string("finished"), .string("started"), .string("finished")])
}

@MainActor @Test func stoppedStreamCannotCompleteOrStopItsReplacement() throws {
    let output = FakeSpeechOutput(), audio = SpeechSession()
    let player = SpeechPlayer(session: audio, output: output)
    player.begin(speechID: "a", replyID: "ra")
    player.append(speechID: "a", replyID: "ra", index: 0, text: "Old.", pcm: Data([1, 0]))
    player.stop()
    player.begin(speechID: "b", replyID: "rb")
    player.append(speechID: "b", replyID: "rb", index: 0, text: "New.", pcm: Data([2, 0]))
    output.callbacks[0]()
    player.end(speechID: "a", replyID: "ra", lastIndex: 0)
    #expect(player.playingSpeechID == "b")
    #expect(player.lastFinishedIndex == -1)
    #expect(player.sentence == "New.")
}

actor SpeechWait {
    var calls = 0
    var waiter: CheckedContinuation<Void, Never>?
    func sleep() async { calls += 1; await withCheckedContinuation { waiter = $0 } }
    func advance() { waiter?.resume(); waiter = nil }
}

@MainActor @Test func watchdogWaitsForLongClipDrainAndLaterClipReactivates() async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), clock = SpeechWait()
    let player = SpeechPlayer(session: audio, output: output, wait: { await clock.sleep() })
    player.begin(speechID: "s", replyID: "r")
    player.append(speechID: "s", replyID: "r", index: 0, text: "Long.", pcm: Data(repeating: 1, count: 240_000))
    for _ in 0..<20 { await Task.yield() }
    #expect(await clock.calls == 0)
    #expect(audio.active)
    output.callbacks[0]()
    try await eventuallySession { await clock.calls == 1 }
    await clock.advance()
    try await eventuallySession { !audio.active }
    #expect(player.missedReplies.contains("r"))
    player.append(speechID: "s", replyID: "r", index: 1, text: "Later.", pcm: Data([2, 0]))
    #expect(audio.active && player.sentence == "Later.")
    player.end(speechID: "s", replyID: "r", lastIndex: 1)
    output.callbacks[1]()
    #expect(!audio.active && !player.missedReplies.contains("r"))
}

@MainActor @Test func singleNativeSessionRejectsOverlapAndStalePlaybackReleaseCannotStopRecording() throws {
    #if os(macOS)
    let session = NativeTalkAudioSession()
    try session.activatePlayback()
    #expect(throws: HubProtocolError.invalidFrame) { try session.activate() }
    session.releasePlayback()
    try session.activate()
    session.releasePlayback()
    #expect(throws: HubProtocolError.invalidFrame) { try session.activatePlayback() }
    session.release()
    try session.activatePlayback()
    session.releasePlayback()
    #endif
}

@MainActor @Test func invalidIdentityAndClipGapsNeverReachOutputOrAdvanceHeardIndex() {
    let output = FakeSpeechOutput(), audio = SpeechSession()
    let player = SpeechPlayer(session: audio, output: output)
    player.begin(speechID: "s", replyID: "r")
    player.append(speechID: "s", replyID: "wrong", index: 0, text: "Wrong.", pcm: Data([1, 0]))
    player.append(speechID: "s", replyID: "r", index: 1, text: "Gap.", pcm: Data([1, 0]))
    #expect(output.bytes.isEmpty && !audio.active)
    player.append(speechID: "s", replyID: "r", index: 0, text: "Partial.", pcm: Data([1, 0]))
    player.stop()
    #expect(player.lastFinishedIndex == -1 && player.missedReplies.contains("r"))
    output.callbacks[0]()
    #expect(player.lastFinishedIndex == -1 && !audio.active)
}

@MainActor @Test func pythonLivePcmTraversesSoleAppSessionConsumerAndDrainsInBackground() async throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let root = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(root["exchanges"] as? [String: Any])
    let frames = try #require(exchanges["speech_live"] as? [[String: Any]])
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let talk = TalkController(recorder: FakeTalkRecorder(), permission: { true })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    for frame in frames {
        if let encoded = frame["binary_base64"] as? String {
            socket.input.yield(.binary(try #require(Data(base64Encoded: encoded))))
        } else { socket.input.yield(.text(try JSONSerialization.data(withJSONObject: frame))) }
    }
    try await eventuallySession { output.bytes.count == 2 }
    #expect(talk.speaking)
    let background = Task { await session.setForeground(false) }
    try await eventuallySession { await socket.sent.contains { $0.type == "set_presence" && $0.payload["can_show_text"] == .bool(false) } }
    let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await background.value
    #expect(await socket.closed == false)
    output.callbacks[0]()
    #expect(output.bytes.count == 2 && audio.active)
    output.callbacks[1]()
    try await eventuallySession { await socket.closed }
    let reports = await socket.sent.filter { $0.type == "playback_report" }
    #expect(reports.map { $0.payload["state"] } == [.string("started"), .string("finished"), .string("started"), .string("finished")])
    let expected = Data([1, 128, 1, 192, 0, 0, 255, 63, 255, 127, 255, 127])
    #expect(output.bytes == [expected, expected])
    #expect(!audio.active && !talk.speaking)
    await session.unpair()
}

@MainActor @Test(arguments: [false, true]) func repeatedNonActiveScenesKeepOwnedReplyPlaying(acknowledgedBeforeBackground: Bool) async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    let pcm = Data(repeating: 1, count: 240_000)
    await socket.text(event(2, "speech_begin", #"{"speech_id":"long","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", "{\"speech_id\":\"long\",\"reply_id\":\"r\",\"index\":0,\"text\":\"Long sentence.\",\"byte_length\":\(pcm.count)}"))
    socket.input.yield(.binary(pcm))
    await socket.text(event(4, "speech_end", #"{"speech_id":"long","reply_id":"r","last_clip_index":0}"#))
    try await eventuallySession { output.bytes == [pcm] }
    let inactive = Task { await session.setForeground(false) }
    try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
    let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
    if acknowledgedBeforeBackground {
        await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
        await inactive.value
    }
    await session.setForeground(false)
    #expect(await socket.closed == false)
    if !acknowledgedBeforeBackground {
        await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
        await inactive.value
    }
    #expect(await socket.sent.filter { $0.payload["can_show_text"] == .bool(false) }.count == 1)
    #expect(audio.active && player.playingSpeechID == "long")
    #expect(session.connection == .suspended)
    output.callbacks[0]()
    try await eventuallySession { await socket.closed }
    #expect(!audio.active)
    let reports = await socket.sent.filter { $0.type == "playback_report" }
    #expect(reports.map { $0.payload["state"] } == [.string("started"), .string("finished")])
    #expect(reports.last?.payload["clip_index"] == .integer(0))
    await session.unpair()
}

@MainActor @Test(arguments: [false, true], [false, true]) func interclipWindowKeepsOwnedBackgroundDrain(repeatedScene: Bool, expireOriginalWindow: Bool) async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(), clock = SpeechWait()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output, wait: { await clock.sleep() })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    try await eventuallySession { output.bytes.count == 1 }
    if !repeatedScene { output.callbacks[0](); try await eventuallySession { await clock.calls == 1 } }
    let inactive = Task { await session.setForeground(false) }
    try await eventuallySession {
        let closed = await socket.closed
        return await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } || closed
    }
    let presence = try #require(await socket.sent.last { $0.payload["can_show_text"] == .bool(false) })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await inactive.value
    if repeatedScene { output.callbacks[0](); try await eventuallySession { await clock.calls == 1 } }
    await session.setForeground(false)
    #expect(await socket.closed == false)
    #expect(audio.active)
    #expect(await clock.calls == 1)
    #expect(await socket.sent.filter { $0.payload["can_show_text"] == .bool(false) }.count == 1)
    if expireOriginalWindow {
        await clock.advance()
        try await eventuallySession { await socket.closed && !audio.active }
        #expect(output.bytes.count == 1)
        #expect(await clock.calls == 1)
        await session.unpair()
        return
    }
    await socket.text(event(4, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":1,"text":"Two.","byte_length":2}"#))
    socket.input.yield(.binary(Data([2, 0])))
    try await eventuallySession { output.bytes.count == 2 }
    try #require(output.callbacks.count == 2)
    #expect(player.sentence == "Two.")
    await clock.advance()
    output.callbacks[1]()
    try await eventuallySession { await clock.calls == 2 }
    await clock.advance()
    try await eventuallySession { await socket.closed && !audio.active }
    let reports = await socket.sent.filter { $0.type == "playback_report" }
    #expect(reports.map { $0.payload["state"] } == [.string("started"), .string("finished"), .string("started"), .string("finished")])
    await session.unpair()
}

@MainActor @Test(arguments: [false, true]) func inactivePlaybackCannotOpenBackgroundWindow(expired: Bool) async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(), clock = SpeechWait()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output, wait: { await clock.sleep() })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    try await eventuallySession { player.speechID == "s" }
    if expired {
        await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
        socket.input.yield(.binary(Data([1, 0])))
        try await eventuallySession { output.bytes.count == 1 }
        output.callbacks[0]()
        try await eventuallySession { await clock.calls == 1 }
        await clock.advance()
        try await eventuallySession { !audio.active && !session.playbackDrainPending }
    }
    await session.setForeground(false)
    #expect(await socket.closed)
    #expect(!audio.active)
    #expect(await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } == false)
    #expect(player.speechID == nil)
    await session.unpair()
}

@MainActor @Test(arguments: [false, true]) func departureDuringForegroundPresenceWaitKeepsOwnedPlayback(resuming: Bool) async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(autoPresence: false)
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { await socket.sent.contains { $0.type == "set_presence" } }
    var pendingTrue = try #require(await socket.sent.last { $0.type == "set_presence" })
    if resuming {
        await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(pendingTrue.id)\",\"ok\":true,\"result\":{\"can_show_text\":true}}")
        try await eventuallySession { session.connection.canSend }
    }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    await socket.text(event(4, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":0}"#))
    try await eventuallySession { output.bytes.count == 1 }
    var resume: Task<Void, Never>?
    if resuming {
        let firstDeparture = Task { await session.setForeground(false) }
        try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
        let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
        await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
        await firstDeparture.value
        resume = Task { await session.setForeground(true) }
        try await eventuallySession { await socket.sent.last { $0.type == "set_presence" }?.payload["can_show_text"] == .bool(true) }
        pendingTrue = try #require(await socket.sent.last { $0.type == "set_presence" })
    }
    let departure = Task { await session.setForeground(false) }
    try await eventuallySession {
        let closed = await socket.closed
        return await socket.sent.last { $0.type == "set_presence" }?.payload["can_show_text"] == .bool(false) || closed
    }
    #expect(await socket.closed == false)
    let pendingFalse = try #require(await socket.sent.last { $0.type == "set_presence" && $0.payload["can_show_text"] == .bool(false) })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(pendingTrue.id)\",\"ok\":true,\"result\":{\"can_show_text\":true}}")
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(pendingFalse.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await departure.value; await resume?.value
    #expect(session.connection == .suspended)
    #expect(audio.active && player.sentence == "One.")
    #expect(await socket.closed == false)
    output.callbacks[0]()
    try await eventuallySession { await socket.closed && !audio.active }
    #expect(await socket.sent.last { $0.type == "playback_report" }?.payload["state"] == .string("finished"))
    await session.unpair()
}

@MainActor @Test func repeatedDepartureWaitsForFinalPlaybackReceipt() async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    await socket.text(event(4, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":0}"#))
    try await eventuallySession { await socket.sent.contains { $0.payload["state"] == .string("started") } }
    let departure = Task { await session.setForeground(false) }
    try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
    let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await departure.value
    await socket.blockSend("playback_report")
    output.callbacks[0]()
    try await eventuallySession { await socket.sendWaiter != nil }
    #expect(!audio.active)
    await session.setForeground(false)
    #expect(await socket.closed == false)
    await socket.releaseSend()
    try await eventuallySession { await socket.closed }
    #expect(await socket.sent.filter { $0.type == "playback_report" }.map { $0.payload["state"] } == [.string("started"), .string("finished")])
    await session.unpair()
}

@MainActor @Test func repeatedNonActiveScenesPreserveMissingPresenceDeadline() async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(), clock = FakeSleeper()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { _ in socket }), lifecycleSleep: { try await clock.sleep($0) })
    }, makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    try await eventuallySession { await clock.pending == 0 }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"long","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"long","reply_id":"r","index":0,"text":"Long sentence.","byte_length":240000}"#))
    socket.input.yield(.binary(Data(repeating: 1, count: 240_000)))
    try await eventuallySession { audio.active }
    let inactive = Task { await session.setForeground(false) }
    try await eventuallySession { await clock.pending == 1 }
    try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
    let waits = await clock.waits.count
    await session.setForeground(false)
    #expect(await socket.closed == false)
    #expect(audio.active)
    #expect(await clock.waits.count == waits)
    await clock.advance()
    await inactive.value
    try await eventuallySession { await socket.closed && !audio.active }
    #expect(player.missedReplies.contains("r"))
    await session.unpair()
}

@MainActor @Test func missingAudioReplayRetriesSameCommandAndCompletedReplayIsFresh() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "reply_end", #"{"reply_id":"missed","request_id":"r","text":"Retained.","reason":"complete"}"#))
    try await eventuallySession { session.playback.missedReplies.contains("missed") }
    await session.replay(replyID: "missed")
    let original = try #require(await socket.sent.first { $0.type == "replay_speech" })
    await session.replay(replyID: "missed")
    #expect(await socket.sent.filter { $0.type == "replay_speech" }.allSatisfy { $0.id == original.id })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(original.id)\",\"ok\":true,\"result\":{}}")
    for _ in 0..<20 { await Task.yield() }
    await session.replay(replyID: "missed")
    #expect(await socket.sent.last { $0.type == "replay_speech" }?.id != original.id)
    #expect(session.conversation.rows.count == 1)
    #expect(await socket.sent.contains { ["take_focus", "say_text", "talk_begin"].contains($0.type) } == false)
    await session.unpair()
}

@MainActor @Test(arguments: ["silence", "voice_control", "hub_stop"]) func localSilenceAndHubStopReleaseWithoutFeedbackInterrupt(_ action: String) async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"Partial.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    try await eventuallySession { audio.active }
    if action == "silence" { session.silencePlayback() }
    else if action == "voice_control" { await session.setVoiceOn(false) }
    else {
        await socket.text(event(4, "speech_stop", #"{"speech_id":"s","reply_id":"r","reason":"interrupt"}"#))
        try await eventuallySession { !audio.active }
    }
    #expect(!audio.active && player.playingSpeechID == nil)
    output.callbacks[0]()
    for _ in 0..<20 { await Task.yield() }
    #expect(await socket.sent.contains { $0.type == "interrupt" || $0.payload["state"] == .string("stopped") } == false)
    await session.unpair()
}

@MainActor @Test func rejectedReplayShowsReasonAndNextPlayUsesFreshIdentity() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.replay(replyID: "r")
    let first = try #require(await socket.sent.first { $0.type == "replay_speech" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(first.id)\",\"ok\":false,\"error\":{\"code\":\"silenced\",\"message\":\"Voice is off.\",\"retry_after_seconds\":null}}")
    try await eventuallySession { session.replayMessages["r"] == "Voice is off." }
    await session.replay(replyID: "r")
    let second = try #require(await socket.sent.last { $0.type == "replay_speech" })
    #expect(first.id != second.id)
    #expect(session.replayMessages["r"] == nil)
    await session.unpair()
}

@MainActor @Test func healthyAcknowledgementDiagnosticDoesNotStopSpeechButSocketLossDoes() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore(), output = FakeSpeechOutput(), audio = SpeechSession(), clock = FakeSleeper()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in
        HubClient(transport: HubTransport(connect: { _ in socket }), sleep: { _ in try await Task.sleep(for: .seconds(100)) },
                  acknowledgementSleep: { try await clock.sleep($0) })
    }, makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.takeFocus()
    let unrelated = try #require(await socket.sent.first { $0.type == "take_focus" }?.id)
    await socket.text(event(2, "speech_begin", #"{"speech_id":"long","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"long","reply_id":"r","index":0,"text":"Still speaking.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    try await eventuallySession { audio.active }
    try await eventuallySession { await socket.sent.contains { $0.type == "playback_report" } }
    let started = try #require(await socket.sent.first { $0.type == "playback_report" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(started.id)\",\"ok\":true,\"result\":{}}")
    try await eventuallySession { await clock.pending == 1 }
    for attempt in 1...3 {
        try await eventuallySession { await socket.sent.filter { $0.id == unrelated }.count == attempt }
        try await eventuallySession { await clock.pending > 0 }
        await clock.advance()
    }
    try await eventuallySession { await session.client?.unansweredCommands.contains(unrelated) == true }
    #expect(audio.active && player.playingSpeechID == "long")
    socket.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventuallySession { !audio.active }
    #expect(player.missedReplies.contains("r"))
    await session.unpair()
}

@MainActor @Test func zeroLengthWireClipStopsAndSettlesWithoutClaimingAudibleText() async throws {
    let socket = FakeSocket(), store = MemoryPairingStore(), output = FakeSpeechOutput(), audio = SpeechSession()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"empty","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"empty","reply_id":"r","index":0,"text":"Not audible.","byte_length":0}"#))
    socket.input.yield(.binary(Data()))
    await socket.text(event(4, "speech_end", #"{"speech_id":"empty","reply_id":"r","last_clip_index":0}"#))
    try await eventuallySession { await socket.sent.contains { $0.payload["state"] == .string("stopped") } }
    let stopped = try #require(await socket.sent.first { $0.payload["state"] == .string("stopped") })
    #expect(stopped.payload["clip_index"] == .integer(-1))
    #expect(output.bytes.isEmpty && !audio.active && player.playingSpeechID == nil)
    #expect(player.lastFinishedIndex == -1 && player.missedReplies.contains("r"))
    #expect(player.messages["r"] == "No audio arrived for this reply. Tap Play to try again.")
    await session.unpair()
}

@MainActor @Test func talkStopsRealPlayerBeforeNetworkThenStaleCompletionCannotReleaseRecording() async throws {
    #if os(macOS)
    let audio = NativeTalkAudioSession(), output = FakeSpeechOutput(), source = FakeCaptureSource(), socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let talk = TalkController(recorder: NativeTalkRecorder(session: audio, source: source), permission: { true })
    let wire = CommandLog()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: talk, playback: player, sendCommand: { client, command in
        await MainActor.run {
            if command.type == "interrupt" {
                #expect(player.playingSpeechID == nil && output.stops > 0)
                #expect(!source.active)
            }
            wire.commands.append(command)
        }
        try await client.send(command)
    })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    for index in 0...1 {
        await socket.text(event(3 + index, "speech_clip", "{\"speech_id\":\"s\",\"reply_id\":\"r\",\"index\":\(index),\"text\":\"Sentence.\",\"byte_length\":2}"))
        socket.input.yield(.binary(Data([1, 0])))
    }
    try await eventuallySession { output.bytes.count == 2 }
    output.callbacks[0]()
    await talk.press()
    #expect(source.active)
    #expect(player.playingSpeechID == nil && output.stops > 0)
    try await eventuallySession { await socket.sent.contains { $0.type == "interrupt" } }
    #expect(await socket.sent.filter { $0.type == "interrupt" }.count == 1)
    #expect(await socket.sent.first { $0.type == "interrupt" }?.payload["last_clip_index"] == .integer(0))
    #expect(wire.commands.isEmpty)
    #expect(await socket.sent.contains { $0.payload["state"] == .string("stopped") } == false)
    output.callbacks[1]()
    #expect(source.active)
    #expect(throws: HubProtocolError.invalidFrame) { try audio.activatePlayback() }
    await session.unpair()
    #expect(!source.active)
    #endif
}

@MainActor final class CommandLog { var commands: [HubCommand] = [] }

actor HeldReplayError {
    var waiter: CheckedContinuation<Void, any Error>?
    var waiting: Bool { waiter != nil }
    func hold() async throws { try await withCheckedThrowingContinuation { waiter = $0 } }
    func fail() { waiter?.resume(throwing: URLError(.networkConnectionLost)); waiter = nil }
}

@MainActor @Test(arguments: ["result", "unpair"]) func lateReplayWriteFailurePreservesDefinitiveResultOrDepartedSession(_ action: String) async throws {
    let socket = FakeSocket(), store = MemoryPairingStore(), gate = HeldReplayError()
    store.record = try testPairingCredentials()
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, sendCommand: { client, command in
        try await client.send(command)
        if command.type == "replay_speech" { try await gate.hold() }
    })
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    let replay = Task { await session.replay(replyID: "r") }
    try await eventuallySession { await gate.waiting }
    let command = try #require(await socket.sent.first { $0.type == "replay_speech" })
    if action == "result" {
        await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(command.id)\",\"ok\":false,\"error\":{\"code\":\"silenced\",\"message\":\"Voice is off.\",\"retry_after_seconds\":null}}")
        try await eventuallySession { session.replayMessages["r"] == "Voice is off." }
    } else { await session.unpair() }
    await gate.fail(); await replay.value
    #expect(session.replayMessages["r"] == (action == "result" ? "Voice is off." : nil))
    await session.unpair()
}

@MainActor @Test func returningToForegroundKeepsHealthyPlayingSocketAndQueuedAudio() async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), transport = FakeTransport(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { await transport.connect($0) })) }, makePaths: { nil }, playback: player)
    await session.launch()
    try await eventuallySession { await transport.socket(0) != nil }
    let socket = try #require(await transport.socket(0))
    await socket.ready(); try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"Long sentence.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    await socket.text(event(4, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":0}"#))
    try await eventuallySession { player.playingSpeechID == "s" }
    let background = Task { await session.setForeground(false) }
    try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
    let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await background.value
    await session.setForeground(true)
    try await eventuallySession { session.connection.canSend }
    #expect(await transport.sockets.count == 1)
    #expect(player.playingSpeechID == "s" && audio.active)
    output.callbacks[0]()
    try await eventuallySession { await socket.sent.contains { $0.payload["state"] == .string("finished") } }
    #expect(await socket.closed == false)
    #expect(!audio.active && !player.missedReplies.contains("r"))
    await session.unpair()
}

@MainActor @Test(arguments: [false, true]) func oldWatchdogDrainCannotCloseRenewedPlaybackWindow(endedBeforeFlush: Bool) async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(), clock = SpeechWait()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output, wait: { await clock.sleep() })
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    try await eventuallySession { await socket.sent.contains { $0.payload["state"] == .string("started") } }
    await socket.blockSend("playback_report")
    output.callbacks[0]()
    try await eventuallySession { await socket.sendWaiter != nil }
    try await eventuallySession { await clock.calls == 1 }
    await clock.advance()
    try await eventuallySession { !audio.active }
    await socket.text(event(4, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":1,"text":"Two.","byte_length":2}"#))
    socket.input.yield(.binary(Data([2, 0])))
    await socket.text(event(5, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":1}"#))
    try await eventuallySession { output.bytes.count == 2 }
    let departure = Task { await session.setForeground(false) }
    try await eventuallySession { await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) } }
    let presence = try #require(await socket.sent.last { $0.type == "set_presence" })
    await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    await departure.value
    if endedBeforeFlush { output.callbacks[1](); #expect(!audio.active) }
    await socket.releaseSend(keepingBlocked: true)
    try await eventuallySession {
        let waiting = await socket.sendWaiter != nil
        return await socket.closed || waiting
    }
    #expect(await socket.closed == false)
    #expect(await socket.sent.last { $0.type == "playback_report" }?.payload["state"] == .string("started"))
    if !endedBeforeFlush { #expect(audio.active && player.sentence == "Two."); output.callbacks[1]() }
    await socket.releaseSend(keepingBlocked: true)
    try await eventuallySession {
        let waiting = await socket.sendWaiter != nil
        return await socket.closed || waiting
    }
    #expect(await socket.closed == false)
    #expect(await socket.sent.last { $0.type == "playback_report" }?.payload["state"] == .string("finished"))
    await socket.releaseSend()
    try await eventuallySession { await socket.closed }
    #expect(await socket.sent.filter { $0.type == "playback_report" }.map { $0.payload["clip_index"] } == [.integer(0), .integer(0), .integer(1), .integer(1)])
    await session.unpair()
}

@MainActor @Test func firstDepartureWaitsForReceiptFromForegroundCompletion() async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket()
    let store = MemoryPairingStore(); store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"One.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    await socket.text(event(4, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":0}"#))
    try await eventuallySession { await socket.sent.contains { $0.payload["state"] == .string("started") } }
    await socket.blockSend("playback_report")
    output.callbacks[0]()
    try await eventuallySession { await socket.sendWaiter != nil }
    let departure = Task { await session.setForeground(false) }
    try await eventuallySession {
        let sent = await socket.sent.contains { $0.payload["can_show_text"] == .bool(false) }
        return await socket.closed || sent
    }
    #expect(await socket.closed == false)
    if let presence = await socket.sent.last(where: { $0.payload["can_show_text"] == .bool(false) }) {
        await socket.text("{\"version\":1,\"type\":\"command_result\",\"id\":\"\(presence.id)\",\"ok\":true,\"result\":{\"can_show_text\":false}}")
    }
    await departure.value
    #expect(!audio.active)
    await socket.releaseSend()
    try await eventuallySession { await socket.closed }
    #expect(await socket.sent.filter { $0.type == "playback_report" }.map { $0.payload["state"] } == [.string("started"), .string("finished")])
    await session.unpair()
}

@MainActor @Test func lateVoiceOnResultCannotRestoreSpeechAfterLatestVoiceOff() async throws {
    let output = FakeSpeechOutput(), audio = SpeechSession(), socket = FakeSocket(), store = MemoryPairingStore()
    store.record = try testPairingCredentials()
    let player = SpeechPlayer(session: audio, output: output)
    let session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) }, makePaths: { nil }, playback: player)
    await session.launch(); await socket.ready()
    try await eventuallySession { session.connection.canSend }
    await session.setVoiceOn(true)
    let on = try #require(await socket.sent.last { $0.type == "set_silence" })
    await session.setVoiceOn(false)
    let off = try #require(await socket.sent.last { $0.type == "set_silence" })
    await acknowledgeSilence(socket, on, value: false)
    await socket.text(event(2, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await socket.text(event(3, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"Old audio.","byte_length":2}"#))
    socket.input.yield(.binary(Data([1, 0])))
    await acknowledgeSilence(socket, off, value: true)
    try await eventuallySession { !session.voicePending }
    #expect(!session.voiceOn && !audio.active && output.bytes.isEmpty)
    await session.setVoiceOn(true)
    let newOn = try #require(await socket.sent.last { $0.type == "set_silence" })
    await acknowledgeSilence(socket, newOn, value: false)
    try await eventuallySession { !session.voicePending }
    #expect(output.bytes.isEmpty)
    #expect(await socket.sent.contains { $0.type == "replay_speech" } == false)
    await session.unpair()
}
