import JarvisClient
import SwiftUI

struct SettingsView: View {
    @Bindable var session: AppSession
    @State private var confirmingUnpair = false
    @Environment(ScriptStyleSetting.self) private var scriptStyle
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    private var theme: JarvisTheme { JarvisTheme() }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 32) {
                Text("Settings").accessibilityAddTraits(.isHeader)
                VStack(alignment: .leading, spacing: 12) {
                    Button { Task { await session.takeFocus() } } label: {
                        Text("Send updates here").frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                            .contentShape(Rectangle())
                    }.buttonStyle(.plain).disabled(session.focusPending)
                    if session.focusHere { Text("Updates come to this iPhone") }
                    if let message = session.focusMessage { Text(message).foregroundStyle(theme.haze) }
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text("Paired Mac").foregroundStyle(theme.haze)
                    Text(session.credentials?.macName ?? "Unpaired")
                }
                Button { confirmingUnpair = true } label: {
                    Text("Unpair this iPhone").frame(minWidth: 44, minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                }.buttonStyle(.plain)
                scriptStyleChoices
            }
            .font(.system(size: controlSize))
            .foregroundStyle(theme.frost)
            .frame(maxWidth: 520, alignment: .leading).padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(theme.void).tint(theme.frost)
        .confirmationDialog("Unpair this iPhone?", isPresented: $confirmingUnpair, titleVisibility: .visible) {
            Button("Unpair this iPhone") { Task { await session.unpair() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes the saved pairing and conversation from this iPhone. Pair again to reconnect.")
        }
    }

    // Temporary: ios/README.md lists what to remove once the User has chosen.
    @ViewBuilder private var scriptStyleChoices: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Conversation look (temporary)").foregroundStyle(theme.haze).accessibilityAddTraits(.isHeader)
            ForEach(ScriptStyle.Look.allCases, id: \.self) { look in
                choice(selected: scriptStyle.style.look == look, select: { scriptStyle.style.look = look }) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(look.name)
                        Text(look.summary).foregroundStyle(theme.haze)
                    }
                    .padding(.vertical, 6)
                }
            }
        }
        VStack(alignment: .leading, spacing: 4) {
            Text("Conversation font (temporary)").foregroundStyle(theme.haze).accessibilityAddTraits(.isHeader)
            ForEach(ScriptFont.all) { font in
                choice(selected: scriptStyle.style.font == font.id, select: { scriptStyle.style.font = font.id }) {
                    Text(font.name).font(font.font(size: controlSize + 2))
                }
            }
        }
    }

    private func choice<Label: View>(selected: Bool, select: @escaping () -> Void,
                                     @ViewBuilder label: () -> Label) -> some View {
        Button(action: select) {
            HStack(spacing: 12) {
                label()
                Spacer(minLength: 0)
                Image(systemName: "checkmark").opacity(selected ? 1 : 0).accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
