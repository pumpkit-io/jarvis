# The Hub decides an Approval from its own record, and runs merges and branch deletes itself

ADR 0004 made the User's yes a recorded Approval. This records how the Hub decides that a yes counts, how it carries out what it can, and how far a Subagent's tools widen for the rest. The code is `src/jarvis_hub/approvals.py`.

## Asking

The Orchestrator asks through `report(..., approval={...})`, naming one action from a closed set: `merge_pr` (repo, PR number, head commit), `delete_branch` (repo, branch, tip commit) or `subagent_action` (a description of the one exact action, and `grants` naming connector tools from a closed list). Before the User hears anything, the Hub reads the target with gh and refuses a commit that is not current, so the User is asked about the PR as it is. The screen shows the exact action in the Hub's words, and the event carries `approval="A1"`. A newer request on the same Task withdraws the one pending, and cancelling the Task withdraws them all. The request is news that needs the User, so it gets through Quiet mode once per change (ADR 0012).

## When a yes counts

The Talker's `approve(approval_id)` takes only the id. The Hub accepts it when:

1. the Talker turn answers a `<user>` message, spoken or typed;
2. the last turn Jarvis finished asked about that Approval: its news carried it, or the Talker named it after a refused `approve`. A turn cut short asked about nothing, since the User may not have heard it;
3. the yes is unambiguous: exactly one Approval is pending and it alone was asked about, or the User's words say more than a bare yes ("yes", "ok, go ahead", "sì, procedi"), which then chose among those Jarvis named;
4. no other Approval was approved in the same turn.

Otherwise the result lists every pending Approval, and the Talker names them and asks which. The words stored with the Approval are the `<user>` line from the Hub's own record of that turn, never anything in the tool call. The record of what was last asked is not saved: after a restart the first yes is refused and the Talker names what is pending, at the cost of one more question.

An Approval is single-use and expires 24 hours after it was asked, on the Hub's clock. Right before acting, the Hub reads the target again: a PR that is no longer open, or whose head moved, and a branch that is gone or moved, lapse the Approval. Nothing runs, and the Orchestrator gets an event saying what changed so it asks again.

## What the Hub runs

Merges and branch deletes run with a fixed argv, no shell, behind ADR 0007's `env -i` (gh signs in from its own config and the keyring, as for reads), and each run is logged to the metrics with the argv, exit code, output and the User's words:

- Merge: `gh pr merge <n> -R <org>/<repo> --merge --match-head-commit <sha>`. The Org's repos merge with a merge commit, and GitHub refuses the merge if the head is no longer that commit, so the re-read is backed by GitHub itself. A repo set to delete head branches on merge still does so: that is its own setting, not the Hub's act.
- Branch delete: GraphQL `updateRefs` with `beforeOid` set to the approved tip and a zero `afterOid`, which GitHub applies only while the ref still points there. The repository's node id comes from the same read that checks the tip. `gh api -X DELETE` was rejected: it deletes whatever the branch points at by then.

A merge GitHub refuses still spends the Approval; the event says why. An approved action a restart cut off runs again on the next launch, re-read first, so one that already happened lapses rather than runs twice. The Orchestrator's read-only guard is unchanged: it still cannot merge or delete.

## What a Subagent gets

On a yes to a `subagent_action`, the Orchestrator is told and starts a Subagent with `approval_id`. The Hub hands that Approval to that one Subagent: its brief ends with an `<approval>` block holding the id, the exact action and the User's words, and its connector check (ADR 0008) also allows the tools of the Approval's grants. The first such call spends the Approval, after which the check denies them again; it denies them too once the Approval expires, and the Approval is spent when the Subagent ends, whatever it did. The grants are a closed list: `x_post` opens Pumpkit's `post_now` and `schedule_post` under both its server names, `posthog_config` opens PostHog's `exec`. A deploy or release needs no connector, so only the written Boundary holds its Subagent to the one action, as ADR 0004 says.

## Considered Options

- Accept a yes when exactly one Approval is pending, and otherwise never. Rejected: with two pending, nothing could be approved until one expired. A yes that names the action, after Jarvis named them all, chooses safely.
- Let the Talker pass the User's words to `approve`. Rejected: the model could write words the User never said.
- Keep a Subagent's widened tools open until it ends. Rejected: one X post approved would allow several.
