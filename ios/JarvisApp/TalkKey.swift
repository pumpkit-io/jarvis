import JarvisClient
import SwiftUI

struct TalkKey: View {
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    @State private var interaction = UUID()
    @GestureState private var pressed = false
    private let theme = JarvisTheme()
    private var recording: Bool { talk.state == .recording }

    var body: some View {
        Image(systemName: "mic.fill")
            .font(.system(size: max(22, controlSize), weight: .semibold))
            .frame(width: 56, height: 56)
            .foregroundStyle(theme.void)
            .background(theme.cyan, in: Circle())
            .scaleEffect(recording && !talk.holdCancelled ? 80.0 / 56.0 : 1)
            .animation(.easeOut(duration: 0.15), value: recording)
            .contentShape(Circle())
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
            .onChange(of: recording) { _, value in
                #if os(iOS)
                if value { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
                #endif
            }
            .accessibilityElement(children: .ignore)
            .accessibilityAddTraits(.isButton)
            .accessibilityLabel(talk.label)
            .accessibilityAction { talk.activate(interaction: interaction) }
            .accessibilityAction(named: "Cancel Talk") { Task { await talk.controlDisappeared(interaction: interaction) } }
            .accessibilityHint(talk.gesture == .hold ? "Hold while speaking. Release to send. Slide up to cancel." : "Tap to start. Tap again to send. Slide up to cancel.")
            .accessibilityValue(recording ? "Recording, \(talk.elapsed) seconds" : talk.label)
            .onDisappear { Task { await talk.controlDisappeared(interaction: interaction) } }
    }
}
