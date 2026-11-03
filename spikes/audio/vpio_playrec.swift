// Plays a WAV through the default output while recording the mic with macOS Voice
// Processing I/O on, so the system echo canceller sees the playback as its reference.
//
//     swift vpio_playrec.swift <reference.wav> <out.wav>
//
// Writes 16 kHz mono float32 audio, the processed mic, for bench_aec.py analyse.

import AVFoundation
import Foundation

let args = CommandLine.arguments
guard args.count == 3 else {
    FileHandle.standardError.write("usage: vpio_playrec.swift <reference.wav> <out.wav>\n".data(using: .utf8)!)
    exit(2)
}

let engine = AVAudioEngine()
let input = engine.inputNode
do {
    // Voice processing must be enabled before the engine starts and before taps read the format.
    try input.setVoiceProcessingEnabled(true)
} catch {
    FileHandle.standardError.write("voice processing unavailable: \(error)\n".data(using: .utf8)!)
    exit(1)
}

let reference = try AVAudioFile(forReading: URL(fileURLWithPath: args[1]))
let player = AVAudioPlayerNode()
engine.attach(player)
engine.connect(player, to: engine.mainMixerNode, format: reference.processingFormat)

let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
let inFormat = input.outputFormat(forBus: 0)
let converter = AVAudioConverter(from: inFormat, to: target)!
let outFile = try AVAudioFile(forWriting: URL(fileURLWithPath: args[2]), settings: target.settings,
                              commonFormat: .pcmFormatFloat32, interleaved: false)

input.installTap(onBus: 0, bufferSize: 1600, format: inFormat) { buffer, _ in
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16_000 / inFormat.sampleRate) + 16
    guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
    var fed = false
    converter.convert(to: converted, error: nil) { _, status in
        if fed {
            status.pointee = .noDataNow
            return nil
        }
        fed = true
        status.pointee = .haveData
        return buffer
    }
    try? outFile.write(from: converted)
}

let done = DispatchSemaphore(value: 0)
player.scheduleFile(reference, at: nil) { done.signal() }
try engine.start()
player.play()
done.wait()
Thread.sleep(forTimeInterval: 1.0)
engine.stop()
print("wrote \(args[2])")
