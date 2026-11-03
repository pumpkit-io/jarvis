# Subagents call only read-only connectors

A Subagent runs with `bypassPermissions` and loads the User's and the repo's settings, because it needs the sdlc skills and the repo's own instructions. Loading settings also attaches the claude.ai connectors of the User's account and the MCP servers of the User's plugins: on 2026-11-03 that was Claude Docs, DigitalOcean, PostHog and VoiceMode, and on other days Pumpkit, whose `post_now` publishes on X at once. Under `bypassPermissions` nothing would stand between a Subagent and those tools but the written Boundary (ADR 0001).

So a Subagent session carries a `PreToolUse` hook on every `mcp__` tool, with `on_failure="block"`, that allows only the tools on `READ_CONNECTOR_TOOLS` in `src/jarvis_hub/subagents.py` (Pumpkit's reads and Claude Docs' reads today) and denies the rest, saying that the call needs an Approval. Hooks run under `bypassPermissions`: the opt-in real-model test sees the hook's deny reach the model. The VoiceMode servers are also left out of the session's tools altogether (`disallowed_tools`), because the Hub owns the microphone and speaker.

When an Approval covers an action a Subagent carries out through a connector, such as an X post through Pumpkit (ADR 0004, ticket #15), the Hub widens that one Subagent's allowlist by the tools the Approval names, and no further.

## Considered Options

- Deny the tools that can post, publish, spend or change production, and allow the rest. Rejected: a new connector or a new tool on an old one would be allowed until someone noticed, and PostHog's single `exec` tool reads and changes configuration alike.
- No MCP servers at all (`strict_mcp_config=True`, as the Talker and the Orchestrator run). Rejected: a Subagent investigating a post or a doc loses reads it can safely use, and the Approval path would need the servers wired back by hand.

## Consequences

- A connector tool that only reads is unavailable to Subagents until someone adds it to `READ_CONNECTOR_TOOLS` with a table case in `tests/test_subagent_rules.py`.
- Bash is not covered: a Subagent can still run `gh`, `curl` or `ssh`, and only the Boundary stops it there, as ADR 0001 says.
