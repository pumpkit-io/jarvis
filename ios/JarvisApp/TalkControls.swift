import JarvisClient
import SwiftUI

struct TalkControls: View {
    @Bindable var session: AppSession
    var controlSize: CGFloat
    var showsKeys = true
    private var theme: JarvisTheme { JarvisTheme() }

    var body: some View {
        VStack(spacing: 12) {
            if let message = session.voiceMessage { Text(message) }
            if let message = session.talk.message {
                Text(message)
                if session.talk.microphoneRefused {
                    Link("Open Settings", destination: microphoneSettingsURL).underline().frame(minHeight: 44)
                }
            }
            if showsKeys {
                if controlSize > 20 {
                    VStack(spacing: 12) {
                        TalkKey(talk: session.talk, controlSize: controlSize)
                        VoiceControl(session: session, controlSize: controlSize)
                    }
                } else {
                    HStack(alignment: .top, spacing: 12) {
                        TalkKey(talk: session.talk, controlSize: controlSize)
                        VoiceControl(session: session, controlSize: controlSize)
                    }
                }
            }
        }
        .font(.system(size: controlSize))
        .foregroundStyle(theme.haze)
    }

    private var microphoneSettingsURL: URL {
        #if os(iOS)
        URL(string: UIApplication.openSettingsURLString)!
        #else
        URL(string: "app-settings:")!
        #endif
    }
}
