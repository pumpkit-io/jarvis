import Foundation
import Observation

@MainActor @Observable public final class OrbModel {
    public enum Speaker: Sendable { case idle, user, jarvis }
    public private(set) var speaker: Speaker = .idle
    public private(set) var level: Double = 0

    /// Meters report linear RMS, where ordinary speech sits near 0.03 to 0.2; these decibel bounds map it to 0...1.
    nonisolated static let quietestDecibels = -55.0
    nonisolated static let loudestDecibels = -15.0

    nonisolated static func loudness(rms: Double) -> Double {
        guard rms > 0 else { return 0 }
        let decibels = 20 * log10(rms)
        return min(1, max(0, (decibels - quietestDecibels) / (loudestDecibels - quietestDecibels)))
    }

    func update(speaker: Speaker, target: Double) {
        self.speaker = speaker
        level = level * 0.5 + min(1, max(0, target)) * 0.5
    }

    func reset() { speaker = .idle; level = 0 }
}

/// How the orb's surface moves: wavy outline, tangential flow that opens tears, and loose points drifting off as wisps.
public struct OrbSurface: Sendable, Equatable {
    public var ripple: Float
    public var flow: Float
    /// Share of points that detach into wisps.
    public var looseShare: Float
    /// How far detached points drift out, in sphere radii.
    public var looseDrift: Float
    public var speed: Float

    public static func target(for speaker: OrbModel.Speaker, level: Float) -> OrbSurface {
        switch speaker {
        case .idle:
            OrbSurface(ripple: 0.02, flow: 0.02, looseShare: 0.05, looseDrift: 0.03, speed: 0.4)
        case .user:
            OrbSurface(ripple: 0.02 + 0.11 * level, flow: 0.02 + 0.11 * level,
                       looseShare: 0.05 + 0.13 * level, looseDrift: 0.03 + 0.17 * level, speed: 1.4)
        case .jarvis:
            OrbSurface(ripple: 0.02 + 0.15 * level, flow: 0.02 + 0.15 * level,
                       looseShare: 0.05 + 0.18 * level, looseDrift: 0.03 + 0.25 * level, speed: 2.2)
        }
    }

    func moved(toward other: OrbSurface, by k: Float) -> OrbSurface {
        func mix(_ a: Float, _ b: Float) -> Float { a + (b - a) * k }
        return OrbSurface(ripple: mix(ripple, other.ripple), flow: mix(flow, other.flow),
                          looseShare: mix(looseShare, other.looseShare), looseDrift: mix(looseDrift, other.looseDrift),
                          speed: mix(speed, other.speed))
    }
}

/// One orb for every speaker: level and surface ease toward the current speaker instead of switching,
/// so a change of speaker never jumps the points.
public struct OrbMotion: Sendable {
    /// A frame gap longer than this, such as the orb returning from hidden, resumes instead of jumping.
    static let longestStep = 1.0 / 20
    static let reducedPace = 0.35
    static let reducedDeformation: Float = 0.45
    /// Per-frame easing tuned at 60 fps; other frame rates convert it so the orb settles in the same time.
    static let levelEasing: Float = 0.25
    static let surfaceEasing: Float = 0.08
    static let largestPulse: Float = 0.09
    /// Surface noise rates are multiples of 0.01, so the phase wraps here without a visible seam.
    public static let phasePeriod = 200 * Double.pi
    /// Rotation, wobble, twinkle and wisp rates are multiples of 0.05, so time wraps here without a seam.
    public static let timePeriod = 40 * Double.pi

    public private(set) var level: Float = 0
    public private(set) var surface = OrbSurface.target(for: .idle, level: 0)
    /// Time of the surface noise, advancing at the surface's speed.
    public private(set) var phase: Double = 0
    /// Time of the rotation, twinkle and wisp drift, which run at one rate in every state.
    public private(set) var time: Double = 0

    public init() {}

    /// How much the whole orb swells with the voice.
    public var pulse: Float { Self.largestPulse * level }

    /// Reduce Motion slows the surface and rotation and softens speech deformation.
    public mutating func advance(by seconds: Double, toward speaker: OrbModel.Speaker, level target: Double,
                                 reducedMotion: Bool = false) {
        let dt = min(max(seconds, 0), Self.longestStep)
        let frames = Float(dt * 60)
        let spoken = Float(min(max(target, 0), 1)) * (reducedMotion ? Self.reducedDeformation : 1)
        level += ((speaker == .idle ? 0 : spoken) - level) * Self.ease(Self.levelEasing, frames)
        surface = surface.moved(toward: .target(for: speaker, level: level), by: Self.ease(Self.surfaceEasing, frames))
        let pace = reducedMotion ? Self.reducedPace : 1
        phase = (phase + dt * pace * Double(surface.speed)).truncatingRemainder(dividingBy: Self.phasePeriod)
        time = (time + dt * pace).truncatingRemainder(dividingBy: Self.timePeriod)
    }

    static func ease(_ perFrame: Float, _ frames: Float) -> Float { 1 - pow(1 - perFrame, frames) }
}
