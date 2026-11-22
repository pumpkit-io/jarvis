import Foundation
import Observation

// Temporary: lets the User compare conversation fonts and looks. ios/README.md lists what to remove.

/// The font is an id from the app's font catalog.
public struct ScriptStyle: Codable, Equatable, Sendable {
    public enum Look: String, Codable, CaseIterable, Sendable { case current, terminal, gutter }

    public var font: String
    public var look: Look

    public static let original = ScriptStyle(font: "original", look: .current)

    public init(font: String, look: Look) { self.font = font; self.look = look }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        font = try container.decodeIfPresent(String.self, forKey: .font) ?? Self.original.font
        // A look removed in a later build falls back alone, keeping the chosen font.
        look = (try? container.decode(Look.self, forKey: .look)) ?? Self.original.look
    }
}

public struct ScriptStyleStore: Sendable {
    private let directory: URL
    public init(directory: URL) { self.directory = directory }

    public static var applicationStore: Self {
        Self(directory: URL.applicationSupportDirectory.appendingPathComponent("Jarvis/Display", isDirectory: true))
    }

    var file: URL { directory.appendingPathComponent("script-style.json") }

    public func load() throws -> ScriptStyle? {
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        return try JSONDecoder().decode(ScriptStyle.self, from: Data(contentsOf: file))
    }

    public func save(_ style: ScriptStyle) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try JSONEncoder().encode(style).write(to: file, options: .atomic)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var location = file; try location.setResourceValues(values)
    }
}

/// The style the conversation draws with, saved whenever the User changes it.
@MainActor @Observable public final class ScriptStyleSetting {
    public var style: ScriptStyle {
        // A failed save only loses the choice at the next launch; the screen still shows it now.
        didSet { if style != oldValue { try? store.save(style) } }
    }
    private let store: ScriptStyleStore

    public init(store: ScriptStyleStore = .applicationStore) {
        self.store = store
        style = (try? store.load()) ?? .original
    }
}
