import Foundation

public struct OpenTask: Identifiable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let lastReport: String?
    public let waitingForUser: Bool
    public let approvalQuestion: String?

    static func current(in events: [HubEvent], now: Date) -> [OpenTask] {
        guard let snapshot = events.last(where: { $0.kind == .taskSnapshot }),
              let payload = try? snapshot.decodePayload(TaskSnapshot.self) else { return [] }
        let pending = events.last(where: { $0.kind == .pendingApproval && $0.seq > snapshot.seq })
            .flatMap { try? $0.decodePayload(PendingApprovals.self) }
        return payload.tasks.filter { ["queued", "working", "needs_user"].contains($0.state) }.map { task in
            let question: String?
            if let pending {
                question = pending.approvals.first {
                    $0.task_id == task.id && (utcDate($0.expires_at).map { $0 > now } ?? false)
                }?.question
            } else { question = task.approval_question }
            return OpenTask(id: task.id, title: task.title, lastReport: task.last_report,
                            waitingForUser: task.state == "needs_user", approvalQuestion: question)
        }.sorted { $0.waitingForUser && !$1.waitingForUser }
    }
}
