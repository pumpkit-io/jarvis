# jarvis

A voice-first orchestrator agent for the repos of one GitHub organization. You talk to Jarvis, it keeps an eye on your repos, helps you decide, and hands practical work to subagents that open PRs. The Python Hub owns audio and runs its agents through the Claude Agent SDK and the installed Claude Code CLI. The VoiceMode plugin is no longer needed.

## Requirements

- Apple Silicon, `uv`, and the Claude Code CLI signed in with a Max subscription. Voice mode uses local audio engines and Kokoro voices `am_liam` and `im_nicola`.
- The `gh` CLI, signed in to an account that can read your organization.
- The [sdlc](https://github.com/pumpkit-io/sdlc) skills, which Jarvis's subagents use.
- Your organization's repos cloned next to this one, in the same parent folder. Jarvis clones missing ones there.

## Setup

```bash
cp .env.example .env    # set JARVIS_USER_NAME and JARVIS_GITHUB_ORG
ln -s "$PWD/jarvis" ~/.local/bin/jarvis   # optional: start Jarvis from any terminal
```

Then rewrite the Repo catalog in `prompts/repo-catalog.md` for your organization.

## Run

```bash
./jarvis
```

Voice mode listens for the Wake word and returns to idle after the Follow-up window. Type into the same terminal for a text-only reply. Ctrl-D or Ctrl-C stops the Hub.

```bash
./jarvis --text                   # no audio
./jarvis --headphones             # echo cancellation off
./jarvis --debug                  # voice state transitions
./jarvis stats
./jarvis stats --bargein
```

Arguments go to `jarvis-hub`. The launcher resolves symlinks before entering its checkout. The Talker warms in the background at startup; this uses one hidden subscription turn (ADR 0006). Audio engines warm locally. Measurements and pending manual voice checks are recorded in [the spike notes](docs/notes/phase-0-spikes.md).

The Hub checks Approvals and carries out merges and branch deletions. Other Boundary actions go to Subagents with a recorded Approval. Read [the Boundary](prompts/boundary.md) and [ADR 0013](docs/adr/0013-approvals-checked-by-the-hub.md) before changing those checks.

## Files

| File | What it is |
| --- | --- |
| `AGENTS.md` | Developer instructions (`CLAUDE.md` imports it); runtime prompts live in `prompts/` |
| `docs/working-preferences.md` | How you want work done; Jarvis hands it to every subagent |
| `GLOSSARY.md` | The terms the instructions use |
