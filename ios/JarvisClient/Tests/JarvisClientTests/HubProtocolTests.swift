import Foundation
import Testing
@testable import JarvisClient

@Test func pythonExchangesDecodeWithoutRetyping() throws {
    let url = try #require(Bundle.module.url(forResource: "client-v1", withExtension: "json", subdirectory: "Fixtures"))
    let root = try JSONSerialization.jsonObject(with: Data(contentsOf: url))
    var events = 0
    var results = 0
    func visit(_ value: Any) throws {
        if let object = value as? [String: Any] {
            if object["seq"] != nil && object["time"] != nil {
                let frame = try HubFrame.decode(JSONSerialization.data(withJSONObject: object))
                guard case .event = frame else { Issue.record("Expected event"); return }
                events += 1
                return
            }
            if let type = object["type"] as? String, ["command_result", "replay_complete"].contains(type) {
                _ = try HubFrame.decode(JSONSerialization.data(withJSONObject: object))
                results += 1
                return
            }
            for child in object.values { try visit(child) }
        } else if let array = value as? [Any] { for child in array { try visit(child) } }
    }
    try visit(root)
    #expect(events > 10)
    #expect(results > 10)
}

@Test func exactSequenceAndUnknownEvents() throws {
    let frame = try HubFrame.decode(Data(#"{"version":1,"seq":9007199254740993,"time":"2026-11-19T12:00:00.123Z","type":"future","client_id":null,"payload":{"new":1}}"#.utf8))
    guard case .event(let event) = frame else { Issue.record("Expected event"); return }
    #expect(event.seq == 9_007_199_254_740_993)
    #expect(event.kind == .unknown("future"))
    #expect(throws: (any Error).self) {
        try HubFrame.decode(Data(#"{"version":2,"type":"replay_complete","through_seq":0}"#.utf8))
    }
}

@Test func documentedPayloadsDecodeAtPublicBoundary() throws {
    let payloads: [(String, String)] = [
        ("heard", #"{"request_id":"r","text":"hello","spoken":true}"#),
        ("reply_delta", #"{"reply_id":"p","request_id":null,"text":"hi"}"#),
        ("reply_end", #"{"reply_id":"p","request_id":null,"text":"hi","reason":"complete"}"#),
        ("notice", #"{"text":"waiting","count":2,"task_ids":["t"]}"#),
        ("show", #"{"reply_id":null,"title":"details","text":"result"}"#),
        ("task_snapshot", #"{"tasks":[{"id":"t","title":"Task","state":"working","last_report":null,"approval_question":null}]}"#),
        ("pending_approval", #"{"approvals":[{"id":"a","task_id":"t","question":"Merge?","expires_at":"2026-11-19T00:00:00Z"}]}"#),
        ("status", #"{"ready":true,"usage_limit":{"limited":true,"resets_at":"2026-11-19T00:00:00Z"},"quiet":false,"focus_client_id":null,"mac_voice_on":true,"silence":true}"#),
        ("speech_begin", #"{"speech_id":"s","reply_id":"p","sample_rate":24000,"channels":1,"sample_width":2}"#),
        ("speech_clip", #"{"speech_id":"s","reply_id":"p","index":0,"text":"hi","byte_length":2}"#),
        ("speech_end", #"{"speech_id":"s","reply_id":"p","last_clip_index":0}"#),
        ("speech_stop", #"{"speech_id":"s","reply_id":"p","reason":"silence"}"#)
    ]
    for (type, payload) in payloads {
        let data = Data("{\"version\":1,\"seq\":1,\"time\":\"2026-11-19T00:00:00Z\",\"type\":\"\(type)\",\"payload\":\(payload)}".utf8)
        guard case .event(let event) = try HubFrame.decode(data) else { Issue.record("Expected event"); continue }
        #expect(event.type == type)
    }
}
