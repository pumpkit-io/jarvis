# Recorded Approvals for Boundary actions

Once the Talker, the Orchestrator and Subagents were split (ADR 0002), the User's yes no longer reached the agent that acts on it. A Haiku Talker heard it, paraphrased it to the Orchestrator, and a Subagent acted on the paraphrase, so "yeah, looks fine" could turn into "the User approved the merge". We made the yes a recorded Approval instead.

- The Orchestrator asks for one exact action ("merge pumpkit#42") through a report that needs the User. The Hub turns that report into a pending Approval tied to the state it describes, such as the PR's head commit.
- The Talker may call `approve` only on the turn right after the User speaks or types, and only when exactly one Approval is pending and it was the last thing Jarvis said. Otherwise it names the pending Approvals and asks which one the User means. The Hub stores the User's words verbatim with the Approval.
- An Approval is single-use and expires after 24 hours. It lapses if its target changes first, and Jarvis asks again, saying what changed.
- The Hub carries out the actions it can run without judgment, such as merging a PR or deleting a branch, after checking the Approval. No model takes part. Actions that need judgment, such as a deploy following the pumpkit-ops runbook or an X post through Pumpkit, go to a Subagent whose brief carries the Approval.

## Considered Options

- Pass the yes along as free text. Rejected: every hop is a chance to paraphrase a yes into existence.
- Have a Subagent carry out every approved action. Rejected for the actions the Hub can run itself: a Subagent that skips permissions can act without an Approval, so only the Hub gives a hard guarantee.

## Consequences

For actions carried out by a Subagent, the Approval is still a written rule, as safe as ADR 0001 makes it and no safer. Any action the Hub learns to run itself should move out of the Subagent path.
