import Foundation
import Security

@MainActor public protocol PairingStore {
    func load() throws -> PairingCredentials?
    func save(_ credentials: PairingCredentials) throws
    func clear() throws
}

@MainActor public struct KeychainRecordAccess {
    public let read: () throws -> Data?
    public let write: (Data) throws -> Void
    public let remove: () throws -> Void

    public init(read: @escaping () throws -> Data?, write: @escaping (Data) throws -> Void, remove: @escaping () throws -> Void) {
        self.read = read; self.write = write; self.remove = remove
    }

    public static var system: Self {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: "io.pumpkit.jarvis.pairing", kSecAttrAccount as String: "current"]
        return Self(read: {
            var request = query
            request[kSecReturnData as String] = true
            request[kSecMatchLimit as String] = kSecMatchLimitOne
            var result: CFTypeRef?
            let status = SecItemCopyMatching(request as CFDictionary, &result)
            if status == errSecItemNotFound { return nil }
            guard status == errSecSuccess, let data = result as? Data else { throw PairingError.storage }
            return data
        }, write: { data in
            let values: [String: Any] = [kSecValueData as String: data,
                                        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
            let status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
            if status == errSecItemNotFound {
                guard SecItemAdd(query.merging(values) { _, new in new } as CFDictionary, nil) == errSecSuccess else { throw PairingError.storage }
            } else if status != errSecSuccess { throw PairingError.storage }
        }, remove: {
            let status = SecItemDelete(query as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else { throw PairingError.storage }
        })
    }
}

@MainActor public final class KeychainPairingStore: PairingStore {
    private let access: KeychainRecordAccess
    public init(access: KeychainRecordAccess = .system) { self.access = access }
    public func load() throws -> PairingCredentials? {
        guard let data = try access.read() else { return nil }
        return try JSONDecoder().decode(PairingCredentials.self, from: data).validated()
    }
    public func save(_ credentials: PairingCredentials) throws { try access.write(JSONEncoder().encode(credentials.validated())) }
    public func clear() throws { try access.remove() }
}
