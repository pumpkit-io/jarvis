# Resolve Task scope after local spoken admission

The local assessor establishes floor admission or voice-only stop. Its amend/new_request outputs, if present, are hints. It cannot discard earlier words or choose which Task changes. No failed research model is wired.

## Invariants

1. A spoken generation has a stable request identity, increasing revision and ordered raw segments. Every positive observation preserves prior segments and appends the full assessed fragments. IDs are unique across launches. Logical onset metadata and current estimated played text keep their documented timing.
2. Before awaiting old-generation interruption, VoiceMode synchronously asks the Hub to record the locally admitted revision. The Hub atomically persists its raw segments and a pending scope hold on already-owned Tasks. Task requests, Approvals and producer revisions are not rewritten merely because of a local relation hint.
3. A pending hold blocks Task tool/action authority. The existing exact Approval checks remain authoritative. Only the admitted Talker can resolve scope: an explicit complete set of owned Task IDs to revise, a new independent request, or a conversation-only response. Missing selection never releases an old Approval by default.
4. Scope resolution is a Hub transaction. Selected Tasks get the accepted raw request context, incremented producer revision, obsolete Approval withdrawal and producer cancellation before chosen work. Unselected Tasks keep their request/Approval targets and resume. Stale, conflicting or repeated resolutions are refused. Every pending scope requires explicit `resolve_scope` before `delegate`, including a scope with only one owned Task.
5. Holds and raw intent survive restart. Recovery exposes pending scope to the ordinary Talker as an already-admitted text-only turn, while held Tasks cannot run old recovery work. It does not depend on a restarted VoiceMode owning the old request. A bounded resolution failure leaves work safely held with a precise notice and retained context for the next User turn, rather than silently releasing authority.
6. A validated new-request resolution may select a starting raw segment for future local semantic context. Historical segments stay available for audit/context. Model paraphrases cannot replace or index the source text.

## Interfaces

- `Line`: raw `segments`, append boundary and optional local hint, alongside generation request ID/revision.
- `VoiceMode.lines(..., admitted=hub.admit_spoken_revision)`: synchronous admission callback before interruption awaits.
- Hub-owned `SpokenScopes`: pending/resolved records, Task holds, validation, durable load/dump and admission context for the Talker. It has no model API.
- Talker `resolve_scope`: scope ID/revision, disposition (`revise_tasks`, `new_request`, `preserve_tasks`), owned Task IDs when revising, optional validated new-request raw boundary. This resolves ownership, not the content of an external action.
- Producer fence: async Hub tools can wait for the bounded scope decision; source revision is checked again afterward. Permission checks for a Subagent need an async pre-tool lease gate while retaining existing synchronous Bash/MCP/Approval target guards. No broader permission is granted.

## Verification

Start with red regressions for a local new_request hint on a true continuation, explicit independent new topic preserving existing Task intent, a missing resolver keeping old Approval authority blocked, selected T1 vs unaffected T2, old action waiting across resolution, and text-only restart with pending scope. Preserve all previous source sequencing, retention, cancellation, provider-limit and #35 silent-control tests. Use only fakes for conversational providers and devices. Full pytest/Pyright remain required before commit.
