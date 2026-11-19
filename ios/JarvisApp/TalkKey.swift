import JarvisClient
import SwiftUI

struct TalkKey: View {
    @Bindable var talk: TalkController
    var controlSize: CGFloat
    @Environment(\.colorScheme) private var scheme
    @State private var interaction: UUID?
    @State private var pressTask: Task<Void, Never>?
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
                    if talk.gesture == .hold, talk.canPress {
                        held = true
                        start()
                    }
                }
                guard !gestureCancelled else { return }
                if (held || talk.gesture == .tap && recording), value.translation.height < -60 {
                    held = false; gestureCancelled = true
                    cancel()
                }
            }
            .onEnded { _ in
                gestureStarted = false
                let cancelled = gestureCancelled; gestureCancelled = false
                guard !cancelled else { return }
                if talk.gesture == .tap {
                    if recording || talk.state == .authorizing || pressTask != nil { send() }
                    else if talk.canPress { start() }
                } else if held {
                    held = false
                    send()
                }
            })
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction {
            if recording || talk.state == .authorizing || pressTask != nil { send() }
            else if talk.canPress { start() }
        }
        .accessibilityAction(named: "Cancel Talk") { cancel() }
        .accessibilityHint(talk.gesture == .hold ? "Hold while speaking. Release to send. Slide up to cancel." : "Tap to start. Tap again to send. Slide up to cancel.")
        .accessibilityValue(recording ? "Recording, \(talk.elapsed) seconds" : talk.ready ? talk.label : "Unavailable")
        .onDisappear { held = false; gestureStarted = false; gestureCancelled = false; cancel() }
    }

    private func start() {
        pressTask?.cancel()
        let id = UUID(); interaction = id
        pressTask = Task {
            await talk.press(interaction: id)
            if interaction == id { pressTask = nil }
        }
    }

    private func send() {
        pressTask?.cancel(); pressTask = nil
        guard let id = interaction else { return }
        Task { await talk.releaseInteraction(id) }
    }

    private func cancel() {
        pressTask?.cancel(); pressTask = nil
        guard let id = interaction else { return }
        interaction = nil
        Task { await talk.cancelInteraction(id) }
    }
}
