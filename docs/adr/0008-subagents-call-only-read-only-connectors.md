# Subagents call only read-only connectors

A Subagent runs with `bypassPermissions` and loads the User's and the repo's settings, because it needs the sdlc skills and the repo's own instructions. Loading settings also attaches the claude.ai connectors of the User's account and the MCP servers of the User's plugins: on 2026-11-03 that was Claude Docs, DigitalOcean, PostHog and VoiceMode, and on other days Pumpkit, whose `post_now` publishes on X at once. Under `bypassPermissions` nothing would stand between a Subagent and those tools but the written Boundary (ADR 0001).

So a Subagent session carries a `PreToolUse` hook on every `mcp__` tool, with `on_failure="block"`, that allows only the tools on `READ_CONNECTOR_TOOLS` in `src/jarvis_hub/subagent_rules.py` (Pumpkit's reads and Claude Docs' reads today) and denies the rest, saying that the call needs an Approval. Hooks run under `bypassPermissions`: the opt-in real-model test sees the hook's deny reach the model. The VoiceMode servers are also left out of the session's tools altogether (`disallowed_tools`), because the Hub owns the microphone and speaker.

Pumpkit reaches a session under two server names, so its reads are listed once (`PUMPKIT_READS`) and allowed under both. `claude_ai_Pumpkit` is the claude.ai connector of the User's account; `pumpkit` is an MCP server in the User's own Claude Code config, today a local Pumpkit Instance at `localhost:5173`. Both expose the same tools, and a Subagent gets whichever is attached that day.

The hook is an SDK callback the Hub registers when it opens the session, not a hook from a settings file, so a repo cannot turn it off. A real-model check on 2026-11-03 put `"disableAllHooks": true` in a scratch repo's `.claude/settings.json`, and then in its `.claude/settings.local.json`, and the hook still denied the call each time; `tests/test_real_model.py` (opt-in) repeats that check.

When an Approval covers an action a Subagent carries out through a connector, such as an X post through Pumpkit (ADR 0004), the Hub widens that one Subagent's check by the tools the Approval's grants name, for one call while the Approval is unexpired (ADR 0013). Every other connector tool off the list stays denied.

## Considered Options

- Deny the tools that can post, publish, spend or change production, and allow the rest. Rejected: a new connector or a new tool on an old one would be allowed until someone noticed, and PostHog's single `exec` tool reads and changes configuration alike.
- No MCP servers at all (`strict_mcp_config=True`, as the Talker and the Orchestrator run). Rejected: a Subagent investigating a post or a doc loses reads it can safely use, and the Approval path would need the servers wired back by hand.

## Consequences

- A connector tool that only reads is unavailable to Subagents until someone adds it to `READ_CONNECTOR_TOOLS` with a table case in `tests/test_subagent_rules.py`.
- Bash is not covered: a Subagent can still run `gh`, `curl` or `ssh`, and only the Boundary stops it there, as ADR 0001 says.
