import AVFoundation
import SwiftUI

@main
struct AudioSpikeApp: App {
    var body: some Scene { WindowGroup { ExperimentView() } }
}

enum RecordingSetup: String, CaseIterable, Identifiable {
    case mix = "Mix with music"
    case duck = "Duck music"
    case pause = "Pause other audio"
    var id: String { rawValue }
    var options: AVAudioSession.CategoryOptions {
        switch self {
        case .mix: return [.mixWithOthers, .allowBluetoothHFP, .allowBluetoothA2DP]
        case .duck: return [.duckOthers, .allowBluetoothHFP, .allowBluetoothA2DP]
        case .pause: return [.allowBluetoothHFP, .allowBluetoothA2DP]
        }
    }
}

enum PlaybackSetup: String, CaseIterable, Identifiable {
    case duck = "Duck other audio"
    case spoken = "Duck and interrupt spoken audio"
    case plain = "Plain playback"
    var id: String { rawValue }
    var options: AVAudioSession.CategoryOptions {
        switch self {
        case .duck: return [.duckOthers]
        case .spoken: return [.duckOthers, .interruptSpokenAudioAndMixWithOthers]
        case .plain: return []
        }
    }
}

@MainActor
final class Experiment: ObservableObject {
    @Published var recordingSetup = RecordingSetup.mix
    @Published var playbackSetup = PlaybackSetup.duck
    @Published var microphone = "Built-in microphone"
    @Published var delay = 5
    @Published var status = "Choose a run. Results have not been measured."
    @Published var entries: [String] = []
    @Published var running = false
    private let session = AVAudioSession.sharedInstance()
    private var recorder: AVAudioRecorder?
    private var player: AVAudioPlayer?
    private var operation: Task<Void, Never>?
    private let recordingURL = FileManager.default.temporaryDirectory.appendingPathComponent("audio-spike.caf")

    func record() {
        guard !running else { return }
        running = true
        operation = Task {
            let permitted = await AVAudioApplication.requestRecordPermission()
            guard permitted, !Task.isCancelled else {
                finish("Recording cancelled or microphone refused. Allow it in Settings.")
                return
            }
            do {
                try session.setCategory(.playAndRecord, mode: .default, options: recordingSetup.options)
                try session.setActive(true)
                let portType: AVAudioSession.Port = microphone == "Built-in microphone" ? .builtInMic : .bluetoothHFP
                guard let input = session.availableInputs?.first(where: { $0.portType == portType }) else {
                    throw ExperimentError.message("Selected microphone is unavailable. Connect AirPods or choose the built-in microphone.")
                }
                try session.setPreferredInput(input)
                let capture = try AVAudioRecorder(url: recordingURL, settings: [
                    AVFormatIDKey: kAudioFormatLinearPCM,
                    AVSampleRateKey: 16000,
                    AVNumberOfChannelsKey: 1,
                    AVLinearPCMBitDepthKey: 16,
                    AVLinearPCMIsFloatKey: false
                ])
                recorder = capture
                guard capture.record() else { throw ExperimentError.message("Recording could not start.") }
                log("Recording 5 s: \(recordingSetup.rawValue); requested \(microphone). \(route)")
                try await Task.sleep(for: .seconds(5))
                finish("Recording finished. Check music recovery; playback is ready.")
            } catch is CancellationError {
                finish("Stopped.")
            } catch { finish(error.localizedDescription) }
        }
    }

    func play(delayed: Bool) {
        guard !running else { return }
        running = true
        let seconds = delayed ? delay : 0
        let setup = playbackSetup
        operation = Task {
            do {
                log("Playback scheduled in \(seconds) s: \(setup.rawValue). Audio session is inactive until playback.")
                try await Task.sleep(for: .seconds(seconds))
                try session.setCategory(.playback, mode: .spokenAudio, options: setup.options)
                try session.setActive(true)
                let audio = try AVAudioPlayer(contentsOf: recordingURL)
                player = audio
                guard audio.play() else { throw ExperimentError.message("Playback could not start.") }
                log("Playback started. \(route)")
                try await Task.sleep(for: .seconds(audio.duration))
                finish("Playback finished. Check that other audio recovered.")
            } catch is CancellationError {
                finish("Stopped.")
            } catch { finish("\(error.localizedDescription) Record a five-second clip first if none exists.") }
        }
    }

    func stop() { operation?.cancel() }

    private var route: String {
        let inputs = session.currentRoute.inputs.map { "\($0.portName) [\($0.portType.rawValue)]" }.joined(separator: ", ")
        let outputs = session.currentRoute.outputs.map { "\($0.portName) [\($0.portType.rawValue)]" }.joined(separator: ", ")
        return "Input: \(inputs). Output: \(outputs)."
    }

    private func log(_ text: String) {
        status = text
        entries.insert("\(Date().formatted(date: .omitted, time: .standard)) \(text)", at: 0)
    }

    private func finish(_ text: String) {
        recorder?.stop()
        recorder = nil
        player?.stop()
        player = nil
        do { try session.setActive(false, options: .notifyOthersOnDeactivation) }
        catch { log("Audio session release failed: \(error.localizedDescription)") }
        running = false
        log(text)
    }
}

enum ExperimentError: LocalizedError {
    case message(String)
    var errorDescription: String? { switch self { case .message(let text): return text } }
}

struct ExperimentView: View {
    @StateObject private var experiment = Experiment()
    var body: some View {
        NavigationStack {
            Form {
                Section("E1: music during recording") {
                    Picker("Recording", selection: $experiment.recordingSetup) {
                        ForEach(RecordingSetup.allCases) { Text($0.rawValue).tag($0) }
                    }
                    Picker("Microphone", selection: $experiment.microphone) {
                        Text("Built-in microphone").tag("Built-in microphone")
                        Text("AirPods microphone").tag("AirPods microphone")
                    }
                    Button("Record 5 seconds") { experiment.record() }
                }.disabled(experiment.running)
                Section("E2: playback and recovery") {
                    Picker("Playback", selection: $experiment.playbackSetup) {
                        ForEach(PlaybackSetup.allCases) { Text($0.rawValue).tag($0) }
                    }
                    Button("Play recorded clip") { experiment.play(delayed: false) }
                }.disabled(experiment.running)
                Section("E3: playback after leaving the app") {
                    Picker("Delay", selection: $experiment.delay) {
                        Text("5 seconds").tag(5)
                        Text("30 seconds").tag(30)
                        Text("2 minutes").tag(120)
                        Text("10 minutes").tag(600)
                    }
                    Button("Schedule playback") { experiment.play(delayed: true) }
                }.disabled(experiment.running)
                Section {
                    Text(experiment.status).accessibilityAddTraits(.updatesFrequently)
                    if experiment.running { Button("Stop run") { experiment.stop() } }
                    ShareLink("Share run log", item: experiment.entries.reversed().joined(separator: "\n"))
                        .disabled(experiment.entries.isEmpty)
                }
                Section("Run log") {
                    if experiment.entries.isEmpty { Text("No runs yet. Start other audio, then choose a run.") }
                    ForEach(Array(experiment.entries.enumerated()), id: \.offset) { _, entry in Text(entry).font(.footnote) }
                }
            }.navigationTitle("Audio experiments")
        }
    }
}
