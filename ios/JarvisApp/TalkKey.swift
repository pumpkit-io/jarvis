import JarvisClient
import SwiftUI

struct TalkKey: View {
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    @Environment(\.colorScheme) private var scheme
    @State private var held = false
    @State private var gestureStarted = false
    @State private var gestureCancelled = false
    private var theme: JarvisTheme { JarvisTheme(scheme: scheme) }
    private var recording: Bool { talk.state == .recording }

    var body: some View {
        VStack(spacing: 8) {
            if recording {
                Text(String(format: "%d:%02d", talk.elapsed / 60, talk.elapsed % 60))
                    .monospacedDigit().fontWeight(.semibold)
            }
            Text(talk.label).fontWeight(.semibold).multilineTextAlignment(.center)
        }
        .font(.system(size: controlSize))
        .frame(maxWidth: .infinity, minHeight: 88)
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .foregroundStyle(recording ? Color.white : theme.keyText)
        .background(recording ? theme.signal : theme.brass, in: RoundedRectangle(cornerRadius: 10))
        .opacity(talk.canPress || recording || talk.state == .authorizing ? 1 : 0.55)
        .disabled(!talk.ready || talk.state == .uploading || talk.state == .thinking)
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { value in
                if !gestureStarted {
                    gestureStarted = true
                    if talk.canPress {
                        held = true
                        Task { await talk.press() }
                    }
                }
                guard !gestureCancelled else { return }
                if held, value.translation.height < -60 {
                    held = false; gestureCancelled = true
                    Task { await talk.cancel() }
                }
            }
            .onEnded { _ in
                gestureStarted = false; gestureCancelled = false
                guard held else { return }
                held = false
                Task { await talk.release() }
            })
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityHint("Hold while speaking. Release to send. Slide up to cancel.")
        .accessibilityValue(recording ? "Recording, \(talk.elapsed) seconds" : talk.ready ? talk.label : "Unavailable")
        .onDisappear { held = false; Task { await talk.cancel() } }
    }
}
