import Foundation
import Observation

@MainActor @Observable public final class OrbModel {
    public enum Speaker: Sendable { case idle, user, jarvis }
    public private(set) var speaker: Speaker = .idle
    public private(set) var level: Double = 0

    func update(speaker: Speaker, target: Double) {
        self.speaker = speaker
        level = level * 0.8 + min(1, max(0, target)) * 0.2
    }

    func reset() { speaker = .idle; level = 0 }
}

/// One orb for every speaker: colour weights, surface motion and deformation ease between states
/// instead of switching, so a change of speaker never jumps the points.
public struct OrbMotion: Sendable {
    /// Every speaker change settles within about 0.4 s.
    static let blendTime = 0.13
    /// A frame gap longer than this, such as the orb returning from hidden, resumes instead of jumping.
    static let longestStep = 1.0 / 20
    static let surfaceSpeed = SIMD3<Double>(0.35, 1.0, 1.8)
    /// The noise and rotation frequencies are multiples of 0.01 and 0.05, so these periods wrap without a visible seam.
    static let phasePeriod = 200 * Double.pi
    static let spinPeriod = 40 * Double.pi

    /// Blend of idle, User and Jarvis; always sums to 1.
    public private(set) var weights = SIMD3<Double>(1, 0, 0)
    /// Time of the surface noise, advancing faster while someone speaks.
    public private(set) var phase: Double = 0
    /// Time of the rotation, which runs at one rate in every state.
    public private(set) var spin: Double = 0

    public init() {}

    public func weight(of speaker: OrbModel.Speaker) -> Double { weights[Self.index(speaker)] }

    /// `pace` slows surface motion and rotation, as Reduce Motion asks, without slowing the blend between speakers.
    public mutating func advance(by seconds: Double, toward speaker: OrbModel.Speaker, pace: Double = 1) {
        let dt = min(max(seconds, 0), Self.longestStep)
        var target = SIMD3<Double>(repeating: 0)
        target[Self.index(speaker)] = 1
        weights += (target - weights) * (1 - exp(-dt / Self.blendTime))
        phase = (phase + dt * pace * (weights * Self.surfaceSpeed).sum()).truncatingRemainder(dividingBy: Self.phasePeriod)
        spin = (spin + dt * pace).truncatingRemainder(dividingBy: Self.spinPeriod)
    }

    public func shape(level: Double, reducedMotion: Bool = false) -> OrbShape {
        let level = min(max(level, 0), 1)
        let speech = (weights * SIMD3(0, 0.03 + 0.235 * level, 0.04 + 0.43 * level)).sum()
        let ripple = (weights * SIMD3(0, 0.25, 0.5)).sum()
        return reducedMotion
            ? OrbShape(speech: Float(speech * 0.45), ripple: Float(ripple * 0.4))
            : OrbShape(speech: Float(speech), ripple: Float(ripple))
    }

    private static func index(_ speaker: OrbModel.Speaker) -> Int {
        switch speaker {
        case .idle: 0
        case .user: 1
        case .jarvis: 2
        }
    }
}

/// The idle surface keeps moving in every state; speech adds finer bumps and ripples on top of it.
public struct OrbShape: Sendable {
    static let surface: Float = 0.035
    /// Displacement stays linear up to the knee, then eases toward the limit, which keeps the
    /// perspective silhouette inside a frame whose base radius is 0.34 of its shorter side.
    static let knee: Float = 0.2
    static let limit: Float = 0.29

    public let speech: Float
    public let ripple: Float

    public init(speech: Float, ripple: Float) {
        self.speech = speech
        self.ripple = ripple
    }

    public func radius(surfaceNoise: Float, speechNoise: Float, rippleNoise: Float) -> Float {
        let displacement = Self.surface * surfaceNoise + speech * (speechNoise + ripple * rippleNoise)
        let size = abs(displacement)
        guard size > Self.knee else { return 1 + displacement }
        let eased = Self.knee + (Self.limit - Self.knee) * tanh((size - Self.knee) / (Self.limit - Self.knee))
        return 1 + (displacement < 0 ? -eased : eased)
    }
}
