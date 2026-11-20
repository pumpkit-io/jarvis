import JarvisClient
import SwiftUI

struct TalkKey: View {
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    @Environment(\.colorScheme) private var scheme
    @State private var interaction = UUID()
    @GestureState private var pressed = false
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
        .scaleEffect(pressed && !talk.holdCancelled ? 0.98 : 1)
        .opacity(talk.canPress || recording || talk.state == .authorizing ? 1 : 0.55)
        .disabled(!talk.ready || talk.state == .uploading || talk.state == .thinking)
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .updating($pressed) { _, pressed, _ in pressed = true }
            .onChanged { value in
                talk.holdBegan(interaction: interaction)
                talk.holdMoved(verticalTranslation: value.translation.height, interaction: interaction)
            }
            .onEnded { _ in talk.holdEnded(interaction: interaction) })
        .onChange(of: pressed) { _, value in
            if !value { talk.holdInterrupted(interaction: interaction) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { talk.activate(interaction: interaction) }
        .accessibilityAction(named: "Cancel Talk") { Task { await talk.controlDisappeared(interaction: interaction) } }
        .accessibilityHint(talk.gesture == .hold ? "Hold while speaking. Release to send. Slide up to cancel." : "Tap to start. Tap again to send. Slide up to cancel.")
        .accessibilityValue(recording ? "Recording, \(talk.elapsed) seconds" : talk.ready ? talk.label : "Unavailable")
        .onDisappear { Task { await talk.controlDisappeared(interaction: interaction) } }
    }
}
