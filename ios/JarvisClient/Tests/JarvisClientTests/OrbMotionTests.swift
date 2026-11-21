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
    let idle = settled(.idle, level: 1), quiet = settled(.idle, level: 0)
    #expect(idle.level == 0 && idle.pulse == 0)
    #expect(close(idle.surface, quiet.surface))
}

@Test func loudJarvisTearsTheSurfaceFurtherThanALoudUser() {
    let jarvis = settled(.jarvis), user = settled(.user), idle = settled(.idle)
    #expect(jarvis.surface.ripple > user.surface.ripple && user.surface.ripple > idle.surface.ripple)
    #expect(jarvis.surface.looseShare > user.surface.looseShare && jarvis.surface.looseDrift > user.surface.looseDrift)
    #expect(jarvis.surface.speed > user.surface.speed && jarvis.pulse > 0)
}

@Test func louderSpeechDeformsMore() {
    let soft = settled(.jarvis, level: 0.2), loud = settled(.jarvis, level: 0.9)
    #expect(loud.surface.ripple > soft.surface.ripple && loud.surface.looseDrift > soft.surface.looseDrift)
    #expect(loud.pulse > soft.pulse)
}

@Test func aSpeakerChangeEasesOverSeveralFramesInsteadOfJumping() {
    var motion = settled(.idle)
    let start = motion.surface, end = settled(.jarvis).surface
    motion.advance(by: frame, toward: .jarvis, level: 1)
    let firstStep = (motion.surface.speed - start.speed) / (end.speed - start.speed)
    #expect(firstStep > 0 && firstStep < 0.2)
    for _ in 0..<60 { motion.advance(by: frame, toward: .jarvis, level: 1) }
    #expect(close(motion.surface, end, within: 0.02))
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

@Test func clocksStayWithinTheirSeamlessPeriodsOverLongSessions() {
    var motion = OrbMotion()
    for _ in 0..<200_000 { motion.advance(by: 1.0 / 20, toward: .jarvis, level: 1) }
    #expect(motion.time >= 0 && motion.time < OrbMotion.timePeriod)
    #expect(motion.phase >= 0 && motion.phase < OrbMotion.phasePeriod)
}
