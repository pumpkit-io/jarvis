import Testing
@testable import JarvisClient

private let frame = 1.0 / 60

@Test func everySpeakerChangeBlendsInTheSameTimeOfAboutFourTenthsOfASecond() {
    let speakers: [OrbModel.Speaker] = [.idle, .user, .jarvis]
    var durations: [Int] = []
    for from in speakers {
        for to in speakers where to != from {
            var motion = OrbMotion()
            for _ in 0..<120 { motion.advance(by: frame, toward: from) }
            var frames = 0
            while motion.weight(of: to) < 0.95 { motion.advance(by: frame, toward: to); frames += 1 }
            durations.append(frames)
        }
    }
    #expect(Set(durations).count == 1)
    #expect((0.35...0.45).contains(Double(durations[0]) * frame))
}

@Test func aSpeakerChangeDoesNotJumpInOneFrame() {
    var motion = OrbMotion()
    for _ in 0..<120 { motion.advance(by: frame, toward: .idle) }
    let phase = motion.phase
    motion.advance(by: frame, toward: .jarvis)
    #expect(motion.weight(of: .jarvis) < 0.2)
    #expect(motion.phase > phase && motion.phase - phase < 0.04)
}

@Test func aLongPauseResumesWhereTheOrbStopped() {
    var motion = OrbMotion()
    motion.advance(by: frame, toward: .idle)
    let phase = motion.phase, spin = motion.spin
    motion.advance(by: 5, toward: .idle)
    #expect(motion.phase - phase < 0.05)
    #expect(motion.spin - spin < 0.06)
}

@Test func idleShapeIsUnchangedAndLoudSpeechStaysInsideTheFrame() {
    var motion = OrbMotion()
    motion.advance(by: frame, toward: .idle)
    #expect(abs(motion.shape(level: 0).radius(surface: 1.6, speech: 0, ripple: 0) - 1.056) < 0.002)
    for _ in 0..<120 { motion.advance(by: frame, toward: .jarvis) }
    let loud = motion.shape(level: 1)
    #expect(loud.radius(surface: 1.6, speech: 1.6, ripple: 1.6) <= 1.46)
    #expect(loud.radius(surface: -1.6, speech: -1.6, ripple: -1.6) > 0.539)
}

@Test func speechDeformsTheOrbMoreThanBefore() {
    var motion = OrbMotion()
    for _ in 0..<120 { motion.advance(by: frame, toward: .jarvis) }
    // Before this change Jarvis at half level reached 1 + 0.25 × (1 + 0.3) for unit noise.
    #expect(motion.shape(level: 0.5).radius(surface: 1, speech: 1, ripple: 1) > 1.38)
    var user = OrbMotion()
    for _ in 0..<120 { user.advance(by: frame, toward: .user) }
    // The User at half level reached 1 + 0.15 × (1 + 0.125).
    #expect(user.shape(level: 0.5).radius(surface: 1, speech: 1, ripple: 1) > 1.21)
}
