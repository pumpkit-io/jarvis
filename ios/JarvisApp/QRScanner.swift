#if os(iOS)
import AVFoundation
import SwiftUI
import UIKit

struct QRScanner: UIViewControllerRepresentable {
    let accept: (String) -> Void
    let unavailable: () -> Void
    func makeUIViewController(context: Context) -> ScannerController { ScannerController(accept: accept, unavailable: unavailable) }
    func updateUIViewController(_ controller: ScannerController, context: Context) {}
    static func dismantleUIViewController(_ controller: ScannerController, coordinator: ()) { controller.stop() }
}

private final class CameraCapture: @unchecked Sendable {
    let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "jarvis.pairing-camera")
    func start(delegate: ScannerController) {
        queue.async { [self] in
            guard let device = AVCaptureDevice.default(for: .video), let input = try? AVCaptureDeviceInput(device: device),
                  session.canAddInput(input) else { Task { @MainActor in delegate.unavailable() }; return }
            session.addInput(input)
            let output = AVCaptureMetadataOutput()
            guard session.canAddOutput(output) else { Task { @MainActor in delegate.unavailable() }; return }
            session.addOutput(output)
            output.setMetadataObjectsDelegate(delegate, queue: .main)
            output.metadataObjectTypes = [.qr]
            session.startRunning()
        }
    }
    func stop() { queue.async { [self] in if session.isRunning { session.stopRunning() } } }
}

final class ScannerController: UIViewController, @preconcurrency AVCaptureMetadataOutputObjectsDelegate {
    private let capture = CameraCapture()
    private let accept: (String) -> Void
    let unavailable: () -> Void
    private var finished = false
    private var preview: AVCaptureVideoPreviewLayer?
    init(accept: @escaping (String) -> Void, unavailable: @escaping () -> Void) { self.accept = accept; self.unavailable = unavailable; super.init(nibName: nil, bundle: nil) }
    required init?(coder: NSCoder) { fatalError("Use init(accept:)") }
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        let layer = AVCaptureVideoPreviewLayer(session: capture.session)
        layer.videoGravity = .resizeAspectFill
        view.layer.addSublayer(layer); preview = layer
        capture.start(delegate: self)
    }
    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); preview?.frame = view.bounds }
    override func viewWillDisappear(_ animated: Bool) { super.viewWillDisappear(animated); stop() }
    func stop() { finished = true; capture.stop() }
    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput metadataObjects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard !finished, let code = metadataObjects.compactMap({ ($0 as? AVMetadataMachineReadableCodeObject)?.stringValue }).first else { return }
        stop(); accept(code)
    }
}
#endif
