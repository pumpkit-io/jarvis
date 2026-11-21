import SwiftUI
import MetalKit
import JarvisClient

struct PointOrbView: UIViewRepresentable {
    var level: CGFloat
    var speaker: OrbModel.Speaker = .idle
    var isVisible = true
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeCoordinator() -> OrbRenderer { OrbRenderer() }

    func makeUIView(context: Context) -> MTKView {
        let renderer = context.coordinator
        let view = MTKView(frame: .zero, device: renderer.device)
        view.colorPixelFormat = .bgra8Unorm
        view.clearColor = OrbRenderer.background
        view.isOpaque = true
        view.enableSetNeedsDisplay = false
        renderer.configure(view)
        view.delegate = renderer
        return view
    }

    func updateUIView(_ view: MTKView, context: Context) {
        let renderer = context.coordinator
        renderer.level = Double(level)
        renderer.speaker = speaker
        renderer.reducedMotion = reduceMotion
        view.preferredFramesPerSecond = reduceMotion ? 30 : 60
        view.isPaused = !isVisible || scenePhase != .active
    }
}

/// Must match `Uniforms` in ParticleOrb.metal field for field.
private struct OrbUniforms {
    var tint: SIMD4<Float>
    var time: Float
    var phase: Float
    var level: Float
    var ripple: Float
    var flow: Float
    var looseShare: Float
    var looseDrift: Float
    var radiusPx: Float
    var viewW: Float
    var viewH: Float
    var dotPx: Float
    var intensity: Float
}

@MainActor final class OrbRenderer: NSObject, MTKViewDelegate {
    /// The theme's void, written raw because the drawable is not sRGB-converting.
    static let background = MTLClearColor(red: 5 / 255.0, green: 7 / 255.0, blue: 12 / 255.0, alpha: 1)
    private static let pointCount = 14_000

    let device: MTLDevice?
    private let queue: MTLCommandQueue?
    private var pipeline: MTLRenderPipelineState?
    private var points: MTLBuffer?

    var level: Double = 0
    var speaker: OrbModel.Speaker = .idle
    var reducedMotion = false

    private var motion = OrbMotion()
    private var lastFrame: CFTimeInterval?

    override init() {
        device = MTLCreateSystemDefaultDevice()
        queue = device?.makeCommandQueue()
        super.init()
    }

    func configure(_ view: MTKView) {
        guard let device, let library = device.makeDefaultLibrary() else { return }
        let descriptor = MTLRenderPipelineDescriptor()
        descriptor.vertexFunction = library.makeFunction(name: "orbVertex")
        descriptor.fragmentFunction = library.makeFunction(name: "orbFragment")
        let color = descriptor.colorAttachments[0]!
        color.pixelFormat = view.colorPixelFormat
        color.isBlendingEnabled = true
        color.rgbBlendOperation = .add
        color.alphaBlendOperation = .add
        color.sourceRGBBlendFactor = .one
        color.destinationRGBBlendFactor = .one
        color.sourceAlphaBlendFactor = .one
        color.destinationAlphaBlendFactor = .one
        pipeline = try? device.makeRenderPipelineState(descriptor: descriptor)
        points = Self.fibonacciSphere(device: device, count: Self.pointCount)
    }

    /// Evenly spread points on a unit sphere; w carries the point's index for per-point randomness.
    private static func fibonacciSphere(device: MTLDevice, count n: Int) -> MTLBuffer? {
        let golden = Float.pi * (3 - sqrt(5))
        let points = (0..<n).map { i -> SIMD4<Float> in
            let y = 1 - Float(i) / Float(n - 1) * 2
            let r = sqrt(max(0, 1 - y * y))
            let theta = golden * Float(i)
            return SIMD4(cos(theta) * r, y, sin(theta) * r, Float(i))
        }
        return device.makeBuffer(bytes: points, length: MemoryLayout<SIMD4<Float>>.stride * n, options: .storageModeShared)
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        let now = CACurrentMediaTime()
        motion.advance(by: lastFrame.map { now - $0 } ?? 0, toward: speaker, level: level, reducedMotion: reducedMotion)
        lastFrame = now

        guard let pipeline, let points, let queue,
              let pass = view.currentRenderPassDescriptor,
              let drawable = view.currentDrawable,
              let commands = queue.makeCommandBuffer(),
              let encoder = commands.makeRenderCommandEncoder(descriptor: pass) else { return }

        let size = view.drawableSize
        let radius = Float(min(size.width, size.height)) * 0.36 * (1 + motion.pulse)
        // Dot size keeps the preview's spacing at 14k points, measured against the 18k reference density.
        let density = sqrt(18_000 / Float(Self.pointCount))
        let surface = motion.surface
        var uniforms = OrbUniforms(
            tint: SIMD4(1, 1, 1, 1),
            time: Float(motion.time),
            phase: Float(motion.phase),
            level: motion.level,
            ripple: surface.ripple,
            flow: surface.flow,
            looseShare: surface.looseShare,
            looseDrift: surface.looseDrift,
            radiusPx: radius,
            viewW: Float(size.width),
            viewH: Float(size.height),
            dotPx: radius * 0.016 * density,
            intensity: 0.9
        )

        encoder.setRenderPipelineState(pipeline)
        encoder.setVertexBuffer(points, offset: 0, index: 0)
        encoder.setVertexBytes(&uniforms, length: MemoryLayout<OrbUniforms>.stride, index: 1)
        encoder.setFragmentBytes(&uniforms, length: MemoryLayout<OrbUniforms>.stride, index: 0)
        encoder.drawPrimitives(type: .point, vertexStart: 0, vertexCount: Self.pointCount)
        encoder.endEncoding()
        commands.present(drawable)
        commands.commit()
    }
}
