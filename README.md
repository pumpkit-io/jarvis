# jarvis

A voice-first orchestrator agent for the repos of one GitHub organization. You talk to Jarvis, it keeps an eye on your repos, helps you decide, and hands practical work to subagents that open PRs. It runs in [Claude Code](https://claude.com/claude-code) with [VoiceMode](https://github.com/mbailey/voicemode).

## Requirements

- Claude Code, with the VoiceMode plugin and its local speech services running (Kokoro voices `am_liam` and `im_nicola`).
- The `gh` CLI, signed in to an account that can read your organization.
- The [sdlc](https://github.com/pumpkit-io/sdlc) skills, which Jarvis's subagents use.
- Your organization's repos cloned next to this one, in the same parent folder. Jarvis clones missing ones there.

## Setup

```bash
cp .env.example .env    # set JARVIS_USER_NAME and JARVIS_GITHUB_ORG
ln -s "$PWD/jarvis" ~/.local/bin/jarvis   # optional: start Jarvis from any terminal
```

Then rewrite the Repo catalog in `AGENTS.md` for your organization.

## Run

```bash
./jarvis
```

It starts Claude Code with Opus in voice mode, so you can talk right away. Extra arguments go to `claude`, for example `./jarvis --continue`.

Jarvis runs with `--dangerously-skip-permissions`. The only guardrail is the Boundary section of `AGENTS.md`: work goes as far as opening a PR, and merging, deploying, publishing or deleting waits for your yes. Read [ADR 0001](docs/adr/0001-skip-permissions-with-written-boundaries.md) before you change that section.

## Files

| File | What it is |
| --- | --- |
| `AGENTS.md` | Jarvis's instructions, loaded every session (`CLAUDE.md` imports it) |
| `docs/working-preferences.md` | How you want work done; Jarvis hands it to every subagent |
| `scripts/session-start.sh` | SessionStart hook: your name, the organization, its live repos and open PRs |
| `GLOSSARY.md` | The terms the instructions use |

## Test

```bash
bash tests/session-start.test.sh
```
