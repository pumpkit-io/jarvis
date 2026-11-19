import Foundation

public struct PairingInput: Equatable, Sendable {
    public let origin: URL
    public let code: String

    public init(origin: String, code: String) throws {
        self.origin = try Self.normalizedOrigin(origin)
        guard code.utf8.count == 6, code.utf8.allSatisfy({ (48...57).contains($0) }) else { throw PairingError.invalidCode }
        self.code = code
    }

    public init(qr: String) throws {
        struct Payload: Decodable { let version: Int; let origin: String; let code: String }
        guard let data = qr.data(using: .utf8), let payload = try? JSONDecoder().decode(Payload.self, from: data),
              payload.version == 1 else { throw PairingError.invalidQR }
        try self.init(origin: payload.origin, code: payload.code)
    }

    public static func normalizedOrigin(_ value: String) throws -> URL {
        guard var parts = URLComponents(string: value), parts.scheme?.lowercased() == "https",
              let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil, parts.path.isEmpty || parts.path == "/",
              parts.port.map({ (1...65535).contains($0) }) ?? true else { throw PairingError.invalidOrigin }
        parts.scheme = "https"; parts.host = host.lowercased(); parts.path = ""
        if parts.port == 443 { parts.port = nil }
        guard let result = parts.url else { throw PairingError.invalidOrigin }
        return result
    }
}

public enum PairingError: Error, Equatable, Sendable {
    case invalidCode, expiredCode, locked, unreachable, invalidOrigin, invalidQR, invalidResponse, storage, cameraUnavailable

    public var message: String {
        switch self {
        case .invalidCode: "That code isn't right. Check it and try again."
        case .expiredCode: "That code has expired. Run ./jarvis pair again."
        case .unreachable: "Can't reach the Mac. Check that Tailscale is on."
        case .locked: "Too many attempts. Run ./jarvis pair again."
        case .invalidOrigin: "Enter the HTTPS address printed by ./jarvis pair."
        case .invalidQR: "That isn't a Jarvis pairing code. Run ./jarvis pair and scan again."
        case .invalidResponse: "The Mac returned an invalid pairing response. Run ./jarvis pair again."
        case .cameraUnavailable: "Couldn't open the camera. Type the code instead."
        case .storage: "Couldn't save the pairing securely. Unlock the iPhone and try again with a new code."
        }
    }
}

public struct PairingCredentials: Codable, Equatable, Sendable {
    public let origin: URL
    public let clientID: String
    public let deviceToken: String
    public let macName: String
    public let clientName: String?

    public init(origin: URL, clientID: String, deviceToken: String, macName: String, clientName: String? = nil) throws {
        self.origin = try PairingInput.normalizedOrigin(origin.absoluteString)
        guard Self.valid(clientID), Self.valid(deviceToken), Self.valid(macName),
              clientName.map(Self.valid) ?? true else { throw PairingError.invalidResponse }
        self.clientID = clientID; self.deviceToken = deviceToken; self.macName = macName; self.clientName = clientName
    }

    private static func valid(_ text: String) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.unicodeScalars.allSatisfy { $0.value >= 32 && $0.value != 127 }
    }

    public func validated() throws -> Self {
        try Self(origin: origin, clientID: clientID, deviceToken: deviceToken, macName: macName, clientName: clientName)
    }
}

public struct PairingService: Sendable {
    public typealias Post = @Sendable (URLRequest) async throws -> (Data, Int)
    private let post: Post
    public init(post: @escaping Post) { self.post = post }

    public func pair(_ input: PairingInput, clientName: String = "iPhone") async throws -> PairingCredentials {
        struct Request: Encodable { let version = 1; let code: String; let name: String; let kind = "iphone" }
        var request = URLRequest(url: input.origin.appendingPathComponent("v1/pair"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Request(code: input.code, name: clientName))
        let data: Data; let status: Int
        do { (data, status) = try await post(request) }
        catch is CancellationError { throw CancellationError() }
        catch { throw PairingError.unreachable }
        guard status == 201 else {
            struct Failure: Decodable { struct Detail: Decodable { let code: String }; let error: Detail }
            let code = (try? JSONDecoder().decode(Failure.self, from: data))?.error.code
            if code == "invalid_code" { throw PairingError.invalidCode }
            if code == "code_expired" { throw PairingError.expiredCode }
            if status == 429 && code == "pairing_locked" { throw PairingError.locked }
            throw PairingError.invalidResponse
        }
        struct Response: Decodable {
            let version: Int; let client_id: String; let device_token: String; let mac_name: String; let origin: String; let client_name: String?
        }
        guard let response = try? JSONDecoder().decode(Response.self, from: data),
              response.version == 1, let origin = try? PairingInput.normalizedOrigin(response.origin), origin == input.origin else { throw PairingError.invalidResponse }
        return try PairingCredentials(origin: origin, clientID: response.client_id, deviceToken: response.device_token,
                                      macName: response.mac_name, clientName: response.client_name)
    }

    public static let urlSession = PairingService { request in
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 15
        configuration.httpShouldSetCookies = false
        let session = URLSession(configuration: configuration, delegate: PairingRedirectGuard(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw PairingError.invalidResponse }
        return (data, http.statusCode)
    }
}

private final class PairingRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
