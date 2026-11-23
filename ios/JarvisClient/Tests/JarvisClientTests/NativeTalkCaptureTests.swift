import AVFoundation
import Foundation
import Testing
@testable import JarvisClient

@Test @MainActor func nativeLiveConfigurationChangeFromAudioQueueReachesMainActor() async throws {
    let engine = AVAudioEngine(), changes = CaptureLimit()
    let observer = NativeLiveCaptureSource.observeConfigurationChanges(of: engine) {
        MainActor.preconditionIsolated()
        changes.calls += 1
    }
    defer { observer.cancel() }
    let notification = EngineConfigurationChange(engine: engine)
    await withCheckedContinuation { continuation in
        DispatchQueue.global().async {
            notification.post()
            continuation.resume()
        }
    }
    try await eventuallySession { changes.calls == 1 }
}

@Test @MainActor func nativeTapConvertsPcmOnBackgroundThread() async throws {
    let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000,
                                          channels: 1, interleaved: false))
    let pcm = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 320))
    pcm.frameLength = 320
    for index in 0..<320 { pcm.floatChannelData![0][index] = 0.5 }
    let buffer = TalkPCMBuffer()
    let handler = try NativeTalkCaptureSource.makeTapHandler(buffer: buffer, limitReached: {})
    let invocation = TapInvocation(handler: handler, pcm: pcm)
    await withCheckedContinuation { continuation in
        DispatchQueue.global().async {
            invocation.call()
            continuation.resume()
        }
    }
    #expect(abs(buffer.level - 0.5) < 0.000001)
    let bytes = buffer.finish()
    #expect(buffer.level == 0)
    #expect(bytes.count == 640)
    #expect(bytes == Data((0..<320).flatMap { _ in [UInt8(0), UInt8(64)] }))
}

@Test @MainActor func nativeTapReachesByteLimitOnMainActor() async throws {
    let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000,
                                          channels: 1, interleaved: false))
    let pcm = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 960))
    pcm.frameLength = 960
    for index in 0..<960 { pcm.floatChannelData![0][index] = 0.5 }
    let buffer = TalkPCMBuffer(), limit = CaptureLimit()
    buffer.append(Data(repeating: 0, count: TalkPCMBuffer.maximumBytes - 2))
    let handler = try NativeTalkCaptureSource.makeTapHandler(buffer: buffer) {
        MainActor.preconditionIsolated()
        limit.calls += 1
    }
    let invocation = TapInvocation(handler: handler, pcm: pcm)
    await withCheckedContinuation { continuation in
        DispatchQueue.global().async {
            invocation.call()
            invocation.call()
            continuation.resume()
        }
    }
    try await eventuallySession { limit.calls == 1 }
    #expect(buffer.finish().count == TalkPCMBuffer.maximumBytes)
}

@MainActor private final class CaptureLimit {
    var calls = 0
}

// Each invocation transfers its PCM to one background queue and is awaited before observation.
private struct TapInvocation: @unchecked Sendable {
    let handler: AVAudioNodeTapBlock
    let pcm: AVAudioPCMBuffer
    func call() { handler(pcm, AVAudioTime(sampleTime: 0, atRate: pcm.format.sampleRate)) }
}

// Only the engine's identity crosses queues; this does not read or change its audio graph.
private struct EngineConfigurationChange: @unchecked Sendable {
    let engine: AVAudioEngine
    func post() { NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: engine) }
}
