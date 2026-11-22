import SwiftUI
import JarvisClient

@main
struct JarvisApp: App {
    @UIApplicationDelegateAdaptor(OutboxWake.self) private var wake
    @State private var scriptStyle = ScriptStyleSetting()

    var body: some Scene {
        WindowGroup {
            JarvisRootView(session: AppSession(store: KeychainPairingStore()))
                .preferredColorScheme(.dark)
                .environment(scriptStyle)
        }
    }
}

/// iOS relaunches or wakes the app when queued uploads finish in the background; their results feed the Outbox.
final class OutboxWake: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        BackgroundOutboxTransport.shared.activate()
        return true
    }

    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == BackgroundOutboxTransport.identifier else { completionHandler(); return }
        BackgroundOutboxTransport.shared.finishBackgroundEvents = completionHandler
        BackgroundOutboxTransport.shared.activate()
    }
}
