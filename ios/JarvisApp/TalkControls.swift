import JarvisClient
import SwiftUI

struct MessageInputBar: View {
    @Bindable var session: AppSession
    var typingChanged: (Bool) -> Void = { _ in }
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    @FocusState private var typing: Bool
    @State private var discarded = false
    @State private var visible = false
    @State private var pillHeight: CGFloat = 56
    @State private var preview = TalkPreview()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let barBottomPadding: CGFloat = 8
    private let theme = JarvisTheme()
    private var recording: Bool { session.talk.state == .recording }
    private var paused: Bool { session.talk.state == .paused }
    private var hasDraft: Bool { !session.draft.isEmpty }

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if paused {
                Button { preview.stop(); Task { await session.talk.discard() } } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 20, weight: .medium))
                        .foregroundStyle(theme.signal)
                        .frame(width: 56, height: 56)
                        .background(theme.deep, in: Circle())
                        .overlay { Circle().strokeBorder(theme.rim, lineWidth: 1) }
                }
                .buttonStyle(.plain)
                .transition(.scale.combined(with: .opacity))
                .accessibilityLabel("Discard Talk")
            } else if discarded {
                DiscardBin().transition(.scale.combined(with: .opacity))
            }
            pill
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { pillHeight = $0 }
                .background { keyboardDismissPadding }
        }
        .animation(.spring(response: 0.3, dampingFraction: 0.85), value: paused)
        .overlay(alignment: .topLeading) {
            messages
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 8)
                .alignmentGuide(.top) { $0[.bottom] }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.3), value: session.liveMode.message)
        }
        .font(.system(size: controlSize))
        .foregroundStyle(theme.haze)
        .padding(.horizontal, 20)
        .padding(.top, 8)
        .padding(.bottom, barBottomPadding)
        .task(id: TalkFeedbackKey(discards: session.talk.discards, visible: visible)) {
            // Settings and Tasks keep the main screen's bar alive, so only the bar on screen claims a discard.
            guard visible, session.talk.takeDiscardConfirmation() else { return }
            #if os(iOS)
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
            #endif
            AccessibilityNotification.Announcement("Talk discarded").post()
            withAnimation(.spring(response: 0.3, dampingFraction: 0.85)) { discarded = true }
            do { try await Task.sleep(for: .milliseconds(1_400)) } catch { discarded = false; return }
            withAnimation(.easeOut(duration: 0.25)) { discarded = false }
        }
        .onChange(of: session.talk.outcome) { _, outcome in
            guard visible, outcome == .tooShort else { return }
            #if os(iOS)
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
            #endif
            AccessibilityNotification.Announcement("Too short to send").post()
        }
        .onChange(of: session.liveMode.message) { _, message in
            guard visible, let message else { return }
            AccessibilityNotification.Announcement(message).post()
        }
        .onChange(of: session.talk.state) { _, state in
            if state != .paused { preview.stop() }
        }
        .onChange(of: typing) { typingChanged(typing) }
        .onAppear { visible = true }
        .onDisappear { visible = false; typingChanged(false) }
    }

    private var messages: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let message = session.draftValidationMessage ?? session.inputMessage { status(Text(message)) }
            if let message = session.voiceMessage { status(Text(message)) }
            if let message = session.talk.message {
                VStack(alignment: .leading, spacing: 4) {
                    Text(message).fixedSize(horizontal: false, vertical: true)
                    if session.talk.microphoneRefused {
                        Link("Open Settings", destination: microphoneSettingsURL)
                            .underline().frame(minHeight: 44)
                    }
                }
                .modifier(StatusBackground())
            }
            if let message = session.liveMode.message { status(Text(message)).transition(.opacity) }
        }
    }

    @ViewBuilder private var keyboardDismissPadding: some View {
        #if os(iOS)
        KeyboardDismissPadding(padding: pillHeight + barBottomPadding)
        #endif
    }

    private func status(_ text: Text) -> some View {
        text.fixedSize(horizontal: false, vertical: true).modifier(StatusBackground())
    }

    private var pill: some View {
        HStack(alignment: .bottom, spacing: 8) {
            field
                .foregroundStyle(theme.frost)
                .padding(.leading, 16)
                .padding(.vertical, 12)
                .frame(minHeight: 56)
                .frame(maxWidth: .infinity, alignment: .leading)
            Group {
                if hasDraft && !recording && !paused {
                    Button { Task { await session.sendTypedLine() } } label: {
                        Image(systemName: "arrow.up").font(.system(size: max(20, controlSize), weight: .semibold))
                            .frame(width: TalkKey.diameter, height: TalkKey.diameter)
                            .foregroundStyle(theme.void).background(theme.cyan, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!session.canSubmit)
                    .accessibilityLabel("Send line")
                } else {
                    TalkKey(talk: session.talk, controlSize: controlSize, level: session.orb.level, beforeResume: preview.stop)
                }
            }
            .padding([.trailing, .bottom], 6)
        }
        // The rim sits behind the contents, so the held mic covers the pill instead of being crossed by it.
        .background {
            RoundedRectangle(cornerRadius: 28).fill(theme.deep)
                .overlay { RoundedRectangle(cornerRadius: 28).strokeBorder(theme.rim, lineWidth: 1) }
        }
    }

    @ViewBuilder private var field: some View {
        if paused {
            PausedRow(talk: session.talk, preview: preview, controlSize: controlSize)
        } else if recording {
            RecordingRow(talk: session.talk, controlSize: controlSize)
        } else {
            TextField("Message Jarvis", text: $session.draft, prompt: Text("Message Jarvis").foregroundStyle(theme.haze), axis: .vertical)
                .lineLimit(1...5)
                .textFieldStyle(.plain)
                .tint(theme.cyan)
                .focused($typing)
                .onSubmit { Task { await session.sendTypedLine() } }
        }
    }

    private var microphoneSettingsURL: URL {
        #if os(iOS)
        URL(string: UIApplication.openSettingsURLString)!
        #else
        URL(string: "app-settings:")!
        #endif
    }
}

