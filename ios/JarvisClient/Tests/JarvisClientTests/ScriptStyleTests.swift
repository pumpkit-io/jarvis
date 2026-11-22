import Foundation
import Testing
@testable import JarvisClient

private func styleDirectory() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent("JarvisScriptStyle-\(UUID().uuidString)")
}

@MainActor @Test func scriptStyleStartsAsTheOriginalLookAndKeepsTheChoiceAcrossLaunches() {
    let directory = styleDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let first = ScriptStyleSetting(store: ScriptStyleStore(directory: directory))
    #expect(first.style == .original)
    first.style.font = "jetbrains-mono"
    first.style.look = .terminal
    let relaunched = ScriptStyleSetting(store: ScriptStyleStore(directory: directory))
    #expect(relaunched.style == ScriptStyle(font: "jetbrains-mono", look: .terminal))
}

@MainActor @Test func unreadableOrRetiredScriptStyleFallsBackWithoutLosingTheRest() throws {
    let directory = styleDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let store = ScriptStyleStore(directory: directory)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    try Data("not json".utf8).write(to: store.file)
    #expect(ScriptStyleSetting(store: store).style == .original)
    try Data(#"{"font":"hack","look":"retired"}"#.utf8).write(to: store.file)
    #expect(ScriptStyleSetting(store: store).style == ScriptStyle(font: "hack", look: .current))
}
