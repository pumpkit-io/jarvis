// Plays a WAV through the default output while recording the mic with macOS Voice
// Processing I/O on, so the system echo canceller sees the playback as its reference.
//
//     swift vpio_playrec.swift <reference.wav> <out.wav>
//
// Writes 16 kHz mono float32 audio, the processed mic, for bench_aec.py analyse.
// Prints `recording_started_at=<unix s>` (the first recorded sample) and
// `playing_at=<unix s>` on stdout, so session.py can place its cues in out.wav's time.

import AVFoundation
import Foundation

setvbuf(stdout, nil, _IONBF, 0)

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("\(message)\n".data(using: .utf8)!)
    exit(1)
}

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
    fail("voice processing unavailable: \(error)")
}

let reference: AVAudioFile
do {
    reference = try AVAudioFile(forReading: URL(fileURLWithPath: args[1]))
} catch {
    fail("cannot read \(args[1]): \(error)")
}
let player = AVAudioPlayerNode()
engine.attach(player)
engine.connect(player, to: engine.mainMixerNode, format: reference.processingFormat)

let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
let inFormat = input.outputFormat(forBus: 0)
guard let converter = AVAudioConverter(from: inFormat, to: target) else { fail("no converter from \(inFormat)") }
let outFile: AVAudioFile
do {
    outFile = try AVAudioFile(forWriting: URL(fileURLWithPath: args[2]), settings: target.settings,
                              commonFormat: .pcmFormatFloat32, interleaved: false)
} catch {
    fail("cannot write \(args[2]): \(error)")
}

var started = false
input.installTap(onBus: 0, bufferSize: 1600, format: inFormat) { buffer, _ in
    if !started {
        started = true
        // The tap fires once a buffer is full, so its first sample is one buffer earlier.
        let first = Date().timeIntervalSince1970 - Double(buffer.frameLength) / inFormat.sampleRate
        print("recording_started_at=\(first)")
    }
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
do {
    try engine.start()
} catch {
    fail("cannot start the audio engine: \(error)")
}
player.play()
print("playing_at=\(Date().timeIntervalSince1970)")
done.wait()
Thread.sleep(forTimeInterval: 1.0)
input.removeTap(onBus: 0)
engine.stop()
// Closing finalises the WAV header; without it the file can read as empty.
outFile.close()
if !started { fail("the microphone delivered no audio") }
print("wrote \(args[2])")
