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
- Network access for dependency installation and the first voice-model downloads. Local voice mode downloads Qwen3-TTS (`mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit`, or Kokoro's `mlx-community/Kokoro-82M-bf16` with `JARVIS_TTS_ENGINE=kokoro`), Parakeet (`mlx-community/parakeet-tdt-0.6b-v3`), Smart Turn (`pipecat-ai/smart-turn-v3`, `smart-turn-v3.2-cpu.onnx`) and openWakeWord's `hey_jarvis` models. First-time downloads can exceed the startup target; later launches reuse their caches.

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
./jarvis stats --voice --since 24h
```

Say “Hey Jarvis” and your request, in one breath if you wish. “Hi Jarvis”, “Ehi Jarvis” and “Jarvis” are intended wake phrases. When the Wake word model misses one, a short idle utterance that opens with “Jarvis” (as transcribed, however misheard) wakes Jarvis too, a moment after you stop speaking. After a reply, the Follow-up window lets you continue without the Wake word for 10 seconds; a short falling sound then tells you Jarvis is back to waiting for it. A whole end control such as “that's all”, “stand by” or “basta così” returns to idle silently. A longer utterance containing those words remains a request. When a conversational reply is appropriate, Jarvis generates its farewell and finishes speaking before returning idle.

Replies start with generated content about the request; slow generation stays silent without a holding sentence. Background work is acknowledged before it starts.

With `--text --speak`, spoken news waits for current speech to finish and batches waiting updates into one Announcement. This keeps unheard updates together when Focus moves to another Client. News still speaks after terminal input ends. User input interrupts an Announcement and preserves its unheard news.

A phone with unfinished playback does not keep the terminal session open. Undelivered remote news stays saved for the next launch.

The [voice trace](docs/notes/voice-tracing.md) correlates captured requests, endpointing, transcription, Hub turns and estimated meaningful playback. It distinguishes missing measurements from zero delay.

Type into the same terminal during voice mode for a text-only reply. Details appear on screen and in `.jarvis/show.log`. Ctrl-D or Ctrl-C stops the Hub. Jarvis continues the same Conversation after a restart, without opening on news ([ADR 0028](docs/adr/0028-one-conversation-across-restarts-folded-into-memory.md)); saved Tasks and waits survive restart, and interrupted Subagents keep their branches.

Startup shows loading and ready notices for the conversation model, speech and listening. The short readiness tone means speech output is ready; check the model and listening notices before speaking. There is no prerecorded greeting.

The Talker reads the saved Conversation in the background at startup, using one hidden subscription turn ([ADR 0006](docs/adr/0006-warm-up-in-the-background.md)). The Orchestrator connects without a prompt. Idle listening and stop phrases use no model call; pending work and Announcements may still need agent turns.

Ask for status or to cancel a Task by name. Quiet mode holds routine Announcements until the next Wake word; news that needs you can still be announced. A direct action request grants permission for that action. You can also authorize a Task unattended, or explicitly authorize the whole session. Task permission survives restarts; session permission ends at restart. Each Boundary action still gets a concrete Approval, using existing permission without another question when it applies. Approval replies use natural language. The Hub runs approved merges and branch deletions; other approved actions go to a Subagent. See [ADR 0022](docs/adr/0022-natural-language-authorization.md) for the permission model and its limits.

## Remote access

The optional door stays off with `JARVIS_REMOTE=off` (the default). Set `JARVIS_REMOTE=on` and `JARVIS_REMOTE_PORT=8765` in `.env` to listen on `127.0.0.1`. Only that loopback address is accepted. Both text and voice mode own the listener; ending terminal input stops it and the Hub.

With the door enabled, ordinary `./jarvis --text` accepts spoken phone requests and returns sentence audio to that phone. It lazily loads the configured speech engine and one Parakeet service without opening a Mac microphone or speaker. Terminal typing stays text-only unless `--speak` is selected. Phone playback reports determine what was heard; reconnecting restores text history without replaying audio.

`GET /v1/health` returns protocol `version: 1`, model readiness, the Mac name and the Usage limit. It requires a device token under [protocol v1](docs/protocol/client-v1.md), following Spec #91's every-request authentication rule over #101's health exception. Unpaired and revoked tokens receive HTTP 401. The running Hub looks up each token in the durable device store on every request; CLI revocation takes effect on the next HTTP call. The health response exposes no paths, configuration or credentials.

Inspect the current Serve configuration before changing it:

```sh
tailscale serve status
```

Preserve existing services. On the implementation Mac, HTTPS 443 already serves an unrelated service on loopback port 3000. Do not replace it. Choose an unused HTTPS port after checking status; for example, if 8443 is free:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:8765
tailscale serve status
```

If HTTPS 443 is unused, `tailscale serve --bg --https=443 http://127.0.0.1:8765` uses the default HTTPS origin. These commands are setup instructions, not actions already performed. Do not reset Serve, overwrite an occupied route, or use Funnel. The installed CLI's `serve --help` documents `--bg` and `--https`.

Set `JARVIS_REMOTE_ORIGIN` to the exact HTTPS origin printed for Jarvis, including a nondefault port, for example `https://mac.example.ts.net:8443`. Use a root origin: v1 Clients do not support a non-root path prefix. Then run `./jarvis pair` in another terminal. It prints a six-digit code and a terminal QR containing protocol version, origin and code. `./jarvis pair --origin https://mac.example.ts.net:8443` overrides the configured origin for that code. Pairing never guesses the address from an HTTP Host header.

The code expires after five minutes and works once; creating another invalidates the previous code. Pairing requests need the running remote-enabled Hub, although the CLI can create the code while it is stopped. Five failed exchanges in five minutes lock pairing for five minutes. The lockout survives a Hub restart and creating a new code. A lost successful response needs a new code; the token is never returned again.

`./jarvis clients` lists active device names, kinds and paired times in UTC. `./jarvis revoke "My iPhone"` revokes that exact, case-sensitive name. Active names are unique: pairing another device with the same friendly name assigns a suffix such as `My iPhone (2)`. The original token keeps working, including after an app resets its local token and pairs again. Use the assigned name from `./jarvis clients` when revoking. Pairing never silently replaces another device. Pairing again gets a new Client identity and token; a revoked name can be reused. These commands run without starting audio or model sessions or requiring the Claude CLI. The shared SQLite store under `.jarvis/events.sqlite3` keeps token and code hashes, never their raw values.

Serve limits access to the tailnet and manages HTTPS; see the [Tailscale Serve instructions](https://tailscale.com/docs/features/tailscale-serve) and [command reference](https://tailscale.com/docs/reference/tailscale-cli/serve). Never use `tailscale funnel`: it publishes to the public internet. Do not forward the port through a router or bind the Hub to a LAN or tailnet address.

Waiting on the User, these account and device steps have not been performed or verified by the automated loopback tests:

- [ ] Install/sign in to Tailscale on the Mac and iPhone, in the same tailnet.
- [ ] Enable HTTPS for the tailnet through the Tailscale admin console or Serve's interactive setup.
- [ ] Restrict the tailnet ACL to the User's own devices reaching this Mac's Serve HTTPS port; remove broader rules that grant the same access.
- [ ] Run Serve, verify its HTTPS origin from the iPhone and verify that another unauthorized tailnet device cannot reach it.
- [ ] Scan the terminal QR on the physical iPhone, then check that authenticated health works and unpaired/revoked tokens are refused over HTTPS and cellular.

The [iPhone setup and real-device checklist](docs/notes/iphone-real-device-verification.md) records every physical check as Waiting on User until it is observed. Build and install with [the iPhone instructions](ios/README.md#install-on-your-iphone).

This slice requires a terminal running `./jarvis` on a Mac that stays awake and on power. It has no background service or keep-awake mechanism. The phone speaks news only while the app is open or an existing reply is still playing; opening the app does not take Focus. After talking to the Mac and leaving, Focus stays on the Mac until an input or explicit Focus request moves it, so news can still speak to the room. A free Personal Team app expires every 7 days and needs rebuilding from the Mac. There are no push notifications, always-on listening, Wake word on the phone, or fresh Talks while disconnected. Music coexistence remains an experiment for the next slice.

## Settings

[`.env.example`](.env.example) is the complete setting reference, with defaults and units beside every key. The Hub parses `.env` as text, never as shell code; nonempty `JARVIS_*` process variables override the file. Restart the Hub after editing settings.

- Identity: `JARVIS_USER_NAME` and `JARVIS_GITHUB_ORG`.
- Agents: `JARVIS_TALKER_MODEL`, `JARVIS_ORCHESTRATOR_MODEL`, `JARVIS_ORCHESTRATOR_EFFORT`, `JARVIS_MAX_EFFORT` and `JARVIS_MAX_SUBAGENTS`. Talker effort stays low with thinking off; Sonnet is its default after the latency tests (ADR 0005). Subagent `xhigh` or `max` also requires the User's explicit request.
- Memory, in tokens: `JARVIS_MEMORY_FOLD_AT`, `JARVIS_MEMORY_TAIL` and `JARVIS_MEMORY_CEILING`, with `JARVIS_MEMORY_MODEL` and `JARVIS_MEMORY_EFFORT` for the Summariser, which writes the Memory a Fold swaps in. The Tail may be at most half of the trigger ([ADR 0028](docs/adr/0028-one-conversation-across-restarts-folded-into-memory.md)). Summaries are kept under `.jarvis/memory/`.
- Listening windows: `JARVIS_FOLLOWUP_SECONDS`, `JARVIS_ANNOUNCE_FOLLOWUP_SECONDS`, `JARVIS_LISTEN_SECONDS`, `JARVIS_END_SILENCE_MS`, `JARVIS_END_EXTEND_MS`, `JARVIS_PREROLL_MS`, `JARVIS_ONSET_PAD_MS` and `JARVIS_MAX_TURN_SECONDS`.
- Engines: `JARVIS_WAKE_ENGINE=openwakeword`, `JARVIS_STT_ENGINE=parakeet`, `JARVIS_TTS_ENGINE=qwen3` (the cloned voices of ADR 0029) or `kokoro` (the fallback), and `JARVIS_AEC=webrtc` or `off`. These are the currently supported choices; Porcupine and Whisper are not runtime alternatives yet. `--headphones` overrides echo cancellation to off.
- Detection: `JARVIS_WAKE_THRESHOLD`, `JARVIS_VAD_THRESHOLD`, `JARVIS_SMART_TURN_THRESHOLD`, `JARVIS_CHIME_GUARD_MS` and `JARVIS_WAKE_REFRACTORY_MS`. Lower wake thresholds trade fewer missed wakes for more false wakes.
- Barge-in: `JARVIS_BARGEIN_MIN_SPEECH_MS`, `JARVIS_BARGEIN_DECIDE_MS` and `JARVIS_BARGEIN_GUARD_MS` control detection and decision timing; Jarvis keeps talking while it decides, and only an interrupt phrase (`lexicons/interrupt.txt`: stop, wait, no, Jarvis, shut up, aspetta…) stops it. `JARVIS_MIN_SPEECH_DB` ignores sound quieter than speech and `JARVIS_ECHO_TAIL_MS` drops speech that began in a reply's echo tail.
- Audio clips: `JARVIS_AUDIO_CLIPS=on` opts in to WAVs and JSON sidecars in `.jarvis/clips/`. `JARVIS_CLIPS_MAX_COUNT`, `JARVIS_CLIPS_MAX_DAYS` and `JARVIS_CLIPS_MAX_MB` prune the oldest clips while saving is enabled. Saving is off by default; turning it off leaves existing clips in place. Logs contain transcripts even when clips are off. There is no voice-speed setting; speed stays at 1.0.

## Troubleshooting

- `claude` missing or authentication fails: put the installed CLI on `PATH`, run `claude` to sign in with Max, then retry `./jarvis --text`. A billing warning means the Hub removed an inherited billing switch; remove it from your shell profile too.
- Configuration error: check the named key against `.env.example`, including units and supported choices. Text mode ignores voice settings with a warning. Stats also runs without loading voice models.
- No microphone or audio: check macOS permission for the terminal, the devices in the `Audio:` notice and the Hub's listening notice. Voice mode follows the macOS default input and output, so connecting headphones moves Jarvis onto them within a second and shows `Audio moved: …`. Use `--text` to continue without audio. With headphones, use `--headphones`.
- eSpeak reports missing `phontab` or a `/Users/runner/...` path: run `uv sync --locked --reinstall-package espeakng-loader` in this checkout. Jarvis uses the installed wheel's data and automatically copies it into a private short `/tmp/jarvis-espeak-*` directory when the checkout path exceeds the native limit. No Homebrew eSpeak, environment override or worktree symlink is required; the private copy lasts for the process. Check `/tmp` permissions/free space if preparation fails. Test both engines and both voices without playing audio or making conversational model calls with `uv run python spikes/audio/check_tts_setup.py [qwen3|kokoro]` (first use may download the models). It also reports how soon the first streamed piece is ready and whether any piece was late for playback. The standalone audio benchmarks use the same setup in their own environment, for example `uv run --project spikes/audio python spikes/audio/bench_qwen_tts.py`.
- Voice loading takes too long: first downloads need network access. Check the model-loading error shown in the terminal. For a measured run, finish downloads first and stop other GPU-heavy processes; see the recording instructions in the spike notes.
- Requests split at a thinking pause: the [endpoint evidence](docs/notes/endpoint-policy-evaluation.md) separates historical replay, the merged capture-queue fix and failed acoustic/text candidates. No replacement endpoint policy has passed the complete-thought gate; keep active defaults until independent runtime validation passes.
- Missed “Ehi Jarvis” or bare “Jarvis”: these are known weak spots in synthetic testing. Record the User's takes before selecting a different engine. Do not treat the unmeasured recall target as achieved.
- Jarvis pauses or stops on its own speaker: compare the speaker checklist with `--headphones` before tuning thresholds. Cancellation now runs before model inference can queue microphone audio; [the adapter replay](docs/notes/playback-capture-isolation.md) explains the reproduced stall defect and remaining physical gate. Device delay, room cancellation and Barge-in still need the real speaker run.
- A writing Subagent cannot start: check that the sdlc plugin is installed and its worktree script is available, that the clone belongs to the configured Org, and that its worktree is not locked by another session.
- Usage limit: an on-screen notice shows the reset time, or says it is unavailable, and gives the next step. Held Tasks and new Subagents wait and retry with backoff. Send a new request after reset; no conversational model call phrases the unavailable notice. A timer clearing the hold does not confirm recovery: the recovery notice and any generated Announcement follow a successful turn. Startup, limit and backend failures use no cached spoken sentences. Stats reports observed limit hits and token usage; these are not API charges. Real limit handling is tested with fakes, not an exhausted account.
- Unexpected restart recovery: inspect `.jarvis/state.json`, `.jarvis/conversation.json` and terminal notices before changing saved data. `.jarvis/metrics.jsonl`, `.jarvis/show.log` and `.jarvis/subagents/` hold timings, shown details and Subagent progress. They may contain private work; keep them local.

## Wake-word license

The default `hey_jarvis` pretrained model comes from [openWakeWord](https://github.com/dscripka/openWakeWord#license). Its pretrained models use **CC BY-NC-SA 4.0**, including the noncommercial restriction; the project's code uses Apache 2.0. See the [model license](https://creativecommons.org/licenses/by-nc-sa/4.0/) for attribution, sharing and use conditions. A commercial wake-word model or another engine needs its own evaluation and integration; changing an environment variable does not install one.

## Development and verification

```bash
uv run pytest -q
uv run pyright
```

Default tests use fake agents, clock and audio and do not spend subscription quota. Real-model and real-GitHub tests are opt-in. [AGENTS.md](AGENTS.md), also loaded by `CLAUDE.md`, contains developer instructions; runtime prompts live in `prompts/`. Domain terms are in [GLOSSARY.md](GLOSSARY.md).

[Spike notes](docs/notes/phase-0-spikes.md) record versions, measurements and accepted limits. [The final verification checklist](docs/notes/voice-hub-verification.md) covers the recording session, model comparison and remaining release gates. The automated suite alone does not establish wake recall, speaker behavior or real-world latency.
