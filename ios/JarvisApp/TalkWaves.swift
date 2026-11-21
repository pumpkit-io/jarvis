import SwiftUI

/// Soft rings around the held mic that drift on their own and swell with the User's voice, like the orb.
struct TalkWaves: View {
    /// Eased microphone loudness from 0 to 1, the same signal the orb follows.
    var level: Double
    /// Radius of the enlarged mic the rings surround.
    var radius: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let theme = JarvisTheme()

    var body: some View {
        TimelineView(.animation) { context in
            let time = context.date.timeIntervalSinceReferenceDate * (reduceMotion ? 0.3 : 1)
            Canvas { canvas, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                for ring in Self.rings {
                    canvas.fill(path(around: center, time: time, ring: ring), with: .color(theme.cyan.opacity(ring.opacity)))
                }
            }
        }
        .frame(width: 2 * (radius + 48), height: 2 * (radius + 48))
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private struct Ring {
        let gap: CGFloat
        let spin: Double
        let phase: Double
        let opacity: Double
    }

    private static let rings = [
        Ring(gap: 6, spin: 0.35, phase: 0, opacity: 0.22),
        Ring(gap: 14, spin: -0.22, phase: 2.1, opacity: 0.12),
    ]

    private func path(around center: CGPoint, time: Double, ring: Ring) -> Path {
        let voice = min(1, max(0, level))
        let swell = (reduceMotion ? 0.5 : 1) * (3 + 12 * voice)
        let base = radius + ring.gap + 10 * voice
        let turn = time * ring.spin
        var path = Path()
        let steps = 96
        for step in 0...steps {
            let angle = Double(step) / Double(steps) * 2 * .pi
            // Two and three slow lobes give the blob shape; the finer ripple only shows while the User speaks.
            let wobble = 0.55 * sin(2 * angle + 1.1 * time + ring.phase)
                + 0.3 * sin(3 * angle - 1.7 * time + ring.phase)
                + 0.15 * sin(5 * angle + 3 * time) * voice
            let distance = base + swell * CGFloat(wobble)
            let point = CGPoint(x: center.x + distance * CGFloat(cos(angle + turn)),
                                y: center.y + distance * CGFloat(sin(angle + turn)))
            if step == 0 { path.move(to: point) } else { path.addLine(to: point) }
        }
        path.closeSubpath()
        return path
    }
}
