import AVFoundation
import Foundation
import Testing
@testable import JarvisClient

@MainActor final class FakeLiveCapture: LiveCaptureSource {
    var starts = 0
    var active = false
    var fails = false
    let speech = FakeSpeechOutput()
    var speechOutput: (any SpeechOutput)? { active ? speech : nil }
    private var sink: (@Sendable (Data) -> Void)?
    func start(frames: @escaping @Sendable (Data) -> Void) throws {
        if fails { throw HubProtocolError.invalidFrame }
        starts += 1; active = true; sink = frames
    }
    func stop() { active = false }
    /// Delivers PCM as the native tap would, still after stop, like a render callback already in flight.
    func emit(_ bytes: Data) { sink?(bytes) }
}

@MainActor private final class LiveRig {
    let socket = FakeSocket(), capture = FakeLiveCapture(), sleeper = FakeSleeper(), orbFrames = FakeSleeper()
    /// Plays whatever the Live mode capture doesn't, as `NativeSpeechOutput` does in the app.
    let standard = FakeSpeechOutput()
    var permissionRequests = 0
    var allowed = true
    let session: AppSession
    let recorder = FakeTalkRecorder()

    init() throws {
        let store = MemoryPairingStore(); store.record = try testPairingCredentials()
        let socket = socket, capture = capture, sleeper = sleeper, orbFrames = orbFrames
        var rig: LiveRig?
        let live = LiveModeController(capture: capture, permission: {
            rig?.permissionRequests += 1; return rig?.allowed ?? false
        }, sleep: { try await sleeper.sleep($0) })
        let player = SpeechPlayer(session: SpeechSession(), output: LiveSpeechRoute(standard: standard, capture: capture))
        session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, talk: TalkController(recorder: recorder, permission: { true }),
                             playback: player, liveMode: live, orbSleep: { try await orbFrames.sleep($0) })
        rig = self
    }

    /// Lets one orb frame run and waits for the orb to show `speaker`.
    func orbShows(_ speaker: OrbModel.Speaker) async throws {
        try await eventuallySession { await orbFrames.pending == 1 }
        await orbFrames.advance()
        try await eventuallySession { await orbFrames.pending == 1 && session.orb.speaker == speaker }
    }

    func voiceState(_ seq: Int, _ state: String) async throws {
        await socket.text(event(seq, "voice_state", #"{"state":"\#(state)"}"#))
        try await eventuallySession { session.liveMode.voiceState.rawValue == state }
    }

    func connect() async throws {
        await session.launch(); await socket.ready()
        try await eventuallySession { session.connection.canSend }
    }

    func setLiveCommands() async -> [HubCommand] { await socket.sent.filter { $0.type == "set_live" } }

    func answer(_ command: HubCommand, _ body: String) async {
        await socket.text(#"{"version":1,"type":"command_result","id":"\#(command.id)",\#(body)}"#)
    }

    func turnOn() async throws {
        let toggle = Task { await session.liveMode.toggle() }
        try await eventuallySession { await setLiveCommands().count == 1 }
        await answer(await setLiveCommands()[0], #""ok":true,"result":{"on":true}"#)
        await toggle.value
        try await eventuallySession { session.liveMode.isOn }
    }

    func receiveChimeAndReply() async throws {
        await socket.text(event(2, "speech_begin", #"{"speech_id":"cue","cue":"chime","sample_rate":24000,"channels":1,"sample_width":2}"#))
        await socket.text(event(3, "speech_clip", #"{"speech_id":"cue","cue":"chime","index":0,"text":"","byte_length":2}"#))
        socket.input.yield(.binary(Data([1, 0])))
        await socket.text(event(4, "speech_end", #"{"speech_id":"cue","cue":"chime","last_clip_index":0}"#))
        try await eventuallySession { capture.speech.bytes.count == 1 }
        await socket.text(event(5, "speech_begin", #"{"speech_id":"reply","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
        for index in 0..<2 {
            await socket.text(event(6 + index, "speech_clip", #"{"speech_id":"reply","reply_id":"r","index":\#(index),"text":"Reply.","byte_length":2}"#))
            socket.input.yield(.binary(Data([UInt8(index + 2), 0])))
        }
        await socket.text(event(8, "speech_end", #"{"speech_id":"reply","reply_id":"r","last_clip_index":1}"#))
        try await voiceState(9, "thinking")
    }
}

private let tenMilliseconds = Data(repeating: 7, count: 320)
/// 10 ms at a quarter of full scale: the level of someone speaking close to the phone.
private let speech = Data((0..<160).flatMap { _ in [UInt8(0x00), 0x20] })

@MainActor @Test func eachVoiceStateSelectsTheOrbSourceTheSpecNames() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    rig.capture.emit(speech)
    try await eventuallySession { await rig.socket.binarySent.count == 1 }

    try await rig.voiceState(2, "listening")
    try await rig.orbShows(.user)
    #expect(rig.session.orb.level > 0.3)

    try await rig.voiceState(3, "thinking")
    try await rig.orbShows(.idle)

    try await rig.voiceState(4, "speaking")
    await rig.socket.text(event(5, "speech_begin", #"{"speech_id":"s","reply_id":"r","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await rig.socket.text(event(6, "speech_clip", #"{"speech_id":"s","reply_id":"r","index":0,"text":"Done.","byte_length":2}"#))
    rig.socket.input.yield(.binary(Data([0, 64])))
    try await eventuallySession { rig.capture.speech.bytes.count == 1 }
    rig.capture.speech.level = 0.2
    try await rig.orbShows(.jarvis)
    #expect(rig.session.orb.level > 0.3)
    await rig.socket.text(event(7, "speech_end", #"{"speech_id":"s","reply_id":"r","last_clip_index":0}"#))
    rig.capture.speech.callbacks[0]()

    try await rig.voiceState(8, "follow_up")
    try await rig.orbShows(.user)
    try await rig.voiceState(9, "idle")
    try await rig.orbShows(.idle)
    await rig.session.unpair()
}

@MainActor @Test(arguments: ["idle", "thinking"])
func liveQuietStatesKeepTheOrbCalmDuringSilentTextStreaming(state: String) async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    await rig.session.setVoiceOn(false)
    try await rig.voiceState(2, state)
    await rig.socket.text(event(3, "reply_delta", #"{"reply_id":"r","text":"Still writing"}"#))
    try await eventuallySession { rig.session.conversation.rows.contains { $0.text == "Still writing" } }

    try await rig.orbShows(.idle)

    await rig.socket.text(event(4, "speech_begin", #"{"speech_id":"cue","cue":"closing","sample_rate":24000,"channels":1,"sample_width":2}"#))
    await rig.socket.text(event(5, "speech_clip", #"{"speech_id":"cue","cue":"closing","index":0,"text":"","byte_length":2}"#))
    rig.socket.input.yield(.binary(Data([1, 0])))
    try await eventuallySession { rig.capture.speech.bytes.count == 1 }
    rig.capture.speech.level = 0.4
    try await rig.orbShows(.idle)

    await rig.session.unpair()
}

@MainActor @Test func whileLiveModeCapturesRepliesPlayThroughTheCaptureEngine() async throws {
    let rig = try LiveRig()
    let standard = FakeSpeechOutput()
    let player = SpeechPlayer(session: SpeechSession(), output: LiveSpeechRoute(standard: standard, capture: rig.capture))
    func reply(_ id: String) {
        player.begin(speechID: id, replyID: id)
        player.append(speechID: id, replyID: id, index: 0, text: "Hi.", pcm: Data([1, 0]))
        player.stop()
    }
    try await rig.connect()
    reply("before")
    try await rig.turnOn()
    reply("live")
    await rig.session.liveMode.toggle()
    reply("after")
    #expect(standard.bytes.count == 2 && rig.capture.speech.bytes.count == 1)
    #expect(rig.capture.speech.stops == 1)
    await rig.session.unpair()
}

@MainActor @Test func aReplyStaysOnTheOutputItStartedOnWhenLiveModeChanges() async throws {
    let rig = try LiveRig()
    let standard = FakeSpeechOutput()
    let player = SpeechPlayer(session: SpeechSession(), output: LiveSpeechRoute(standard: standard, capture: rig.capture))
    try await rig.connect()
    try await rig.turnOn()
    player.begin(speechID: "s", replyID: "r")
    player.append(speechID: "s", replyID: "r", index: 0, text: "One.", pcm: Data([1, 0]))
    rig.capture.speech.level = 0.5
    #expect(player.level == 0.5)
    await rig.session.liveMode.toggle()
    player.append(speechID: "s", replyID: "r", index: 1, text: "Two.", pcm: Data([2, 0]))
    #expect(rig.capture.speech.bytes == [Data([1, 0]), Data([2, 0])] && standard.bytes.isEmpty)
    player.stop()
    #expect(rig.capture.speech.stops == 1 && standard.stops == 0)
    await rig.session.unpair()
}

@MainActor @Test(arguments: ["send", "discard"])
func aTalkPausesLiveFramesFromItsPressUntilItIsSentOrDiscarded(ending: String) async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    rig.capture.emit(tenMilliseconds)
    try await eventuallySession { await rig.socket.binarySent.count == 1 }

    await rig.session.talk.press()
    #expect(rig.session.talk.state == .recording && rig.recorder.active)
    #expect(!rig.capture.active && rig.session.liveMode.isOn)
    rig.capture.emit(tenMilliseconds)
    try await Task.sleep(for: .milliseconds(20))
    #expect(await rig.socket.binarySent.count == 1)

    if ending == "send" { await rig.session.talk.send() } else { await rig.session.talk.discard() }
    #expect(rig.capture.active && rig.capture.starts == 2)
    rig.capture.emit(Data(repeating: 9, count: 320))
    try await eventuallySession { await rig.socket.binarySent.count == 2 }
    #expect(await rig.socket.binarySent.last == Data(repeating: 9, count: 320))
    await rig.session.unpair()
}

@MainActor @Test func liveModeTurnedOffDuringATalkStaysOffAfterIt() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    await rig.session.talk.press()
    await rig.session.liveMode.toggle()
    await rig.session.talk.send()
    #expect(!rig.session.liveMode.isOn && !rig.capture.active && rig.capture.starts == 1)
    #expect(await rig.setLiveCommands().map(\.payload) == [["on": .bool(true)], ["on": .bool(false)]])
    await rig.session.unpair()
}

@MainActor @Test func aLockedTalkKeepsLiveCapturePausedThroughPauseAndResume() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    let interaction = UUID()
    rig.session.talk.holdBegan(interaction: interaction)
    try await eventuallySession { rig.recorder.active }
    await rig.session.talk.holdMoved(verticalTranslation: -200, interaction: interaction).value
    #expect(rig.session.talk.isLocked && !rig.capture.active)
    rig.session.talk.pause()
    #expect(rig.session.talk.state == .paused && !rig.capture.active)
    await rig.session.talk.resume()
    #expect(rig.recorder.active && !rig.capture.active)
    await rig.session.talk.holdEnded(interaction: interaction).value
    #expect(rig.recorder.active && !rig.capture.active)
    await rig.session.talk.discard()
    #expect(!rig.recorder.active && rig.capture.active && rig.capture.starts == 2)
    await rig.session.unpair()
}

@MainActor @Test func cuesPlayThroughTheLiveCaptureWhateverTheSilenceAndAreReported() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    func cue(_ seq: Int, _ id: String, _ name: String, _ pcm: Data) async {
        await rig.socket.text(event(seq, "speech_begin", #"{"speech_id":"\#(id)","cue":"\#(name)","sample_rate":24000,"channels":1,"sample_width":2}"#))
        await rig.socket.text(event(seq + 1, "speech_clip", #"{"speech_id":"\#(id)","cue":"\#(name)","index":0,"text":"","byte_length":\#(pcm.count)}"#))
        rig.socket.input.yield(.binary(pcm))
        await rig.socket.text(event(seq + 2, "speech_end", #"{"speech_id":"\#(id)","cue":"\#(name)","last_clip_index":0}"#))
    }
    func reports(_ id: String) async -> [JSONValue?] {
        await rig.socket.sent.filter { $0.type == "playback_report" && $0.payload["speech_id"] == .string(id) }.map { $0.payload["state"] }
    }

    await cue(2, "chime", "chime", Data([1, 0]))
    try await eventuallySession { rig.capture.speech.bytes.count == 1 }
    await rig.session.setVoiceOn(false)
    #expect(rig.capture.speech.stops == 0 && !rig.session.talk.speaking)
    rig.capture.speech.callbacks[0]()
    try await eventuallySession { await reports("chime") == [.string("started"), .string("finished")] }

    await cue(5, "closing", "closing", Data([2, 0]))
    try await eventuallySession { rig.capture.speech.bytes.count == 2 }
    #expect(rig.capture.speech.bytes == [Data([1, 0]), Data([2, 0])] && rig.standard.bytes.isEmpty)
    rig.capture.speech.callbacks[1]()
    try await eventuallySession { await reports("closing") == [.string("started"), .string("finished")] }
    await rig.session.unpair()
}

@MainActor @Test func aReplyArrivingBeforeTheChimeFinishesPlaysAfterItAndReportsEveryClip() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    try await rig.receiveChimeAndReply()
    #expect(rig.capture.speech.bytes == [Data([1, 0])])
    #expect(rig.capture.speech.stops == 0)
    #expect(rig.standard.bytes.isEmpty)
    #expect(await rig.socket.sent.filter { $0.type == "playback_report" }.map(\.payload) == [
        ["speech_id": .string("cue"), "clip_index": .integer(0), "state": .string("started")]
    ])

    for (index, count) in [3, 5, 6].enumerated() {
        try await eventuallySession { rig.capture.speech.callbacks.count > index }
        rig.capture.speech.callbacks[index]()
        try await eventuallySession { await rig.socket.sent.filter { $0.type == "playback_report" }.count == count }
    }
    let reports = await rig.socket.sent.filter { $0.type == "playback_report" }.map(\.payload)
    #expect(reports == [
        ["speech_id": .string("cue"), "clip_index": .integer(0), "state": .string("started")],
        ["speech_id": .string("cue"), "clip_index": .integer(0), "state": .string("finished")],
        ["speech_id": .string("reply"), "clip_index": .integer(0), "state": .string("started")],
        ["speech_id": .string("reply"), "clip_index": .integer(0), "state": .string("finished")],
        ["speech_id": .string("reply"), "clip_index": .integer(1), "state": .string("started")],
        ["speech_id": .string("reply"), "clip_index": .integer(1), "state": .string("finished")]
    ])
    #expect(rig.session.playback.missedReplies.isEmpty)
    await rig.session.unpair()
}

@MainActor @Test(arguments: ["silence", "talk", "hub_stop"])
func cancellingAReplyWaitingBehindTheChimeCannotPlayItLater(reason: String) async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    try await rig.receiveChimeAndReply()
    if reason == "silence" {
        await rig.session.setVoiceOn(false)
        #expect(rig.capture.speech.stops == 0)
    } else if reason == "talk" {
        await rig.session.talk.press()
        #expect(rig.recorder.active && !rig.capture.active)
        try await eventuallySession { await rig.socket.sent.contains { $0.type == "interrupt" } }
        #expect(await rig.socket.sent.first { $0.type == "interrupt" }?.payload == [
            "speech_id": .string("reply"), "last_clip_index": .integer(-1)
        ])
    } else {
        await rig.socket.text(event(10, "speech_stop", #"{"speech_id":"reply","reply_id":"r","reason":"interrupted"}"#))
        try await rig.voiceState(11, "idle")
    }
    rig.capture.speech.callbacks[0]()
    if reason != "talk" {
        try await eventuallySession { await rig.socket.sent.contains {
            $0.type == "playback_report" && $0.payload["speech_id"] == .string("cue") && $0.payload["state"] == .string("finished")
        } }
    }
    #expect(rig.capture.speech.bytes == [Data([1, 0])])
    #expect(rig.session.playback.missedReplies.contains("r"))
    await rig.session.unpair()
}

@MainActor @Test func liveModeIsOffAtLaunchAndStreamsOnlyOnceTheHubTurnsItOn() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    #expect(!rig.session.liveMode.isOn && rig.capture.starts == 0)
    #expect(await rig.setLiveCommands().isEmpty)

    let toggle = Task { await rig.session.liveMode.toggle() }
    try await eventuallySession { await rig.setLiveCommands().count == 1 }
    #expect(rig.permissionRequests == 1)
    let command = await rig.setLiveCommands()[0]
    #expect(command.payload == ["on": .bool(true)])
    #expect(!rig.session.liveMode.isOn && rig.capture.starts == 0)

    await rig.answer(command, #""ok":true,"result":{"on":true}"#)
    await toggle.value
    try await eventuallySession { rig.session.liveMode.isOn && rig.capture.active }
    rig.capture.emit(tenMilliseconds)
    rig.capture.emit(Data(repeating: 3, count: 3_200))
    try await eventuallySession { await rig.socket.binarySent.count == 2 }
    #expect(await rig.socket.binarySent == [tenMilliseconds, Data(repeating: 3, count: 3_200)])
    await rig.session.unpair()
}

@MainActor @Test func turningLiveModeOffSendsSetLiveOffAfterTheLastFrameAndShowsNothing() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    rig.capture.emit(tenMilliseconds)
    try await eventuallySession { await rig.socket.binarySent.count == 1 }

    await rig.session.liveMode.toggle()
    #expect(!rig.session.liveMode.isOn && !rig.capture.active)
    #expect(rig.session.liveMode.message == nil)
    let commands = await rig.setLiveCommands()
    #expect(commands.map(\.payload) == [["on": .bool(true)], ["on": .bool(false)]])
    #expect(commands[0].id != commands[1].id)
    rig.capture.emit(tenMilliseconds)
    try await Task.sleep(for: .milliseconds(20))
    #expect(await rig.socket.binarySent.count == 1)
    await rig.session.unpair()
}

@MainActor @Test func refusedMicrophoneLeavesLiveModeOffWithTheTalkPermissionMessage() async throws {
    let rig = try LiveRig()
    rig.allowed = false
    try await rig.connect()
    await rig.session.liveMode.toggle()
    #expect(rig.permissionRequests == 1)
    #expect(!rig.session.liveMode.isOn && rig.capture.starts == 0)
    #expect(await rig.setLiveCommands().isEmpty)
    #expect(rig.session.talk.microphoneRefused)
    #expect(rig.session.talk.message == "Jarvis needs the microphone to hear you. Allow it in Settings.")
    #expect(rig.session.liveMode.message == nil)
    await rig.session.unpair()
}

@MainActor @Test func aMacInTextModeLeavesLiveModeOffAndSaysWhy() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    await rig.session.liveMode.toggle()
    try await eventuallySession { await rig.setLiveCommands().count == 1 }
    await rig.answer(await rig.setLiveCommands()[0],
                     #""ok":false,"error":{"code":"voice_unavailable","message":"Live mode needs Jarvis in voice mode on the Mac","retry_after_seconds":null}"#)
    try await eventuallySession { rig.session.liveMode.message != nil }
    #expect(rig.session.liveMode.message == "Live mode needs Jarvis in voice mode on the Mac.")
    #expect(!rig.session.liveMode.isOn && rig.capture.starts == 0)
    await rig.session.unpair()
}

@MainActor @Test func anyOtherHubErrorLeavesLiveModeOffWithTheGenericFailure() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    await rig.session.liveMode.toggle()
    try await eventuallySession { await rig.setLiveCommands().count == 1 }
    await rig.answer(await rig.setLiveCommands()[0],
                     #""ok":false,"error":{"code":"invalid_payload","message":"Live mode is only for an iPhone","retry_after_seconds":null}"#)
    try await eventuallySession { rig.session.liveMode.message != nil }
    #expect(rig.session.liveMode.message == "Couldn't reach the Mac. Not changed.")
    #expect(!rig.session.liveMode.isOn && rig.capture.starts == 0)
    await rig.session.liveMode.toggle()
    try await eventuallySession { await rig.setLiveCommands().count == 2 }
    await rig.session.unpair()
}

@MainActor @Test func theOnMessageShowsThenClearsAfterItsDelay() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    try await rig.turnOn()
    #expect(rig.session.liveMode.message == "Live mode on. Say “Jarvis” to talk.")
    try await eventuallySession { await rig.sleeper.waits.contains(2.5) }
    await rig.sleeper.advance()
    try await eventuallySession { rig.session.liveMode.message == nil }
    #expect(rig.session.liveMode.isOn)
    await rig.session.unpair()
}

@MainActor @Test func aSocketThatDropsBeforeTheAnswerLeavesLiveModeOff() async throws {
    let rig = try LiveRig()
    try await rig.connect()
    await rig.session.liveMode.toggle()
    try await eventuallySession { await rig.setLiveCommands().count == 1 }
    await rig.socket.close()
    try await eventuallySession { rig.session.liveMode.message != nil }
    #expect(rig.session.liveMode.message == "Couldn't reach the Mac. Not changed.")
    #expect(!rig.session.liveMode.isOn && rig.capture.starts == 0)
    await rig.session.unpair()
}

@Test func uplinkAudioGoesOnlyToTheSocketLiveModeWasTurnedOnFor() async throws {
    let transport = FakeTransport()
    let client = HubClient(transport: HubTransport(connect: { await transport.connect($0) }), sleep: { _ in })
    await client.start()
    try await eventually { await transport.socket(0) != nil }
    let first = try #require(await transport.socket(0))
    await first.ready()
    try await eventually { await client.state == .ready }
    #expect(await client.sendAudio(tenMilliseconds) == false)

    let command = HubCommand(type: "set_live", payload: ["on": .bool(true)])
    try await client.send(command)
    await first.text(#"{"version":1,"type":"command_result","id":"\#(command.id)","ok":true,"result":{"on":true}}"#)
    try await eventually { await client.results[command.id] != nil }
    #expect(await client.sendAudio(tenMilliseconds))
    #expect(await client.sendAudio(Data(repeating: 0, count: 100)) == false)
    #expect(await first.binarySent == [tenMilliseconds])

    first.input.finish(throwing: URLError(.networkConnectionLost))
    try await eventually { await transport.socket(1) != nil }
    let second = try #require(await transport.socket(1))
    await second.ready(through: 1)
    try await eventually { await client.state == .ready }
    #expect(await client.sendAudio(tenMilliseconds) == false)
    #expect(await second.binarySent.isEmpty)
    await client.stop()
}

@Test func nativeLiveTapCutsConvertedAudioIntoWholeTenMillisecondFrames() async throws {
    let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false))
    let chunks = LiveChunks()
    let handler = try NativeLiveCaptureSource.makeTapHandler { chunks.add($0) }
    for frames in [2_000, 80] {
        let pcm = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)))
        pcm.frameLength = AVAudioFrameCount(frames)
        for index in 0..<frames { pcm.floatChannelData![0][index] = 0.5 }
        let call = LiveTapCall(handler: handler, pcm: pcm)
        await withCheckedContinuation { continuation in
            DispatchQueue.global().async { call.run(); continuation.resume() }
        }
    }
    #expect(chunks.sizes == [3_200, 640, 320])
    #expect(chunks.first?.prefix(4) == Data([0, 64, 0, 64]))
}

private final class LiveChunks: @unchecked Sendable {
    private let lock = NSLock()
    private var received: [Data] = []
    func add(_ data: Data) { lock.withLock { received.append(data) } }
    var sizes: [Int] { lock.withLock { received.map(\.count) } }
    var first: Data? { lock.withLock { received.first } }
}

// Each call hands its PCM to one background queue and is awaited before the chunks are read.
private struct LiveTapCall: @unchecked Sendable {
    let handler: AVAudioNodeTapBlock
    let pcm: AVAudioPCMBuffer
    func run() { handler(pcm, AVAudioTime(sampleTime: 0, atRate: pcm.format.sampleRate)) }
}

@MainActor @Test func pythonLiveModeFixtureReplaysThroughTheAppSession() async throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let root = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    let exchanges = try #require(root["exchanges"] as? [String: Any])
    let live = try #require(exchanges["live_mode"] as? [String: Any])
    let pairs = try #require(live["commands"] as? [[String: Any]])
    let commands = try pairs.map { try JSONDecoder().decode(HubCommand.self, from: JSONSerialization.data(withJSONObject: $0["command"] as Any)) }
    let uplink = try #require(live["uplink"] as? [[String: Any]]).map { Data(base64Encoded: $0["binary_base64"] as? String ?? "") }
    let downlink = try #require(live["downlink"] as? [[String: Any]])
    let rig = try LiveRig()
    try await rig.connect()

    let toggle = Task { await rig.session.liveMode.toggle() }
    try await eventuallySession { await rig.setLiveCommands().count == 1 }
    let on = await rig.setLiveCommands()[0]
    #expect(on.payload == commands[0].payload)
    var result = try #require(pairs[0]["result"] as? [String: Any]); result["id"] = on.id
    await rig.socket.text(String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self))
    await toggle.value
    try await eventuallySession { rig.session.liveMode.isOn && rig.capture.active }

    for (sent, frame) in uplink.enumerated() {
        rig.capture.emit(try #require(frame))
        try await eventuallySession { await rig.socket.binarySent.count == sent + 1 }
    }
    #expect(await rig.socket.binarySent == uplink.compactMap { $0 })

    var played: [Data] = []
    for frame in downlink {
        if let encoded = frame["binary_base64"] as? String {
            played.append(try #require(Data(base64Encoded: encoded)))
            rig.socket.input.yield(.binary(played.last!))
            continue
        }
        rig.socket.input.yield(.text(try JSONSerialization.data(withJSONObject: frame)))
        let payload = try #require(frame["payload"] as? [String: Any])
        switch frame["type"] as? String {
        case "voice_state":
            try await eventuallySession { rig.session.liveMode.voiceState.rawValue == payload["state"] as? String }
        case "speech_end":
            // Each stream's one clip finishes before the next stream begins, as the phone would play it.
            try await eventuallySession { rig.capture.speech.bytes.count == played.count }
            rig.capture.speech.callbacks[played.count - 1]()
            let finished = HubCommand(type: "playback_report", payload: [
                "speech_id": .string(payload["speech_id"] as? String ?? ""), "clip_index": .integer(0), "state": .string("finished")])
            try await eventuallySession { await rig.socket.sent.contains { $0.type == finished.type && $0.payload == finished.payload } }
        default: break
        }
    }
    #expect(played.count == 3)
    #expect(rig.capture.speech.bytes == played && rig.standard.bytes.isEmpty)
    #expect(await rig.socket.sent.contains { $0.type == "playback_report" && $0.payload == commands[1].payload })
    #expect(rig.session.conversation.rows.contains { $0.text == "Opened." })

    await rig.session.liveMode.toggle()
    #expect(await rig.setLiveCommands().last?.payload == commands[2].payload)
    await rig.session.unpair()
}
