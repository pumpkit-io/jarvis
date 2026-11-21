import JarvisClient
import SwiftUI

struct TalkKey: View {
    static let diameter: CGFloat = 44
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    var level: Double = 0
    /// Runs before a paused Talk resumes, so a playing preview gives the microphone back first.
    var beforeResume: () -> Void = {}
    @State private var interaction = UUID()
    @State private var drag = CGSize.zero
    @GestureState private var pressed = false
    private let theme = JarvisTheme()
    private static let heldDiameter: CGFloat = 80
    private static let capsuleRise: CGFloat = -106
    private var recording: Bool { talk.state == .recording }
    private var paused: Bool { talk.state == .paused }
    private var holding: Bool { recording && !talk.holdCancelled && !talk.isLocked }
    private var locked: Bool { recording && talk.isLocked }

    var body: some View {
        ZStack {
            if paused {
                sendButton(held: false)
            } else if locked {
                sendButton(held: true)
            } else {
                microphone
            }
        }
        .onChange(of: recording) { _, value in
            #if os(iOS)
            if value { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
            #endif
        }
        .onChange(of: talk.isLocked) { _, value in
            if value { withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) { drag = .zero } }
            #if os(iOS)
            if value { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
            #endif
        }
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(talk.label)
        .accessibilityAction { talk.activate(interaction: interaction) }
        .accessibilityAction(named: "Cancel Talk") { Task { await talk.cancel(interaction: interaction) } }
        .accessibilityHint(talk.isLocked || paused ? "Send the recorded Talk" : "Hold while speaking. Release to send. Slide up to lock or left to cancel.")
        .accessibilityValue(recording ? "Recording, \(talk.elapsed) seconds" : paused ? "Paused" : talk.label)
        .onDisappear { Task { await talk.controlDisappeared(interaction: interaction) } }
        // Outside the key's accessibility element, so pause and resume stay their own buttons.
        .overlay(alignment: .bottom) { capsule }
    }

    private var microphone: some View {
        Image(systemName: "mic.fill")
            .font(.system(size: max(20, controlSize), weight: .semibold))
            .frame(width: Self.diameter, height: Self.diameter)
            .foregroundStyle(theme.void)
            .background(theme.cyan, in: Circle())
            .scaleEffect(holding ? Self.heldDiameter / Self.diameter : 1)
            .background {
                if holding {
                    TalkWaves(level: level, radius: Self.heldDiameter / 2)
                        .transition(.scale(scale: 0.6).combined(with: .opacity))
                }
            }
            .animation(.easeOut(duration: 0.15), value: holding)
            .offset(drag)
            .contentShape(Circle())
            // Global coordinates, because the bubble moves under the finger that drives it.
            .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .global)
                .updating($pressed) { _, pressed, _ in pressed = true }
                .onChanged { value in
                    talk.holdBegan(interaction: interaction)
                    talk.holdMoved(horizontalTranslation: value.translation.width,
                                   verticalTranslation: value.translation.height, interaction: interaction)
                    guard holding else { return }
                    drag = CGSize(width: min(0, value.translation.width),
                                  height: max(-TalkController.lockDistance, min(0, value.translation.height)))
                }
                .onEnded { _ in
                    talk.holdEnded(interaction: interaction)
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) { drag = .zero }
                })
            .onChange(of: pressed) { _, value in
                if !value && !talk.isLocked { talk.holdInterrupted(interaction: interaction) }
            }
    }

    private func sendButton(held: Bool) -> some View {
        Button { Task { await talk.send() } } label: {
            Image(systemName: "arrow.up")
                .font(.system(size: max(20, controlSize), weight: .semibold))
                .frame(width: Self.diameter, height: Self.diameter)
                .foregroundStyle(theme.void)
                .background(theme.cyan, in: Circle())
                .scaleEffect(held ? Self.heldDiameter / Self.diameter : 1)
                .background {
                    if held { TalkWaves(level: level, radius: Self.heldDiameter / 2) }
                }
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder private var capsule: some View {
        if holding {
            let progress = talk.lockProgress
            VStack(spacing: 8 * (1 - progress)) {
                Image(systemName: "lock.fill")
                Image(systemName: "chevron.up")
                    .opacity(1 - progress)
                    .frame(height: 14 * (1 - progress))
            }
            .font(.system(size: controlSize))
            .foregroundStyle(theme.haze)
            .padding(12)
            .background(theme.deep, in: Capsule())
            .overlay { Capsule().strokeBorder(theme.rim, lineWidth: 1) }
            .offset(y: Self.capsuleRise + 16 * progress)
            .accessibilityHidden(true)
        } else if locked {
            capsuleButton("pause.fill", label: "Pause Talk") { talk.pause() }
        } else if paused {
            capsuleButton("mic.fill", label: "Resume Talk") { beforeResume(); Task { await talk.resume() } }
        }
    }

    private func capsuleButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button {
            #if os(iOS)
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            #endif
            action()
        } label: {
            Image(systemName: symbol)
                .font(.system(size: controlSize, weight: .semibold))
                .foregroundStyle(theme.frost)
                .frame(width: 44, height: 44)
                .background(theme.deep, in: Circle())
                .overlay { Circle().strokeBorder(theme.rim, lineWidth: 1) }
        }
        .buttonStyle(.plain)
        .offset(y: Self.capsuleRise + 12)
        .transition(.scale.combined(with: .opacity))
        .accessibilityLabel(label)
    }
}
