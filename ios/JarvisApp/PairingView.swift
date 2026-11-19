import JarvisClient
import SwiftUI
#if os(iOS)
import AVFoundation
import UIKit
#endif

struct PairingView: View {
    let pairing: Bool
    let error: PairingError?
    let cameraRefused: Bool
    let scan: () -> Void
    let submit: (String, String) -> Void
    let settings: () -> Void
    @State var manual = false
    @State private var origin = ""
    @State private var code = ""
    @Environment(\.colorScheme) private var scheme
    @ScaledMetric(relativeTo: .body) private var scriptSize = 19
    @ScaledMetric(relativeTo: .subheadline) private var controlSize = 15
    private var theme: JarvisTheme { JarvisTheme(scheme: scheme) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 32) {
                Text("Pair with your Mac")
                    .font(.system(size: controlSize))
                    .accessibilityAddTraits(.isHeader)
                Text("Run ./jarvis pair on the Mac, then scan the code or type it here.")
                    .font(.system(size: scriptSize, design: .serif))
                    .lineSpacing(5)
                    .padding(.top, 24)
                VStack(alignment: .leading, spacing: 16) {
                    if cameraRefused {
                        Text("Allow the camera in Settings to scan, or type the code.")
                            .foregroundStyle(theme.secondary)
                        Button("Settings", action: settings).buttonStyle(.plain).frame(minHeight: 44)
                    } else {
                        outlinedButton(pairing ? "Pairing…" : "Scan code", action: scan)
                    }
                    outlinedButton("Type the code") { manual = true }
                    if manual {
                        VStack(alignment: .leading, spacing: 12) {
                            TextField("HTTPS address from the Mac", text: $origin)
                                .textContentType(.URL)
                                .frame(minHeight: 44)
                                #if os(iOS)
                                .keyboardType(.URL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                #endif
                            TextField("Six-digit code", text: $code)
                                .textContentType(.oneTimeCode)
                                .frame(minHeight: 44)
                                #if os(iOS)
                                .keyboardType(.numberPad)
                                #endif
                        }
                        .textFieldStyle(.plain)
                        .padding(16)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(theme.secondary))
                        outlinedButton(pairing ? "Pairing…" : "Pair") { submit(origin, code) }
                    }
                    if let error {
                        Text(error.message)
                            .foregroundStyle(theme.secondary)
                            .accessibilityLabel(error.message.replacingOccurrences(of: "`", with: ""))
                    }
                }
                .font(.system(size: controlSize))
                .disabled(pairing)
            }
            .foregroundStyle(theme.text)
            .frame(maxWidth: 520, alignment: .leading)
            .padding(20)
            .frame(maxWidth: .infinity)
        }
        .background(theme.background)
        .tint(theme.text)
    }

    private func outlinedButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).frame(maxWidth: .infinity, minHeight: 44).padding(8)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(theme.secondary))
    }
}

struct JarvisRootView: View {
    @State private var session: AppSession
    @Environment(\.scenePhase) private var phase
    @Environment(\.colorScheme) private var scheme
    #if os(iOS)
    @State private var camera = PairingCamera(request: { await AVCaptureDevice.requestAccess(for: .video) })
    @State private var scanning = false
    #endif

    init(session: AppSession) { _session = State(initialValue: session) }

    var body: some View {
        Group {
            if session.loading {
                ProgressView("Connecting…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if session.credentials != nil {
                NavigationStack { ConversationView(session: session) }.tint(JarvisTheme(scheme: scheme).text)
            } else {
                PairingView(pairing: session.pairing, error: session.error, cameraRefused: cameraRefused,
                            scan: scan, submit: submit, settings: openSettings)
            }
        }
        .task { await session.launch() }
        .onChange(of: phase) { _, value in
            Task { await session.setForeground(value == .active) }
            #if os(iOS)
            if value == .active { refreshCamera() }
            if value != .active { scanning = false }
            #endif
        }
        #if os(iOS)
        .onAppear { refreshCamera() }
        .sheet(isPresented: $scanning) {
            NavigationStack {
                QRScanner(accept: { text in
                    scanning = false
                    do {
                        let input = try PairingInput(qr: text)
                        Task { await session.pair(input) }
                    } catch { session.showError(error as? PairingError ?? .invalidQR) }
                }, unavailable: {
                    scanning = false
                    session.showError(.cameraUnavailable)
                })
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle("Scan code")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { scanning = false } } }
            }
        }
        #endif
    }

    private var cameraRefused: Bool {
        #if os(iOS)
        camera.permission == .refused
        #else
        true
        #endif
    }

    private func submit(_ origin: String, _ code: String) {
        do {
            let input = try PairingInput(origin: origin, code: code)
            Task { await session.pair(input) }
        } catch { session.showError(error as? PairingError ?? .invalidCode) }
    }
    private func scan() {
        #if os(iOS)
        Task { if await camera.authorize(), phase == .active { scanning = true } }
        #endif
    }
    private func openSettings() {
        #if os(iOS)
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
        #endif
    }
    #if os(iOS)
    private func refreshCamera() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: camera.refresh(.allowed)
        case .notDetermined: camera.refresh(.unknown)
        default: camera.refresh(.refused)
        }
    }
    #endif
}
