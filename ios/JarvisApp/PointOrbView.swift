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
}

/// Must match `OrbPoint` in ParticleOrb.metal.
private struct OrbPoint {
    var position: SIMD4<Float>
    var random: SIMD4<Float>
}

@MainActor final class OrbRenderer: NSObject, MTKViewDelegate {
    /// The theme's sRGB components, written raw because the drawable does no colour conversion.
    static let background: MTLClearColor = {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        UIColor(JarvisTheme().void).getRed(&r, green: &g, blue: &b, alpha: &a)
        return MTLClearColor(red: Double(r), green: Double(g), blue: Double(b), alpha: 1)
    }()
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
        guard let device, let library = device.makeDefaultLibrary() else {
            assertionFailure("ParticleOrb.metal is missing from the app target")
            return
        }
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
        do {
            pipeline = try device.makeRenderPipelineState(descriptor: descriptor)
        } catch {
            assertionFailure("Orb pipeline failed: \(error)")
        }
        points = Self.fibonacciSphere(device: device, count: Self.pointCount)
    }

    /// Evenly spread points on a unit sphere, each with its own random values for wisps, shading and twinkle.
    private static func fibonacciSphere(device: MTLDevice, count n: Int) -> MTLBuffer? {
        let golden = Float.pi * (3 - sqrt(5))
        let points = (0..<n).map { i -> OrbPoint in
            let y = 1 - Float(i) / Float(n - 1) * 2
            let r = sqrt(max(0, 1 - y * y))
            let theta = golden * Float(i)
            let random = SIMD4<Float>(hash(i, 1), hash(i, 2), hash(i, 3), 0)
            return OrbPoint(position: SIMD4(cos(theta) * r, y, sin(theta) * r, 1), random: random)
        }
        return device.makeBuffer(bytes: points, length: MemoryLayout<OrbPoint>.stride * n, options: .storageModeShared)
    }

    /// The reference's sine hash, in Double: the GPU's fast sine is inaccurate at these large arguments.
    private static func hash(_ i: Int, _ k: Double) -> Float {
        let v = sin(Double(i) * 12.9898 + k * 78.233) * 43758.5453
        return Float(v - v.rounded(.down))
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
        // Dots grow as points thin out, so coverage matches an 18,000-point sphere.
        let density = sqrt(18_000 / Float(Self.pointCount))
        let surface = motion.surface
        var uniforms = OrbUniforms(
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
            dotPx: radius * 0.016 * density
        )

        encoder.setRenderPipelineState(pipeline)
        encoder.setVertexBuffer(points, offset: 0, index: 0)
        encoder.setVertexBytes(&uniforms, length: MemoryLayout<OrbUniforms>.stride, index: 1)
        encoder.drawPrimitives(type: .point, vertexStart: 0, vertexCount: Self.pointCount)
        encoder.endEncoding()
        commands.present(drawable)
        commands.commit()
    }
}
