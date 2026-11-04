# Jarvis

Jarvis is a local voice Hub for the repos of one GitHub organization. A Talker holds the conversation, a read-only Orchestrator plans Tasks, and Subagents work in isolated worktrees. The Hub owns audio, waiting and recorded Approvals. The VoiceMode plugin is no longer needed.

Voice engines are provisional until the User's recording session completes. The implementation has fake-adapter tests; the [verification checklist](docs/notes/voice-hub-verification.md) names the physical measurements still required.

## Requirements

- macOS on Apple Silicon. The project uses MLX and Python 3.12; `uv` selects Python from the project's version requirement.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git on `PATH`.
- The Claude Code `claude` CLI signed in with a Max subscription. Run `claude` once and complete sign-in before launching the Hub. Model calls use the installed CLI through the Claude Agent SDK, not a separate API account (see [ADR 0002](docs/adr/0002-hub-with-split-talker-and-orchestrator.md)).
- No `ANTHROPIC_API_KEY` in the Hub's shell. It removes API keys, alternate authentication tokens, base URLs and cloud-provider billing switches with a warning. Unset those in your shell profile; `.env` is for `JARVIS_*` settings.
- [GitHub CLI](https://cli.github.com/), signed in using `gh auth login`, with access to the Org's repos. Git authentication must also work for fetching and pushing.
- The [sdlc skills](https://github.com/pumpkit-io/sdlc), installed for Claude Code. Writing Subagents need its `worktree.sh`; the Hub looks in the installed plugin, then in a sibling `sdlc` clone.
- Microphone permission for the terminal app under macOS System Settings → Privacy & Security → Microphone, and an available audio output. Text mode needs neither permission nor an audio device.
- Network access for dependency installation and the first voice-model downloads. Local voice mode downloads Kokoro (`mlx-community/Kokoro-82M-bf16`), Parakeet (`mlx-community/parakeet-tdt-0.6b-v3`), Smart Turn (`pipecat-ai/smart-turn-v3`, `smart-turn-v3.2-cpu.onnx`) and openWakeWord's `hey_jarvis` models. First-time downloads can exceed the startup target; later launches reuse their caches.

## Setup

Clone this repo inside the folder that holds your Org's repos. The parent of the main checkout is the Org folder. Existing clones must have an `origin` pointing to the matching GitHub Org repo; Jarvis clones missing repos there when needed.

From this checkout:

```bash
uv sync --locked
claude plugin marketplace add pumpkit-io/sdlc
claude plugin install sdlc@pumpkit-io --scope user
cp .env.example .env
```

Set `JARVIS_USER_NAME` and `JARVIS_GITHUB_ORG` in `.env`. Edit `prompts/repo-catalog.md` to describe your Org's repos, and `docs/working-preferences.md` to describe how you want work done. Read [the Boundary](prompts/boundary.md) before giving Jarvis work.

Start `./jarvis --text` to check CLI sign-in and the terminal conversation. Then start `./jarvis` in a terminal, grant microphone permission if prompted and allow the voice models to download. Wait for the listening notice before speaking.

Optionally, with this checkout as the current directory and `~/.local/bin` on `PATH`:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/jarvis" ~/.local/bin/jarvis
```

The launcher follows symlinks before entering its checkout. In a git worktree, configuration still comes from the main checkout's `.env`; each worktree keeps its own `.jarvis/` state and logs.

## Run

```bash
./jarvis                         # voice mode, idle until the Wake word
./jarvis --text                  # terminal conversation with no audio
./jarvis --headphones            # voice mode with echo cancellation off
./jarvis --debug                 # voice state transitions on screen
./jarvis --text --speak          # typed input with spoken replies
./jarvis stats --since 24h
./jarvis stats --bargein --since 24h
```

Say “Hey Jarvis” and your request, in one breath if you wish. “Hi Jarvis”, “Ehi Jarvis” and “Jarvis” are intended wake phrases; recall for the User's voice is still awaiting measurement. After a reply, the Follow-up window lets you continue without the Wake word. A whole stop phrase such as “that's all”, “stand by” or “basta così” plays the cached acknowledgment and returns to idle. A longer utterance containing those words remains a request.

Type into the same terminal during voice mode for a text-only reply. Details appear on screen and in `.jarvis/show.log`. Ctrl-D or Ctrl-C stops the Hub. Each launch starts a fresh conversation; saved Tasks and waits survive restart, and interrupted Subagents keep their branches.

The Talker warms in the background at startup, using one hidden subscription turn ([ADR 0006](docs/adr/0006-warm-up-in-the-background.md)). The Orchestrator connects without a prompt. Idle listening and stop phrases use no model call; pending work and Announcements may still need agent turns.

Ask for status or to cancel a Task by name. Quiet mode holds routine Announcements until the next Wake word; news that needs you can still be announced. A Boundary action asks for one recorded Approval. The Hub runs approved merges and branch deletions; other approved actions go to a Subagent. See [ADR 0013](docs/adr/0013-approvals-checked-by-the-hub.md) for the rules and limits.

## Settings

[`.env.example`](.env.example) is the complete setting reference, with defaults and units beside every key. The Hub parses `.env` as text, never as shell code; nonempty `JARVIS_*` process variables override the file. Restart the Hub after editing settings.

- Identity: `JARVIS_USER_NAME` and `JARVIS_GITHUB_ORG`.
- Agents: `JARVIS_TALKER_MODEL`, `JARVIS_ORCHESTRATOR_MODEL`, `JARVIS_ORCHESTRATOR_EFFORT`, `JARVIS_MAX_EFFORT`, `JARVIS_MAX_SUBAGENTS` and `JARVIS_TALKER_RESTART_AT`. Talker effort stays low with thinking off; Sonnet is its default after the latency tests (ADR 0005). Subagent `xhigh` or `max` also requires the User's explicit request.
- Listening windows: `JARVIS_FOLLOWUP_SECONDS`, `JARVIS_ANNOUNCE_FOLLOWUP_SECONDS`, `JARVIS_LISTEN_SECONDS`, `JARVIS_END_SILENCE_MS`, `JARVIS_END_EXTEND_MS`, `JARVIS_PREROLL_MS`, `JARVIS_ONSET_PAD_MS` and `JARVIS_MAX_TURN_SECONDS`.
- Engines: `JARVIS_WAKE_ENGINE=openwakeword`, `JARVIS_STT_ENGINE=parakeet`, `JARVIS_TTS_ENGINE=kokoro` and `JARVIS_AEC=webrtc` or `off`. These are the currently supported choices; Porcupine and Whisper are not runtime alternatives yet. `--headphones` overrides echo cancellation to off.
- Detection: `JARVIS_WAKE_THRESHOLD`, `JARVIS_VAD_THRESHOLD`, `JARVIS_SMART_TURN_THRESHOLD`, `JARVIS_CHIME_GUARD_MS` and `JARVIS_WAKE_REFRACTORY_MS`. Lower wake thresholds trade fewer missed wakes for more false wakes.
- Barge-in: `JARVIS_BARGEIN_MIN_SPEECH_MS`, `JARVIS_BARGEIN_DECIDE_MS` and `JARVIS_BARGEIN_GUARD_MS` control pause and decision timing.
- Audio clips: `JARVIS_AUDIO_CLIPS=on` opts in to WAVs and JSON sidecars in `.jarvis/clips/`. `JARVIS_CLIPS_MAX_COUNT`, `JARVIS_CLIPS_MAX_DAYS` and `JARVIS_CLIPS_MAX_MB` prune the oldest clips while saving is enabled. Saving is off by default; turning it off leaves existing clips in place. Logs contain transcripts even when clips are off. There is no voice-speed setting; speed stays at 1.0.

## Troubleshooting

- `claude` missing or authentication fails: put the installed CLI on `PATH`, run `claude` to sign in with Max, then retry `./jarvis --text`. A billing warning means the Hub removed an inherited billing switch; remove it from your shell profile too.
- Configuration error: check the named key against `.env.example`, including units and supported choices. Text mode ignores voice settings with a warning. Stats also runs without loading voice models.
- No microphone or audio: check macOS permission for the terminal, the selected devices and the Hub's listening notice. Use `--text` to continue without audio. With headphones, use `--headphones`.
- Voice loading takes too long: first downloads need network access. Check the model-loading error shown in the terminal. For a measured run, finish downloads first and stop other GPU-heavy processes; see the recording instructions in the spike notes.
- Missed “Ehi Jarvis” or bare “Jarvis”: these are known weak spots in synthetic testing. Record the User's takes before selecting a different engine. Do not treat the unmeasured recall target as achieved.
- Jarvis pauses or stops on its own speaker: try headphones and record the speaker checklist before tuning thresholds. Echo cancellation and Barge-in still need the real speaker run.
- A writing Subagent cannot start: check that the sdlc plugin is installed and its worktree script is available, that the clone belongs to the configured Org, and that its worktree is not locked by another session.
- Usage limit: the Hub reports the reset time when available and queues new Subagents. Stats reports observed limit hits and token usage; these are not API charges. Real limit handling is tested with fakes, not an exhausted account.
- Unexpected restart recovery: inspect `.jarvis/state.json` and terminal notices before changing saved data. `.jarvis/metrics.jsonl`, `.jarvis/show.log` and `.jarvis/subagents/` hold timings, shown details and Subagent progress. They may contain private work; keep them local.

## Wake-word license

The default `hey_jarvis` pretrained model comes from [openWakeWord](https://github.com/dscripka/openWakeWord#license). Its pretrained models use **CC BY-NC-SA 4.0**, including the noncommercial restriction; the project's code uses Apache 2.0. See the [model license](https://creativecommons.org/licenses/by-nc-sa/4.0/) for attribution, sharing and use conditions. A commercial wake-word model or another engine needs its own evaluation and integration; changing an environment variable does not install one.

## Development and verification

```bash
uv run pytest -q
uv run pyright
```

Default tests use fake agents, clock and audio and do not spend subscription quota. Real-model and real-GitHub tests are opt-in. [AGENTS.md](AGENTS.md), also loaded by `CLAUDE.md`, contains developer instructions; runtime prompts live in `prompts/`. Domain terms are in [GLOSSARY.md](GLOSSARY.md).

[Spike notes](docs/notes/phase-0-spikes.md) record versions, measurements and accepted limits. [The final verification checklist](docs/notes/voice-hub-verification.md) covers the recording session, model comparison and remaining release gates. The automated suite alone does not establish wake recall, speaker behavior or real-world latency.
