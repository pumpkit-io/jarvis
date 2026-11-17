# Sdlc skills start a Subagent's brief, in set-up repos only

The sdlc plugin's skills carry the User's way of doing each stage of work, but a Subagent could not reach them reliably. On 2026-11-17 a test showed that a brief whose first line is `/sdlc:grill-with-docs <request>` injects that skill's instructions into the Subagent's session with no tool call. Naming a skill in prose loads nothing, and a Subagent cannot start a skill marked `disable-model-invocation` (every command skill) itself.

So `prompts/orchestrator.md` tells the Orchestrator to start a brief with `/sdlc:<skill> <request>` when the work matches a command skill, and gives the mapping from the kind of work to the skill. Only the first slash command of a brief loads, so each brief carries one skill. The skill's own steps still sit under the Boundary: pushing and opening a PR are fine, merging, deleting and deploying need an Approval.

The User decided two limits:

- **Set-up repos only.** The Orchestrator reads the repo's folder for a `## Agent skills` line in `AGENTS.md` or `CLAUDE.md`, or `docs/agents/issue-tracker.md` or `docs/agents/domain.md`, the same test as `check-sdlc`'s `setup.present`. A repo that fails it, or that the Orchestrator cannot read, is not set up, and its Subagents work without a slash command. Setting a repo up is the one exception, since `setup-sdlc` is the request itself.
- **`grill-with-docs` only on request.** It puts many questions to the User and takes long, so the Orchestrator uses it only when the User names it or says to "grill" an idea. A vague idea is not a reason. The word "grill" stays out of other briefs because it loads the `grilling` skill.

## Considered Options

- Name the skill in prose and rely on the Subagent to load it. Rejected: prose loads nothing, and command skills cannot be self-invoked.
- Hardcode the list of set-up repos in the prompt. Rejected: the list goes stale as repos are set up, and the check is two reads.
- Use the skills in every repo. Rejected by the User: a repo without the harness lacks the tracker, domain and worktree docs the skills read.

## Consequences

- The Subagent's log keeps no injected skill text, so the Orchestrator judges from the report whether the skill loaded.
- A repo set up after the Orchestrator's last look is picked up on the next brief, since the check runs for each one.
- An auto skill cannot be started by a brief's first line the way a command skill is, so the brief names it and tells the Subagent to load it with the Skill tool.
