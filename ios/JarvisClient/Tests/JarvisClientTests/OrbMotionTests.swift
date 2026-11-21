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

@Test func reduceMotionSlowsTheSurfaceButNotTheBlend() {
    var normal = OrbMotion(), reduced = OrbMotion()
    for _ in 0..<24 {
        normal.advance(by: frame, toward: .user)
        reduced.advance(by: frame, toward: .user, pace: 0.35)
    }
    #expect(reduced.weight(of: .user) == normal.weight(of: .user))
    #expect(reduced.phase < normal.phase * 0.5)
}

@Test func idleSurfaceKeepsItsGentleDepth() {
    var motion = OrbMotion()
    motion.advance(by: frame, toward: .idle)
    let idle = motion.shape(level: 0)
    #expect(abs(idle.radius(surfaceNoise: 1.6, speechNoise: 1.6, rippleNoise: 1.6) - 1.056) < 0.002)
}

@Test func loudSpeechStaysWithinThePerspectiveFrame() {
    var motion = OrbMotion()
    for _ in 0..<120 { motion.advance(by: frame, toward: .jarvis) }
    let loud = motion.shape(level: 1)
    // With breathing (x1.02) and the camera at 3, a radius of 1.29 projects to half the frame.
    #expect(loud.radius(surfaceNoise: 1.6, speechNoise: 1.6, rippleNoise: 1.6) <= 1.29)
    #expect(loud.radius(surfaceNoise: -1.6, speechNoise: -1.6, rippleNoise: -1.6) >= 0.71)
}

@Test func halfLevelSpeechDeformsAboutAThirdMoreThanTheFirstOrb() {
    // Typical noise of 0.5: the first orb displaced Jarvis by 0.16 and the User by 0.084 at half level.
    var jarvis = OrbMotion(), user = OrbMotion()
    for _ in 0..<120 {
        jarvis.advance(by: frame, toward: .jarvis)
        user.advance(by: frame, toward: .user)
    }
    let jarvisRadius = jarvis.shape(level: 0.5).radius(surfaceNoise: 0.5, speechNoise: 0.5, rippleNoise: 0.5)
    let userRadius = user.shape(level: 0.5).radius(surfaceNoise: 0.5, speechNoise: 0.5, rippleNoise: 0.5)
    #expect((1.19...1.23).contains(jarvisRadius))
    #expect((1.10...1.12).contains(userRadius))
}
