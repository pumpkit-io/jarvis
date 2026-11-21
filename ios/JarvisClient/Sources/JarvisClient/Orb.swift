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
