import JarvisClient
import SwiftUI

struct SettingsView: View {
    @Bindable var session: AppSession
    @State private var confirmingUnpair = false
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
}