private struct TalkFeedbackKey: Equatable {
    let discards: Int
    let visible: Bool
}

/// Keeps a status line readable while the conversation scrolls behind the transparent bar.
private struct StatusBackground: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(JarvisTheme().deep, in: RoundedRectangle(cornerRadius: 14))
    }
}

#if os(iOS)
/// Starts the interactive keyboard dismissal at the top of the composer instead of the keyboard's edge.
private struct KeyboardDismissPadding: UIViewRepresentable {
    var padding: CGFloat

    func makeUIView(context: Context) -> Probe { Probe() }
    func updateUIView(_ view: Probe, context: Context) { view.padding = padding }

    final class Probe: UIView {
        var padding: CGFloat = 0 { didSet { if padding != oldValue { apply() } } }
        override func didMoveToWindow() {
            super.didMoveToWindow()
            apply()
            // The conversation's scroll view can join the window in the same pass, after this probe.
            DispatchQueue.main.async { [weak self] in self?.apply() }
        }
        // UIKit doesn't say whose guide the dismiss gesture reads, so every candidate gets the padding.
        private func apply() {
            guard let window else { return }
            var views: [UIView] = [window]
            while let view = views.popLast() {
                if view === window || view is UIScrollView || view === window.rootViewController?.view {
                    view.keyboardLayoutGuide.keyboardDismissPadding = padding
                }
                views.append(contentsOf: view.subviews)
            }
        }
    }
}
#endif

struct VoiceControl: View {
    @Bindable var session: AppSession
    var controlSize: CGFloat
    private let theme = JarvisTheme()

    var body: some View {
        Button { Task { await session.setVoiceOn(!session.voiceOn) } } label: {
            Image(systemName: session.voiceOn ? "speaker.wave.2" : "speaker.slash")
                .font(.system(size: controlSize))
                .frame(width: 44, height: 44)
                .contentShape(Circle())
                .background(theme.deep, in: Circle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(theme.haze)
        .accessibilityLabel("Jarvis voice")
        .accessibilityHint(session.voiceOn ? "Turn off speech on this phone" : "Turn on speech on this phone")
        .accessibilityValue(session.voicePending ? "Waiting for the Mac" : session.voiceOn ? "On" : "Off")
    }
}
