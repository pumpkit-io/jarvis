import AVFoundation
import Foundation
import Testing
@testable import JarvisClient

@MainActor final class FakeLiveCapture: LiveCaptureSource {
    var starts = 0
    var active = false
    var fails = false
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
    let socket = FakeSocket(), capture = FakeLiveCapture(), sleeper = FakeSleeper()
    var permissionRequests = 0
    var allowed = true
    let session: AppSession

    init() throws {
        let store = MemoryPairingStore(); store.record = try testPairingCredentials()
        let socket = socket, capture = capture, sleeper = sleeper
        var rig: LiveRig?
        let live = LiveModeController(capture: capture, permission: {
            rig?.permissionRequests += 1; return rig?.allowed ?? false
        }, sleep: { try await sleeper.sleep($0) })
        session = AppSession(store: store, makeClient: { _ in HubClient(transport: HubTransport(connect: { _ in socket })) },
                             makePaths: { nil }, liveMode: live)
        rig = self
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
}

private let tenMilliseconds = Data(repeating: 7, count: 320)

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
