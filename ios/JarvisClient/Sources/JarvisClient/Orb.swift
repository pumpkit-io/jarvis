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

    /// Blend of idle, User and Jarvis; always sums to 1.
    public private(set) var weights = SIMD3<Double>(1, 0, 0)
    /// Time of the surface noise, advancing faster while someone speaks.
    public private(set) var phase: Double = 0
    /// Time of the rotation, which runs at one rate in every state.
    public private(set) var spin: Double = 0

    public init() {}

    public func weight(of speaker: OrbModel.Speaker) -> Double { weights[Self.index(speaker)] }

    public mutating func advance(by seconds: Double, toward speaker: OrbModel.Speaker) {
        let dt = min(max(seconds, 0), Self.longestStep)
        var target = SIMD3<Double>(repeating: 0)
        target[Self.index(speaker)] = 1
        weights += (target - weights) * (1 - exp(-dt / Self.blendTime))
        phase += dt * (weights * Self.surfaceSpeed).sum()
        spin += dt
    }

    public func shape(level: Double) -> OrbShape {
        let level = min(max(level, 0), 1)
        let speech = weights * SIMD3(0, 0.29 * level, 0.52 * level)
        let ripple = weights * SIMD3(0, 0.3, 0.7)
        return OrbShape(speech: Float(speech.sum()), ripple: Float(ripple.sum()))
    }

    private static func index(_ speaker: OrbModel.Speaker) -> Int {
        switch speaker {
        case .idle: 0
        case .user: 1
        case .jarvis: 2
        }
    }
}

/// The idle surface moves in every state; speech adds finer bumps on top, so the orb itself never changes.
public struct OrbShape: Sendable {
    static let surface: Float = 0.035
    public let speech: Float
    public let ripple: Float
    /// Displacement stays linear up to the knee, then eases toward the limit so loud peaks stay inside the frame.
    static let knee: Float = 0.34
    static let limit: Float = 0.46

    public init(speech: Float, ripple: Float) {
        self.speech = speech
        self.ripple = ripple
    }

    /// Radius of a unit-sphere point from its idle surface noise and its speech and ripple noise.
    public func radius(surface: Float, speech bumps: Float, ripple fine: Float) -> Float {
        let displacement = Self.surface * surface + speech * (bumps + ripple * fine)
        let size = abs(displacement)
        guard size > Self.knee else { return 1 + displacement }
        let eased = Self.knee + (Self.limit - Self.knee) * tanh((size - Self.knee) / (Self.limit - Self.knee))
        return 1 + (displacement < 0 ? -eased : eased)
    }
}
