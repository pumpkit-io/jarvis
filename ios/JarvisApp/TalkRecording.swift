import AVFoundation
import JarvisClient
import SwiftUI

/// The pill's contents while a Talk records: the pulsing dot, the clock, and how to cancel.
struct RecordingRow: View {
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    private let theme = JarvisTheme()

    var body: some View {
        HStack(spacing: 10) {
            PulsingDot()
            TalkClock(talk: talk)
            Group {
                if talk.isLocked {
                    Button("Cancel") { Task { await talk.discard() } }
                        .buttonStyle(.plain)
                        .foregroundStyle(theme.cyan)
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                        .padding(.vertical, -6)
                        .accessibilityLabel("Cancel Talk")
                } else {
                    Text("‹ Slide to cancel")
                        .foregroundStyle(theme.haze)
                        .opacity(1 - 0.7 * talk.discardProgress)
                        .offset(x: -24 * talk.discardProgress)
                }
            }
            .frame(maxWidth: .infinity)
            // Centres the cancel control in the part of the pill the held bubble doesn't cover.
            .padding(.trailing, 36)
        }
        .font(.system(size: controlSize))
        .accessibilityElement(children: .contain)
    }
}

/// The pill's contents while a Talk is paused: listen back, see its shape and length.
struct PausedRow: View {
    @Bindable var talk: TalkController
    var preview: TalkPreview
    var controlSize: CGFloat
    @State private var levels: [Double] = []
    private let theme = JarvisTheme()

    var body: some View {
        HStack(spacing: 10) {
            Button { preview.toggle(talk.recorded) } label: {
                Image(systemName: preview.playing ? "pause.fill" : "play.fill")
                    .font(.system(size: controlSize, weight: .semibold))
                    .foregroundStyle(theme.cyan)
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
                    .padding(-6)
            }
            .buttonStyle(.plain)
            .padding(6)
            .accessibilityLabel(preview.playing ? "Pause playback" : "Play Talk")
            TimelineView(.animation(paused: !preview.playing)) { _ in
                WaveformBars(levels: levels, progress: preview.progress)
            }
            .frame(maxWidth: .infinity, maxHeight: 24)
            .accessibilityHidden(true)
            TalkClock(talk: talk, hundredths: false)
        }
        .font(.system(size: controlSize))
        .onAppear { levels = TalkWaveform.levels(talk.recorded, bars: 36) }
        .onChange(of: talk.recorded.count) { levels = TalkWaveform.levels(talk.recorded, bars: 36) }
        .accessibilityElement(children: .contain)
    }
}

private struct WaveformBars: View {
    var levels: [Double]
    var progress: Double
    private let theme = JarvisTheme()

    var body: some View {
        HStack(alignment: .center, spacing: 2) {
            ForEach(levels.indices, id: \.self) { index in
                Capsule()
                    .fill(Double(index) / Double(max(1, levels.count)) < progress ? theme.cyan : theme.haze)
                    .frame(width: 3, height: 3 + 19 * levels[index])
            }
        }
        .frame(maxWidth: .infinity)
    }
}

/// The Talk's length to the hundredth of a second while recording, in the User's decimal style.
struct TalkClock: View {
    @Bindable var talk: TalkController
    var hundredths = true

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.03)) { _ in
            Text(Self.format(talk.duration, hundredths: hundredths))
                .monospacedDigit()
                .fontWeight(.semibold)
                .foregroundStyle(JarvisTheme().frost)
        }
        .accessibilityLabel("Recorded \(Int(talk.duration)) seconds")
    }

    static func format(_ seconds: Double, hundredths: Bool) -> String {
        let total = max(0, seconds)
        let whole = Int(total)
        let clock = String(format: "%d:%02d", whole / 60, whole % 60)
        guard hundredths else { return clock }
        let separator = Locale.current.decimalSeparator ?? "."
        return clock + separator + String(format: "%02d", Int((total - Double(whole)) * 100))
    }
}

