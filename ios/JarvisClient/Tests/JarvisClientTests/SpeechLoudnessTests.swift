import AVFoundation
import Foundation
import Testing
@testable import JarvisClient

// The fixtures are Qwen3-TTS sentences as the Hub sends them: 24 kHz mono PCM16LE at its -25 LUFS reference.
private let languages = ["en", "it"]
private let hubPieceBytes = 11_520 * 2
private let renderRate = 48_000.0

@MainActor @Test(arguments: languages) func hubSpeechArrivesAtTheHubsReferenceLevel(language: String) throws {
    let pcm = try hubSpeech(language)

    #expect(abs(integratedLoudness(try render(pieces(pcm), through: .plain)) - -25) < 1)
}

@MainActor @Test(arguments: languages) func speechIsLevelledToAboutMinus16LUFSWithAPeakUnderMinus1dBFS(language: String) throws {
    let played = try render(pieces(try hubSpeech(language)), through: .speechOutput)

    #expect(abs(integratedLoudness(played) - -16) < 1)
    #expect(played.map(abs).max()! <= pow(10, -1 / 20))
}

@MainActor @Test func streamedPiecesPlayWithoutASeamBetweenThem() throws {
    let pcm = try hubSpeech("en")

    let whole = try render([pcm], through: .speechOutput)
    let streamed = try render(pieces(pcm), through: .speechOutput)

    #expect(whole.count == streamed.count)
    #expect(zip(whole, streamed).map { abs($0 - $1) }.max()! < 1e-4)
}

private func hubSpeech(_ language: String) throws -> Data {
    try Data(contentsOf: #require(Bundle.module.url(forResource: "hub-speech-\(language)", withExtension: "pcm", subdirectory: "Fixtures")))
}

private func pieces(_ pcm: Data) -> [Data] {
    stride(from: 0, to: pcm.count, by: hubPieceBytes).map { pcm.subdata(in: $0..<min($0 + hubPieceBytes, pcm.count)) }
}

private enum Chain { case plain, speechOutput }

/// Renders offline what the phone would play, with a little tail for the limiter's lookahead and resampling.
@MainActor private func render(_ clips: [Data], through chain: Chain) throws -> [Float] {
    let engine = AVAudioEngine()
    let format = try #require(AVAudioFormat(standardFormatWithSampleRate: renderRate, channels: 1))
    try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 4_096)
    let source = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24_000, channels: 1, interleaved: false))
    var play: (Data) throws -> Void
    switch chain {
    case .speechOutput:
        let output = NativeSpeechOutput(sharing: engine)
        play = { try output.play($0) {} }
    case .plain:
        let node = AVAudioPlayerNode()
        engine.attach(node); engine.connect(node, to: engine.mainMixerNode, format: source)
        play = { pcm in
            node.scheduleBuffer(try floatBuffer(pcm, format: source))
            if !node.isPlaying { node.play() }
        }
    }
    try engine.start()
    for clip in clips { try play(clip) }
    let seconds = Double(clips.reduce(0) { $0 + $1.count } / 2) / 24_000 + 0.1
    let total = AVAudioFramePosition(seconds * renderRate)
    let buffer = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: engine.manualRenderingMaximumFrameCount))
    var rendered: [Float] = []
    while engine.manualRenderingSampleTime < total {
        let frames = min(buffer.frameCapacity, AVAudioFrameCount(total - engine.manualRenderingSampleTime))
        #expect(try engine.renderOffline(frames, to: buffer) == .success)
        rendered += UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength))
    }
    engine.stop()
    return rendered
}

private func floatBuffer(_ pcm: Data, format: AVAudioFormat) throws -> AVAudioPCMBuffer {
    let buffer = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(pcm.count / 2)))
    buffer.frameLength = buffer.frameCapacity
    pcm.withUnsafeBytes { raw in
        for index in 0..<Int(buffer.frameLength) {
            buffer.floatChannelData![0][index] = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: index * 2, as: Int16.self))) / 32_768
        }
    }
    return buffer
}

/// ITU-R BS.1770-4 integrated loudness at 48 kHz: K-weighting, 400 ms blocks every 100 ms, absolute and relative gates.
private func integratedLoudness(_ samples: [Float]) -> Double {
    var weighted = samples.map(Double.init)
    for (b, a) in [([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585]),
                   ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])] {
        var x1 = 0.0, x2 = 0.0, y1 = 0.0, y2 = 0.0
        for index in weighted.indices {
            let x = weighted[index]
            let y = b[0] * x + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
            x2 = x1; x1 = x; y2 = y1; y1 = y
            weighted[index] = y
        }
    }
    let block = Int(renderRate * 0.4), step = Int(renderRate * 0.1)
    let powers = stride(from: 0, through: weighted.count - block, by: step).map { start in
        weighted[start..<start + block].reduce(0) { $0 + $1 * $1 } / Double(block)
    }
    func lufs(_ power: Double) -> Double { -0.691 + 10 * log10(power) }
    func mean(_ values: [Double]) -> Double { values.reduce(0, +) / Double(values.count) }
    let audible = powers.filter { lufs($0) > -70 }
    let relativeGate = lufs(mean(audible)) - 10
    return lufs(mean(audible.filter { lufs($0) > relativeGate }))
}
