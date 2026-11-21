import JarvisClient
import SwiftUI

struct MessageInputBar: View {
    @Bindable var session: AppSession
    var typingChanged: (Bool) -> Void = { _ in }
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    @FocusState private var typing: Bool
    @State private var discarded = false
    private let theme = JarvisTheme()
    private var recording: Bool { session.talk.state == .recording }
    private var hasDraft: Bool { !session.draft.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let message = session.draftValidationMessage ?? session.inputMessage {
                Text(message).fixedSize(horizontal: false, vertical: true)
            }
            if let message = session.voiceMessage { Text(message).fixedSize(horizontal: false, vertical: true) }
            if let message = session.talk.message {
                Text(message).fixedSize(horizontal: false, vertical: true)
                if session.talk.microphoneRefused {
                    Link("Open Settings", destination: microphoneSettingsURL)
                        .underline().frame(minHeight: 44)
                }
            }
            HStack(alignment: .bottom, spacing: 12) {
                field
                if hasDraft && !recording {
                    Button { Task { await session.sendTypedLine() } } label: {
                        Image(systemName: "arrow.up").font(.system(size: max(22, controlSize), weight: .semibold))
                            .frame(width: 56, height: 56)
                            .foregroundStyle(theme.void).background(theme.cyan, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!session.canSubmit)
                    .accessibilityLabel("Send line")
                } else {
                    TalkKey(talk: session.talk, controlSize: controlSize)
                }
            }
            .padding(.vertical, recording ? 12 : 0)
        }
        .font(.system(size: controlSize))
        .foregroundStyle(theme.haze)
        .padding(.horizontal, 20)
        .padding(.vertical, 8)
        .background(theme.void)
        .overlay(alignment: .bottomTrailing) {
            if discarded {
                Image(systemName: "trash.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(theme.signal)
                    .padding(.trailing, 36)
                    .offset(y: -84)
                    .transition(.scale.combined(with: .opacity))
                    .accessibilityHidden(true)
            }
        }
        .task(id: session.talk.outcome) {
            guard session.talk.outcome == .discarded else { return }
            #if os(iOS)
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
            #endif
            withAnimation(.spring(response: 0.25)) { discarded = true }
            do { try await Task.sleep(for: .milliseconds(650)) } catch { discarded = false; return }
            withAnimation(.easeOut(duration: 0.25)) { discarded = false }
        }
        .onChange(of: typing) { typingChanged(typing) }
        .onDisappear { typingChanged(false) }
    }

    private var field: some View {
        Group {
            if recording {
                HStack(spacing: 8) {
                    if session.talk.isLocked {
                        Button { Task { await session.talk.discard() } } label: {
                            Image(systemName: "trash").frame(minWidth: 44, minHeight: 44)
                        }
                        .buttonStyle(.plain).foregroundStyle(theme.signal)
                        .accessibilityLabel("Discard Talk")
                    }
                    Circle().fill(theme.signal).frame(width: 8, height: 8).accessibilityHidden(true)
                    Text(String(format: "%d:%02d", session.talk.elapsed / 60, session.talk.elapsed % 60))
                        .monospacedDigit().fontWeight(.semibold)
                    if session.talk.isLocked {
                        HStack(alignment: .center, spacing: 2) {
                            ForEach(0..<(controlSize > 20 ? 5 : 9)) { index in
                                Capsule().fill(theme.ice)
                                    .frame(width: 3, height: 3 + 25 * session.orb.level * (0.35 + 0.65 * abs(sin(Double(index + 1)))))
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 30)
                        .accessibilityLabel("Microphone level")
                    } else {
                        Text("‹ Slide to cancel")
                            .font(.system(size: controlSize))
                            .foregroundStyle(theme.haze)
                            .opacity(1 - 0.7 * session.talk.discardProgress)
                            .offset(x: -24 * session.talk.discardProgress)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .accessibilityElement(children: .contain)
            } else {
                TextField("Message Jarvis", text: $session.draft, prompt: Text("Message Jarvis").foregroundStyle(theme.haze), axis: .vertical)
                    .lineLimit(1...5)
                    .textFieldStyle(.plain)
                    .tint(theme.cyan)
                    .focused($typing)
                    .onSubmit { Task { await session.sendTypedLine() } }
            }
        }
        .foregroundStyle(theme.frost)
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .frame(minHeight: 56)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(theme.deep, in: RoundedRectangle(cornerRadius: 28))
    }

    private var microphoneSettingsURL: URL {
        #if os(iOS)
        URL(string: UIApplication.openSettingsURLString)!
        #else
        URL(string: "app-settings:")!
        #endif
    }
}

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
