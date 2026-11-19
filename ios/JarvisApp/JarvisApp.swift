import SwiftUI
import JarvisClient

@main
struct JarvisApp: App {
    var body: some Scene {
        WindowGroup {
            JarvisRootView(session: AppSession(store: KeychainPairingStore()))
        }
    }
}
