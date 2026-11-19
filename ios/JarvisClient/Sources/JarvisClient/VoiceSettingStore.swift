import CryptoKit
import Foundation

public struct VoiceSettingStore: Sendable {
    private let directory: URL
    public init(directory: URL) { self.directory = directory }

    public static var applicationStore: Self {
        Self(directory: URL.applicationSupportDirectory.appendingPathComponent("Jarvis/Voice", isDirectory: true))
    }

    public func load(_ credentials: PairingCredentials) throws -> Bool? {
        let file = file(credentials)
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        return try JSONDecoder().decode(Bool.self, from: Data(contentsOf: file))
    }

    public func save(_ silence: Bool, for credentials: PairingCredentials) throws {
        let file = file(credentials)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try JSONEncoder().encode(silence).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var location = file; try location.setResourceValues(values)
    }

    public func clear(_ credentials: PairingCredentials) throws {
        let file = file(credentials)
        if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
        let gesture = file.deletingPathExtension().appendingPathExtension("gesture.json")
        if FileManager.default.fileExists(atPath: gesture.path) { try FileManager.default.removeItem(at: gesture) }
    }

    public func loadGesture(_ credentials: PairingCredentials) throws -> TalkGesture? {
        let file = file(credentials).deletingPathExtension().appendingPathExtension("gesture.json")
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        return try JSONDecoder().decode(TalkGesture.self, from: Data(contentsOf: file))
    }

    public func saveGesture(_ gesture: TalkGesture, for credentials: PairingCredentials) throws {
        let file = file(credentials).deletingPathExtension().appendingPathExtension("gesture.json")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try JSONEncoder().encode(gesture).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        var location = file; try location.setResourceValues(values)
    }

    private func file(_ credentials: PairingCredentials) -> URL {
        let identity = credentials.origin.absoluteString + "\n" + credentials.clientID
        let key = SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(key).appendingPathExtension("json")
    }
}
