import JarvisClient
import SwiftUI

struct ConversationView: View {
    let connection: HubConnectionState = .unpaired
    @Environment(\.colorScheme) private var scheme
    @ScaledMetric(relativeTo: .body) private var scriptSize = 19
    @ScaledMetric(relativeTo: .subheadline) private var controlSize = 15

    private var theme: JarvisTheme { JarvisTheme(scheme: scheme) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 32) {
                Text(connection.message)
                    .font(.system(size: controlSize))
                    .foregroundStyle(theme.secondary)
                    .accessibilityAddTraits(.isHeader)

                Text("Nothing said yet. Hold the key and ask Jarvis something.")
                    .font(.system(size: scriptSize, design: .serif))
                    .foregroundStyle(theme.text)
                    .lineSpacing(5)
                    .padding(.top, 24)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
        }
        .background(theme.background)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 12) {
                HStack {
                    Text("Type instead")
                    Spacer()
                    Text("Coming soon")
                }
                .foregroundStyle(theme.secondary)
                .padding(16)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(theme.secondary, lineWidth: 1))
                .accessibilityElement(children: .combine)

                HStack(alignment: .top, spacing: 12) {
                    VStack(spacing: 6) {
                        Text("Hold to talk")
                            .fontWeight(.semibold)
                        Text("Coming soon")
                    }
                    .frame(maxWidth: .infinity, minHeight: 88)
                    .padding(.vertical, 12)
                    .foregroundStyle(theme.keyText)
                    .background(theme.brass, in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityElement(children: .combine)
                    .accessibilityHint("Talk is not available in this skeleton.")

                    VStack(spacing: 6) {
                        Text("Voice")
                        Text("Coming\nsoon")
                            .multilineTextAlignment(.center)
                    }
                    .padding(12)
                    .frame(minWidth: 60, minHeight: 88)
                    .foregroundStyle(theme.secondary)
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(theme.secondary, lineWidth: 1))
                    .accessibilityElement(children: .combine)
                }
            }
            .font(.system(size: controlSize))
            .padding(20)
            .background(theme.background)
        }
    }
}

#Preview("Dark") {
    ConversationView().preferredColorScheme(.dark)
}

#Preview("Light") {
    ConversationView().preferredColorScheme(.light)
}
