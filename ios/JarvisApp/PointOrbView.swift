import SwiftUI
import JarvisClient

struct PointOrbView: View {
    var level: CGFloat
    var speaker: OrbModel.Speaker = .idle
    private let pointCount = 2500
    var isVisible = true
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let theme = JarvisTheme()

    @State private var points: [SIMD3<Float>] = []
    @State private var start = Date()

    var body: some View {
        TimelineView(.animation(minimumInterval: reduceMotion ? 1.0 / 30 : 1.0 / 60,
                                paused: !isVisible || scenePhase != .active)) { timeline in
            let t = Float(timeline.date.timeIntervalSince(start)) * (reduceMotion ? 0.35 : 1)
            Canvas { ctx, size in
                draw(in: &ctx, size: size, t: t)
            }
        }
        .onAppear {
            if points.count != pointCount { points = Self.fibonacciSphere(pointCount) }
        }
    }

    private func draw(in ctx: inout GraphicsContext, size: CGSize, t: Float) {
        guard !points.isEmpty else { return }

        let lvl = Float(min(max(level, 0), 1))
        let radius = Float(min(size.width, size.height)) * 0.34
        let cx = Float(size.width) / 2
        let cy = Float(size.height) / 2

        var (amp, freq, speed, spikes): (Float, Float, Float, Float) = switch speaker {
        case .idle:   (0.035,              1.8, 0.35, 0.0)
        case .user:   (0.04 + 0.22 * lvl,  2.6, 1.0,  0.25 * lvl)
        case .jarvis: (0.05 + 0.40 * lvl,  3.2, 1.8,  0.6 * lvl)
        }

        if reduceMotion { amp *= 0.45; spikes *= 0.4 }

        let color: Color = switch speaker {
        case .idle:   theme.idleBlue
        case .user:   theme.ice
        case .jarvis: theme.cyan
        }

        let ay = t * 0.25
        let ax = 0.35 * sin(t * 0.2)
        let cosY = cos(ay), sinY = sin(ay)
        let cosX = cos(ax), sinX = sin(ax)

        let breathe: Float = 1 + 0.02 * sin(t * 1.4)
        let camera: Float = 3.0

        let buckets = 6
        var paths = Array(repeating: Path(), count: buckets)

        for p in points {
            var n = noise(p * freq, t * speed)
            if spikes > 0 { n += spikes * noise(p * freq * 2.3 + 7.1, t * speed * 1.7) }
            let r = breathe * (1 + amp * n)
            var x = p.x * r, y = p.y * r, z = p.z * r

            let x1 = x * cosY + z * sinY
            let z1 = -x * sinY + z * cosY
            let y1 = y * cosX - z1 * sinX
            let z2 = y * sinX + z1 * cosX
            x = x1; y = y1; z = z2

            let s = camera / (camera - z)
            let sx = cx + x * radius * s
            let sy = cy - y * radius * s

            let depth = min(max((z + 1.4) / 2.8, 0), 1)
            let dot = radius * 0.011 * (0.55 + depth) * (1 + 0.5 * lvl) * s
            let b = min(Int(depth * Float(buckets)), buckets - 1)
            paths[b].addEllipse(in: CGRect(x: CGFloat(sx - dot / 2), y: CGFloat(sy - dot / 2),
                                           width: CGFloat(dot), height: CGFloat(dot)))
        }

        ctx.blendMode = .plusLighter

        var glow = ctx
        glow.addFilter(.blur(radius: CGFloat(radius) * 0.035))
        for b in 0..<buckets {
            let o = 0.25 + 0.75 * (Double(b) + 0.5) / Double(buckets)
            glow.fill(paths[b], with: .color(color.opacity(o * (0.35 + 0.5 * Double(lvl)))))
        }

        for b in 0..<buckets {
            let o = 0.15 + 0.85 * (Double(b) + 0.5) / Double(buckets)
            ctx.fill(paths[b], with: .color(color.opacity(o)))
        }
    }

    @inline(__always)
    private func noise(_ p: SIMD3<Float>, _ t: Float) -> Float {
        let a = sin(p.x + t)              * cos(p.y * 1.3 - t * 0.7)
        let b = sin(p.y * 1.7 + t * 1.3)  * cos(p.z * 1.1 + t * 0.5)
        let c = sin(p.z * 2.3 - t * 0.9)  * cos(p.x * 1.9 + t * 1.1)
        return (a + b + c) / 3 * 1.6
    }

    static func fibonacciSphere(_ n: Int) -> [SIMD3<Float>] {
        guard n > 1 else { return [SIMD3(0, 1, 0)] }
        let golden = Float.pi * (3 - sqrt(5))
        return (0..<n).map { i in
            let y = 1 - (Float(i) / Float(n - 1)) * 2
            let r = sqrt(max(0, 1 - y * y))
            let theta = golden * Float(i)
            return SIMD3(cos(theta) * r, y, sin(theta) * r)
        }
    }
}