private struct PulsingDot: View {
    @State private var dim = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Circle()
            .fill(JarvisTheme().signal)
            .frame(width: 8, height: 8)
            .opacity(dim ? 0.05 : 1)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 0.5).repeatForever(autoreverses: true)) { dim = true }
            }
            .accessibilityHidden(true)
    }
}

/// The red dot drops into a bin beside the pill, confirming a discarded Talk.
struct DiscardBin: View {
    @State private var started = false
    private let theme = JarvisTheme()

    private struct Frame {
        var dotY: CGFloat = 0
        var dotScale: CGFloat = 1
        var bin: CGFloat = 0
    }

    var body: some View {
        Circle()
            .fill(theme.deep)
            .overlay { Circle().strokeBorder(theme.rim, lineWidth: 1) }
            .frame(width: 56, height: 56)
            .keyframeAnimator(initialValue: Frame(), trigger: started) { circle, frame in
                circle.overlay {
                    ZStack {
                        Image(systemName: "trash")
                            .font(.system(size: 22, weight: .medium))
                            .foregroundStyle(theme.signal)
                            .scaleEffect(x: 1, y: frame.bin, anchor: .bottom)
                            .opacity(frame.bin > 0.05 ? 1 : 0)
                        Circle()
                            .fill(theme.signal)
                            .frame(width: 8, height: 8)
                            .scaleEffect(frame.dotScale)
                            .offset(y: frame.dotY)
                    }
                }
            } keyframes: { _ in
                KeyframeTrack(\.dotY) {
                    CubicKeyframe(-14, duration: 0.25)
                    CubicKeyframe(6, duration: 0.3)
                    LinearKeyframe(6, duration: 0.8)
                }
                KeyframeTrack(\.dotScale) {
                    LinearKeyframe(1, duration: 0.4)
                    CubicKeyframe(0, duration: 0.2)
                    LinearKeyframe(0, duration: 0.75)
                }
                KeyframeTrack(\.bin) {
                    LinearKeyframe(0, duration: 0.35)
                    SpringKeyframe(1, duration: 0.35)
                    LinearKeyframe(1, duration: 0.65)
                }
            }
            .onAppear { started = true }
            .accessibilityHidden(true)
    }
}

/// Plays a paused Talk back on this phone; the recorded PCM is wrapped as a WAV file in memory.
@MainActor @Observable final class TalkPreview: NSObject, AVAudioPlayerDelegate {
    private(set) var playing = false
    private var player: AVAudioPlayer?
    private var loadedBytes = -1
    private var ownsSession = false

    var progress: Double {
        guard let player, player.duration > 0 else { return 0 }
        return player.currentTime / player.duration
    }

    func toggle(_ pcm: Data) {
        if playing { player?.pause(); playing = false; return }
        if player == nil || loadedBytes != pcm.count {
            player = try? AVAudioPlayer(data: Self.wav(pcm))
            player?.delegate = self
            loadedBytes = pcm.count
        }
        if !ownsSession {
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
            ownsSession = (try? AVAudioSession.sharedInstance().setActive(true)) != nil
        }
        playing = player?.play() ?? false
    }

    func stop() {
        guard player != nil else { return }
        player?.stop(); player = nil; loadedBytes = -1; playing = false
        // Only the session this preview opened is closed, so Jarvis speech that started since keeps playing.
        guard ownsSession else { return }
        ownsSession = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in
            self.playing = false
            self.player?.currentTime = 0
        }
    }

    static func wav(_ pcm: Data) -> Data {
        var data = Data()
        func append(_ text: String) { data.append(contentsOf: Array(text.utf8)) }
        func append32(_ value: UInt32) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        func append16(_ value: UInt16) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        append("RIFF"); append32(UInt32(36 + pcm.count)); append("WAVE")
        append("fmt "); append32(16); append16(1); append16(1); append32(16_000); append32(32_000); append16(2); append16(16)
        append("data"); append32(UInt32(pcm.count))
        data.append(pcm)
        return data
    }
}
