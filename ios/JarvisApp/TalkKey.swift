import JarvisClient
import SwiftUI

struct TalkKey: View {
    static let diameter: CGFloat = 44
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    @State private var interaction = UUID()
    @GestureState private var pressed = false
    private let theme = JarvisTheme()
    private var recording: Bool { talk.state == .recording }

    var body: some View {
        ZStack {
            if talk.isLocked {
                Button { Task { await talk.send() } } label: {
                    Image(systemName: "arrow.up")
                        .font(.system(size: max(20, controlSize), weight: .semibold))
                        .frame(width: Self.diameter, height: Self.diameter)
                        .foregroundStyle(theme.void)
                        .background(theme.cyan, in: Circle())
                }
                .buttonStyle(.plain)
            } else {
                Image(systemName: "mic.fill")
                    .font(.system(size: max(20, controlSize), weight: .semibold))
                    .frame(width: Self.diameter, height: Self.diameter)
                    .foregroundStyle(theme.void)
                    .background(theme.cyan, in: Circle())
                    .scaleEffect(recording && !talk.holdCancelled ? 80 / Self.diameter : 1)
                    .animation(.easeOut(duration: 0.15), value: recording)
                    .contentShape(Circle())
                    .gesture(DragGesture(minimumDistance: 0)
                        .updating($pressed) { _, pressed, _ in pressed = true }
                        .onChanged { value in
                            talk.holdBegan(interaction: interaction)
                            talk.holdMoved(horizontalTranslation: value.translation.width,
                                           verticalTranslation: value.translation.height, interaction: interaction)
                        }
                        .onEnded { _ in talk.holdEnded(interaction: interaction) })
                    .onChange(of: pressed) { _, value in
                        if !value && !talk.isLocked { talk.holdInterrupted(interaction: interaction) }
                    }
                    .overlay(alignment: .bottom) {
                        if recording {
                            VStack(spacing: 8) {
                                Image(systemName: "lock.fill")
                                Image(systemName: "chevron.up")
                            }
                            .font(.system(size: controlSize))
                            .foregroundStyle(theme.haze)
                            .padding(12)
                            .background(theme.deep, in: Capsule())
                            .offset(y: -86 - 24 * talk.lockProgress)
                            .opacity(0.6 + 0.4 * talk.lockProgress)
                            .accessibilityHidden(true)
                        }
                    }
            }
        }
        .onChange(of: recording) { _, value in
            #if os(iOS)
            if value { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
            #endif
        }
        .onChange(of: talk.isLocked) { _, value in
            #if os(iOS)
            if value { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
            #endif
        }
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(talk.label)
        .accessibilityAction { talk.activate(interaction: interaction) }
        .accessibilityAction(named: "Cancel Talk") { Task { await talk.cancel(interaction: interaction) } }
        .accessibilityHint(talk.isLocked ? "Send the recorded Talk" : "Hold while speaking. Release to send. Slide up to lock or left to cancel.")
        .accessibilityValue(recording ? "Recording, \(talk.elapsed) seconds" : talk.label)
        .onDisappear { Task { await talk.controlDisappeared(interaction: interaction) } }
    }
}
