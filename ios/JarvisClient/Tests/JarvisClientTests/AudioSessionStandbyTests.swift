import Foundation
import Testing
@testable import JarvisClient

@MainActor private final class RecordingDriver: AudioSessionDriver {
    enum Call: Equatable { case apply(AudioSessionSetup, activate: Bool), deactivate }
    var calls: [Call] = []
    var failing = false
    func apply(_ setup: AudioSessionSetup, activate: Bool) throws {
        if failing { throw HubProtocolError.invalidFrame }
        calls.append(.apply(setup, activate: activate))
    }
    func deactivate() { calls.append(.deactivate) }
}

@MainActor @Test func talkInStandbyOnlyChangesTheDuckNeverTheActivation() throws {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    session.standby()
    driver.calls = []
    try session.activate()
    session.release()
    #expect(driver.calls == [.apply(.recording, activate: false), .apply(.standby, activate: false)])
    try session.activate()
    session.release()
    #expect(!driver.calls.contains(.deactivate) && !driver.calls.contains(.apply(.recording, activate: true)))
}

@MainActor @Test func standbyActivatesOnceWithoutDucking() {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    session.standby()
    session.standby()
    #expect(driver.calls == [.apply(.standby, activate: true)])
}

@MainActor @Test func withoutStandbyATalkActivatesAndDeactivatesAsBefore() throws {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    try session.activate()
    session.release()
    #expect(driver.calls == [.apply(.recording, activate: true), .deactivate])
}

@MainActor @Test func endingStandbyDeactivatesOnceAndOnlyWhenIdle() throws {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    session.standby()
    try session.activate()
    driver.calls = []
    session.endStandby()
    #expect(driver.calls.isEmpty)
    session.release()
    #expect(driver.calls == [.deactivate])
    session.endStandby()
    #expect(driver.calls == [.deactivate])
}

@MainActor @Test func aReplyDeactivatesSoMusicAndPodcastsRecoverThenStandbyResumes() throws {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    session.standby()
    driver.calls = []
    try session.activatePlayback()
    session.releasePlayback()
    #expect(driver.calls == [.apply(.playback, activate: true), .deactivate, .apply(.standby, activate: true)])
    driver.calls = []
    try session.activate()
    session.release()
    #expect(driver.calls == [.apply(.recording, activate: false), .apply(.standby, activate: false)])
}

@MainActor @Test func liveModeKeepsItsSessionAndStandbyResumesWhenItEnds() throws {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    session.standby()
    driver.calls = []
    try session.activateLive()
    session.standby()
    session.releaseLive()
    #expect(driver.calls == [.apply(.recording, activate: true), .deactivate, .apply(.standby, activate: true)])
}

@MainActor @Test func aFailedStandbyReleaseFallsBackToDeactivating() throws {
    let driver = RecordingDriver(), session = NativeTalkAudioSession(driver: driver)
    session.standby()
    try session.activate()
    driver.calls = []
    driver.failing = true
    session.release()
    #expect(driver.calls == [.deactivate])
    driver.failing = false
    try session.activate()
    #expect(driver.calls == [.deactivate, .apply(.recording, activate: true)])
}
