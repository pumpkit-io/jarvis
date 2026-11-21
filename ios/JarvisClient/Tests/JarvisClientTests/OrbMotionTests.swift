import Testing
@testable import JarvisClient

private let frame = 1.0 / 60

private func settled(_ speaker: OrbModel.Speaker, level: Double = 1, seconds: Double = 2) -> OrbMotion {
    var motion = OrbMotion()
    for _ in 0..<Int(seconds / frame) { motion.advance(by: frame, toward: speaker, level: level) }
    return motion
}

private func close(_ a: OrbSurface, _ b: OrbSurface, within tolerance: Float = 0.002) -> Bool {
    abs(a.ripple - b.ripple) < tolerance && abs(a.flow - b.flow) < tolerance
        && abs(a.looseShare - b.looseShare) < tolerance && abs(a.looseDrift - b.looseDrift) < tolerance
        && abs(a.speed - b.speed) < tolerance
}

@Test func idleIsACalmSphereWhateverTheLevel() {
    let idle = settled(.idle, level: 1)
    #expect(idle.level == 0 && idle.pulse == 0)
    #expect(close(idle.surface, OrbSurface(ripple: 0.02, flow: 0.02, looseShare: 0.05, looseDrift: 0.03, speed: 0.4)))
}

@Test func loudJarvisTearsTheSurfaceFurtherThanALoudUser() {
    let jarvis = settled(.jarvis).surface, user = settled(.user).surface
    #expect(close(jarvis, OrbSurface(ripple: 0.13, flow: 0.13, looseShare: 0.2, looseDrift: 0.22, speed: 2)))
    #expect(close(user, OrbSurface(ripple: 0.1, flow: 0.1, looseShare: 0.15, looseDrift: 0.16, speed: 1.3)))
    #expect(abs(settled(.jarvis).pulse - 0.06) < 0.001)
}

@Test func aSpeakerChangeDoesNotJumpInOneFrame() {
    var motion = settled(.idle)
    let before = motion.surface.speed
    motion.advance(by: frame, toward: .jarvis, level: 1)
    #expect(motion.surface.speed - before < 0.08 * (2 - before) + 0.001)
    #expect(motion.level <= 0.25 + 0.001)
}

@Test func easingSettlesInTheSameTimeAtAnyFrameRate() {
    var fast = settled(.idle), slow = settled(.idle)
    for _ in 0..<60 { fast.advance(by: 1.0 / 60, toward: .jarvis, level: 0.7) }
    for _ in 0..<30 { slow.advance(by: 1.0 / 30, toward: .jarvis, level: 0.7) }
    #expect(close(fast.surface, slow.surface, within: 0.001))
    #expect(abs(fast.level - slow.level) < 0.001)
}

@Test func theSurfaceFlowsFasterWhileSomeoneSpeaks() {
    var idle = settled(.idle), jarvis = settled(.jarvis)
    let idlePhase = idle.phase, jarvisPhase = jarvis.phase
    idle.advance(by: frame, toward: .idle, level: 0)
    jarvis.advance(by: frame, toward: .jarvis, level: 1)
    #expect(jarvis.phase - jarvisPhase > (idle.phase - idlePhase) * 4)
}

@Test func aLongPauseResumesWhereTheOrbStopped() {
    var motion = settled(.jarvis)
    let phase = motion.phase, time = motion.time
    motion.advance(by: 5, toward: .jarvis, level: 1)
    #expect(motion.time - time <= 0.05 + 1e-9)
    #expect(motion.phase - phase <= 0.1 + 1e-6)
}

@Test func reduceMotionSlowsTheOrbAndSoftensSpeech() {
    var normal = OrbMotion(), reduced = OrbMotion()
    for _ in 0..<120 {
        normal.advance(by: frame, toward: .jarvis, level: 1)
        reduced.advance(by: frame, toward: .jarvis, level: 1, reducedMotion: true)
    }
    #expect(reduced.time < normal.time * 0.5)
    #expect(reduced.phase < normal.phase * 0.5)
    #expect(reduced.surface.looseDrift < normal.surface.looseDrift * 0.7)
}
