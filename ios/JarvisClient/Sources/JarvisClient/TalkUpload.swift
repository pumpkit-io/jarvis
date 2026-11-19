import Foundation

public struct TalkUpload: Sendable {
    public typealias Post = @Sendable (URLRequest) async throws -> (Data, Int)
    private let post: Post
    public init(post: @escaping Post = TalkUpload.urlSessionPost) { self.post = post }

    public func send(credentials: PairingCredentials, talkID: String, commandID: String, pcm: Data) async throws -> CommandResult {
        guard !talkID.isEmpty, talkID.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 }),
              !commandID.isEmpty, !pcm.isEmpty, pcm.count <= 9_600_000, pcm.count.isMultiple(of: 2) else { throw HubProtocolError.invalidFrame }
        let url = credentials.origin.appendingPathComponent("v1/utterances").appendingPathComponent(talkID)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"; request.httpBody = pcm; request.timeoutInterval = 30
        request.setValue("Bearer \(credentials.deviceToken)", forHTTPHeaderField: "Authorization")
        request.setValue(commandID, forHTTPHeaderField: "Idempotency-Key")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue("pcm_s16le_16000_mono", forHTTPHeaderField: "X-Jarvis-Audio-Format")
        let (data, status) = try await post(request)
        guard status == 200 else { throw HubTransportError.http(status) }
        let result = try JSONDecoder().decode(CommandResult.self, from: data)
        guard result.id == commandID, result.ok, result.result?["talk_id"] == .string(talkID) else { throw HubProtocolError.invalidFrame }
        return result
    }

    public static func urlSessionPost(_ request: URLRequest) async throws -> (Data, Int) {
        let session = URLSession(configuration: .ephemeral, delegate: TalkRedirectRefusal(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw HubProtocolError.invalidFrame }
        return (data, response.statusCode)
    }
}

private final class TalkRedirectRefusal: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
