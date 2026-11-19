import Foundation
import Testing
@testable import JarvisClient

@Test func pairingInputPreservesZeroesAndRejectsUnsafeOrigins() throws {
    let input = try PairingInput(origin: "https://MAC.example:443/", code: "001234")
    #expect(input.origin.absoluteString == "https://mac.example")
    #expect(input.code == "001234")
    #expect(throws: (any Error).self) { try PairingInput(origin: "http://mac.example", code: "001234") }
    #expect(throws: (any Error).self) { try PairingInput(origin: "https://mac.example/path", code: "001234") }
    #expect(throws: (any Error).self) { try PairingInput(origin: "https://mac.example", code: "１２３４５６") }
    #expect(try PairingInput(qr: #"{"version":1,"origin":"https://mac.example","code":"001234"}"#) == input)
    #expect(throws: (any Error).self) { try PairingInput(qr: #"{"version":2,"origin":"https://mac.example","code":"001234"}"#) }
}

@Test func pairingPostsOnceAndChecksResponseOrigin() async throws {
    let input = try PairingInput(origin: "https://mac.example", code: "001234")
    let service = PairingService { request in
        #expect(request.value(forHTTPHeaderField: "Authorization") == nil)
        let body = try #require(JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any])
        #expect(body["code"] as? String == "001234")
        return (Data(#"{"version":1,"client_id":"iphone-1","device_token":"secret","mac_name":"Mac","origin":"https://evil.example"}"#.utf8), 201)
    }
    await #expect(throws: PairingError.invalidResponse) { try await service.pair(input) }
}

@Test func pairingErrorUsesProtocolEnvelope() async throws {
    let service = PairingService { _ in (Data(#"{"version":1,"error":{"code":"code_expired","message":"expired"}}"#.utf8), 400) }
    await #expect(throws: PairingError.expiredCode) {
        try await service.pair(PairingInput(origin: "https://mac.example", code: "001234"))
    }
}

@Test(arguments: [(400, "invalid_code", PairingError.invalidCode), (400, "code_expired", .expiredCode),
                  (429, "pairing_locked", .locked), (302, "redirect", .invalidResponse)])
func pairingShowsSpecificFailuresWithoutRetry(status: Int, code: String, expected: PairingError) async throws {
    let calls = PairingCalls()
    let service = PairingService { _ in
        await calls.increment()
        return (try JSONSerialization.data(withJSONObject: ["version": 1, "error": ["code": code]]), status)
    }
    await #expect(throws: expected) { try await service.pair(PairingInput(origin: "https://mac.example", code: "001234")) }
    #expect(await calls.count == 1)
}

actor PairingCalls { var count = 0; func increment() { count += 1 } }

@Test func lostPairingResponseIsNeverAutomaticallyRepeated() async throws {
    let calls = PairingCalls()
    let service = PairingService { _ in await calls.increment(); throw URLError(.networkConnectionLost) }
    await #expect(throws: PairingError.unreachable) { try await service.pair(PairingInput(origin: "https://mac.example", code: "001234")) }
    #expect(await calls.count == 1)
}

@Test func pairingAcceptsNormalizedOriginButRequiresProtocolVersion() async throws {
    let input = try PairingInput(origin: "https://mac.example", code: "001234")
    let service = PairingService { request in
        #expect(request.url?.path == "/v1/pair")
        let data = try #require(request.httpBody)
        let body = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(body["name"] as? String == "My iPhone")
        #expect(body["kind"] as? String == "iphone")
        return (Data(#"{"version":1,"client_id":"iphone-1","device_token":"secret","mac_name":"Mac","origin":"https://MAC.example:443/","client_name":"My iPhone (2)"}"#.utf8), 201)
    }
    let credentials = try await service.pair(input, clientName: "My iPhone")
    #expect(credentials.origin == input.origin)
    #expect(credentials.clientName == "My iPhone (2)")
    let incompatible = PairingService { _ in
        (Data(#"{"version":2,"client_id":"iphone-1","device_token":"secret","mac_name":"Mac","origin":"https://mac.example"}"#.utf8), 201)
    }
    await #expect(throws: PairingError.invalidResponse) { try await incompatible.pair(input) }
}
