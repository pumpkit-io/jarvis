# Jarvis development

This repo implements Jarvis, a Python voice assistant for one GitHub organization. These instructions are for agents developing the harness. Runtime character and conversation rules live in `prompts/`.

Read `docs/working-preferences.md` before scoping or implementing work. Use `GLOSSARY.md` for domain terms and `docs/agents/domain.md` when changing the domain model or an ADR. Issues live in GitHub Issues; read `docs/agents/issue-tracker.md` before working with them.

## Architecture

`src/jarvis_hub/main.py` wires the Hub to terminal and audio adapters. The Hub owns Tasks, events, Approvals, persistence and the agent sessions. `voice/state.py` is the pure voice state machine; audio engines and playback sit behind adapters. `agents/backend.py` defines the backend protocol; `agents/sdk.py` uses the Claude Agent SDK and the installed `claude` CLI.

The Talker holds the conversation, the read-only Orchestrator plans Tasks, and Subagents do repo work in isolated worktrees. Their prompts are `prompts/talker.md`, `prompts/orchestrator.md` and `prompts/subagent.md`. The shared Boundary is `prompts/boundary.md`; read it before taking an action on a repo or external service. `prompts/working-rules.md` and `prompts/repo-catalog.md` complete the runtime instructions. Edit each rule at its source.

## Running and testing

Use `./jarvis --text` for a terminal conversation without audio. `./jarvis` starts voice mode; `--headphones` disables echo cancellation and `--debug` displays voice transitions. The launcher follows symlinks and runs from its own checkout. Configuration is parsed from `.env`; `.env.example` lists the settings.

Run `uv run pytest -q` and `uv run pyright` before reporting a change complete. Tests inject fake agents, clock and audio through the Hub seam and never spend model quota. Real-model and real-GitHub tests are opt-in; leave them off unless explicitly authorized. Runtime state and logs live under the gitignored `.jarvis/` folder. See `docs/notes/phase-0-spikes.md` for measurements and manual voice checks.

The Talker warms in the background at launch (ADR 0006). That hidden turn spends subscription usage; idle listening and stop phrases need no model call. Preserve `strict_mcp_config` and the Hub's tool guards when changing the SDK adapter; see ADRs 0007, 0008 and 0013.

## Skills

Load `code-comments` before writing comments. Use the sdlc skills that fit the work; keep runtime prompts separate from developer instructions.

### Worktrees

Never edit files or commit in the main checkout, even for a one-line change. Before your first file change or commit, load the `worktrees` skill and resolve a worktree for the work, then work and commit there. Release its lock when the work is committed. The one exception is a main-checkout path a skill names, such as `improve-codebase-architecture`'s notes.
