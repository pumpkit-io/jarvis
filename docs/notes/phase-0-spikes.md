# Phase 0 spikes

Results of the Phase 0 spikes for the voice Hub (Spec #3). Each section belongs to one Ticket: `## Agent SDK` to #4, `## Audio` to #5, `## Talker in the Hub` to #7, which answers the open questions #4 left. The throwaway scripts live under `spikes/`, never in Hub code.

## Agent SDK

Ticket #4, run on 2026-11-03 on the M3.

**Versions:** `claude-agent-sdk` 0.2.177 (Python 3.13 via `uv`), driving the installed `claude` CLI 2.1.317 (on `PATH`) through `cli_path`. The SDK also ships its own CLI binary (2.1.317 here) and uses it when `cli_path` is unset, so the Hub sets `cli_path` explicitly. Model aliases resolved to `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`.

Scripts: `spikes/agent/caps.py`, `matrix.py`, `latency.py`, and the baseline harness in `spikes/agent/baseline/`. Raw output: `spikes/agent/results/*.json`, valid JSON with the versions inside. Run each with `env -u ANTHROPIC_API_KEY uv run python <script>` from `spikes/agent/`. `matrix.py --runs N` appends N runs to its file, so every run stays committed.

### Verdicts

| Criterion | Result |
|---|---|
| Baseline, end of speech to first audio | **Waiting on the User.** Harness built and dry-run on old logs; the measured run needs the User speaking (one session with #5). |
| SDK on the Max login, no API key | Confirmed. |
| Streamed text deltas | Confirmed. |
| `interrupt()` mid-stream | Confirmed. |
| String system prompt replaces the default | Confirmed. |
| `setting_sources=[]` loads nothing | Confirmed, with built-in leftovers noted below. |
| In-process tool callable | Confirmed. |
| `tools=[]` plus in-process MCP only | Confirmed. |
| Effort and thinking matrix | Done. Every value accepted by every model; Haiku needs `effort="low"` to stop thinking. |
| Talker latency ≤ 1.0 s p50 to first sentence | **Missed by both.** Haiku 1.21 s, Sonnet 1.06 s. Recommend Sonnet at low effort as the Talker default (see below). |

### Subscription, not API

`ANTHROPIC_API_KEY` was removed from the environment of every run. Evidence the calls drew on the Max subscription:

- the `init` message reports `apiKeySource: "none"`;
- `get_server_info()["account"]` reports `subscriptionType: "Claude Max"`, `apiProvider: "firstParty"`;
- the stream carries a `RateLimitEvent` for the subscription's `five_hour` window with `status: "allowed"`. The committed results keep only the window type and status: the raw event also holds utilization and overage fields, which `caps.py` drops because the repo is public.

`ResultMessage.total_cost_usd` is still filled in (about $0.001 on the second turn's `ResultMessage` of a two-turn Haiku chat). It is a notional figure, not a charge. The Hub should log usage from `usage` and the rate-limit events, and must not present `total_cost_usd` as spend.

### SDK capabilities

All with `ClaudeSDKClient`, one session, two or more turns.

- **Two turns.** The second turn recalled the first ("41, plus one" gave "42"), same `session_id`.
- **Text deltas.** `include_partial_messages=True` yields `StreamEvent`s whose `event` is the raw API event; text arrives as `content_block_delta` with `delta.type == "text_delta"`.
- **Interrupt.** Asked to count to 300, `interrupt()` was called once more than 40 characters had streamed (`chars_at_interrupt` 41), yet 89 characters had arrived by the time the `ResultMessage` came (last line "33"), so 48 characters of deltas arrived after the call. The `ResultMessage` followed 11 ms after `interrupt()`, with `subtype: "error_during_execution"` and `terminal_reason: "aborted_streaming"`. The next `query()` on the same client answered normally. The Hub should read `terminal_reason`, not `is_error`, to tell an interrupt from a failure. For Barge-in this means the Hub must drop every text delta that arrives after it calls `interrupt()` and stop its TTS queue itself; it cannot rely on the stream going quiet at once.
- **String system prompt.** `system_prompt="Your name is Zorblax…"` gave "Zorblax", and the whole request was 906 input tokens: Claude Code's default prompt is gone.
- **`setting_sources=[]`.** Run in this repo, whose project settings carry the SessionStart hook: no hook ran, no user plugins, no MCP servers, 56 slash commands; request size 883 tokens. With `setting_sources` omitted: the hook ran, 10 plugins, 9 MCP servers, 119 slash commands, 66,262 tokens. Still present with `[]`: four built-in plugins (`cc-plugin-*`), 20 bundled skills and the five built-in agent types. They are inert for the Talker, which has no Skill or Agent tool. For the Orchestrator, pass `skills=[]` if the bundled skill listing should stay out of its prompt.
- **In-process tool.** A `@tool` registered through `create_sdk_mcp_server("jarvis", …)` was called as `mcp__jarvis__lookup_code`; the Python handler ran in-process and its result reached the reply.
- **No built-in tools.** With `tools=[]` and that server, `init.tools` was exactly `["mcp__jarvis__lookup_code"]`. Asked to run `ls`, the model answered "NO_SHELL" without any tool call.

### Effort and thinking per model

`matrix.py` sent one short arithmetic prompt per cell (`effort` alone, `thinking` alone, and `effort="low"` with thinking disabled), three runs, all committed in `results/matrix.json`. "Thinks" means thinking tokens were produced on that prompt in every run; the pattern was the same in all three.

| | default | `low` | `medium` | `high` | `xhigh` | `max` | `thinking` disabled | adaptive | enabled (budget 2048) | `low` + disabled |
|---|---|---|---|---|---|---|---|---|---|---|
| Haiku | thinks | **none** | thinks | thinks | thinks | thinks | **thinks** | thinks | thinks | **none** |
| Sonnet | none | none | none | none | none | none | none | none | none | none |
| Opus | none | none | none | none | none | **thinks** | none | none | none | none |

Haiku thought 24–156 tokens per cell (max at 114–156). Opus at `max` thought 24–44 tokens; at `xhigh` it did not think on this prompt. Sonnet never thought, at any setting.

- **Every value is accepted by every model.** No error, no warning on stderr, including `xhigh` and `max` on Haiku and Sonnet. The SDK documents `xhigh` as falling back to `high` outside Opus. So the CLI rejects nothing: the Hub's check is only the `JARVIS_MAX_EFFORT` cap, plus mapping `xhigh` to `high` for non-Opus models if it wants honest logs.
- **Haiku 5.5 ignores `thinking={"type": "disabled"}` on its own** and keeps thinking (24–55 tokens on this prompt). Only `effort="low"` turns thinking off. The Talker on Haiku therefore needs `effort="low"`, with or without the thinking setting.
- Haiku at `low` answered the arithmetic wrong every time (6 of 6 rows over the three runs: `low` and `low` plus disabled, 11:25 for 12:25). Irrelevant to small talk, worth knowing before giving Haiku `low` any Subagent work.
- The fixed thinking budget was accepted everywhere, but Sonnet never thought and Opus thought only at `max`, so whether the budget is honoured on these models is unproven. The SDK treats a budget as on/off (adaptive) on newer models; use `effort` to steer them.

### Talker latency

`latency.py`: one warm `ClaudeSDKClient` per configuration, a short Jarvis-style system prompt kept byte-identical across turns, the in-process `delegate` and `status` tools, `tools=[]`, `setting_sources=[]`, one uncounted warm-up turn, then the same 20 utterances (10 English, 10 Italian: small talk, "open a PR", "what's running", "stop", "aspetta, apri invece una issue" and so on). Each utterance is tagged `talk` or `work` by what the User asks for, not by what the model did. Time runs from `client.query()` to the first text delta and to the end of the first sentence (`.?!` followed by a space, or the end of a text block). Turns are numbered 0-19 as the rows of `results/latency.json`: turn 0 is the first counted turn, and the warm-up is not stored.

Cache reads (`cache_read`). Turn 0, a talk turn, read 1,052 tokens for both models; the ranges below cover turns 1-19.

- The other talk turns read 11.9-15.1k (Haiku) and 11.1-13.4k (Sonnet).
- Most work turns read far more: 23.6-29.5k for 8 of Haiku's 10, 22.1-26.3k for 7 of Sonnet's 10. Against the nearest talk turns before and after them within turns 1-19, that is 11.7-14.9k more for Haiku and 11.0-13.3k more for Sonnet, and the gap grows through the session: Haiku's turn 15 read 28,773 against 14,295 at turn 14, and its turn 17 read 29,514 against 14,569 at turn 16.
- Haiku's turn 1 (a work turn, tool before text) read 16,959, above the talk range and below the other work turns.
- Haiku's turn 18 (14,971, no tool before text) and Sonnet's three remaining work turns (turns 1, 9 and 18: 11,660, 11,888 and 13,280) sit inside the talk range. Sonnet's turn 1 is 11,660; its tool call came after text, inferred from the reply.
- The rows record only whether a tool came before the first text, so the jump is not tied to tool calls. A second API call in the turn would explain it, but these rows cannot show that.

`api_ms` is the per-turn delta of `duration_api_ms`, which is cumulative on a persistent client. In f29b80a every results file opened with a plain-text header line holding the SDK version and the CLI's path, with no CLI version, so none was valid JSON; `latency.json` held the cumulative values in `api_ms` (row 0 = 2515) and no `kind`. e672e82 rewrote `latency.json` as JSON without rerunning it: it added the `versions` block (CLI 2.1.317), which no run of `latency.py` wrote, derived `api_ms`, `api_ms_cumulative` and the `kind` tags from the recorded values, and set row 0's `api_ms` to null, because the warm-up total was not kept as a baseline. Every timing and cache read is unchanged from the f29b80a run. A rerun would fill row 0. The other two files differ: `matrix.json` gained two new runs in e672e82 (its first run is the f29b80a one, unchanged), and `caps.json` was rerun in 9e3713f, after which 0f826d3 replaced its plugin and MCP server names with counts without a rerun.

| Talker | First delta p50 | p90 | First sentence p50 | p90 | Tool called before any text |
|---|---|---|---|---|---|
| Haiku, `effort="low"`, thinking disabled | 1.17 s | 2.18 s | 1.21 s | 2.18 s | 9 of 20 turns |
| Sonnet, `effort="low"`, thinking disabled | 0.70 s | 2.31 s | 1.06 s | 2.31 s | 2 of 20 turns, both `status` |

Split by kind of utterance, the same 10 in each group for both models (first sentence p50):

| | Talk (small talk, thanks, stop) | Work (repo work, status, cancel) |
|---|---|---|
| Haiku | 0.85 s | **1.75 s** |
| Sonnet | 1.11 s | 0.94 s |

Over the 9 work utterances that Haiku answered with a tool call first (leaving out "aspetta, apri invece una issue"), Haiku is 1.82 s and Sonnet 0.82 s.

The first counted turn (turn 0) is slow for both models and sits inside the headline numbers above, which are kept as recorded. Without it (19 turns):

| Talker | First delta p50 | p90 | First sentence p50 | p90 |
|---|---|---|---|---|
| Haiku | 0.97 s | 2.06 s | 1.06 s | 2.06 s |
| Sonnet | 0.66 s | 2.26 s | 1.06 s | 2.26 s |

Without it Haiku is at 1.06 s p50, still above the target. Sonnet's p50 barely moves, from 1.062 s to 1.055 s.

**Verdict.** Neither meets the ≤ 1.0 s p50 target. Haiku is faster when it only talks, but it called a tool before saying anything on 9 of the 10 work turns: 6 delegations, the 2 `status` turns (where a tool first is expected) and the cancel. That ignores the prompt's speak-first rule on delegations, so the User would wait for the tool round trip (work p50 about 1.75 s) before hearing "Right away, sir". Sonnet spoke first on all 6 true delegations (0.52–1.06 s) and called a tool first only on the two `status` turns. Its two slower speak-first work turns were the "Cancel the last task" refusal (1.4 s) and the "Aspetta" clarifying question (1.5 s). Its work p50 is 0.94 s, and overall it missed the target by 62 ms.

**Recommendation:** make Sonnet at `effort="low"`, thinking disabled, the Talker default, replacing the Haiku default in the Spec, because the speak-first rule is what keeps acknowledgments under a second. Keep the model in `JARVIS_TALKER_MODEL` and rerun this script against the real Talker prompt in Ticket #7: if a firmer speak-first prompt fixes Haiku, Haiku wins back the default. The Phase 2 mitigations still apply to close the remaining gap: early first-clause emission and the 1.2 s cached filler. The cost: Sonnet draws more of the Max limits than Haiku per Talker turn.

Two findings for the sentence splitter (Ticket on spoken replies): text blocks before and after a tool call arrive with no space between them ("Right away, sir.The task is underway"), so the end of a text block must count as a sentence end; and the first counted turn of a session (turn 0) is slow (2.3 s Sonnet, 3.1 s Haiku). `latency.py` already sends a warm-up turn before it, yet turn 0 still read only 1,052 tokens from the cache, against 11.1-12.1k on turns 3 and 4, the first turns after it with no tool call for either model. So the warm-up as done did not warm the cache, and the cause is unknown. Open question for Ticket #7: find what warms the Talker's cache at launch, and measure the first real turn against it.

### Baseline: today's launcher

**Status: waiting on the User's recording session** (shared with Ticket #5). It cannot be faked: it needs the User speaking.

How it is measured. `./jarvis` runs `converse` in a loop: each call speaks, then records until VoiceMode hears its silence threshold (1000 ms), transcribes, and returns; the model then makes the next call, which synthesises and plays. For each turn, `spikes/agent/baseline/analyze.py` takes

- end of speech = `RECORDING_END` − the silence threshold, from VoiceMode's event log;
- first audio = `TTS_FIRST_AUDIO` of the next `converse` call;

and splits the gap into silence tail, STT, tool return, model time (PostToolUse of one `converse` to PreToolUse of the next, from the hook log) and TTS time to first audio. Turns where the model ran other tools in between are counted, and reported separately as "talk-only".

Steps for the session. `./jarvis` follows its symlink and starts in the main checkout, where `spikes/` only exists after merge, so give absolute paths. From any directory, with `WT` the absolute path of the checkout holding this branch:

0. Close every other VoiceMode client (any other Claude session that can call `converse`) first. Their events land in the same log and the same time window. VoiceMode tags a listening call's events with that call's session id, and the analyzer leaves out events tagged with another call's id, but `TOOL_REQUEST_END` and every event of a speak-only call carry no id, so the analyzer cannot tell those apart. Run the baseline with no other converse session open.
1. `JARVIS_BASELINE_DIR=$WT/spikes/agent/baseline jarvis --settings $WT/spikes/agent/baseline/settings.json`. The settings add `PreToolUse`, `PostToolUse` and `PostToolUseFailure` hooks (a failed `converse` call fires the last instead of `PostToolUse`) that run `python3 "$JARVIS_BASELINE_DIR/hook.py"` and append timestamps and the session id to `hooks.jsonl` beside it (gitignored). Without `JARVIS_BASELINE_DIR` set (or pointing at a folder with no `hook.py`) the command prints a warning to stderr and exits 0, so a missing variable cannot block tool calls; it also means nothing is logged. Each hook starts `python3` (about 30-50 ms per hook, two per turn), so the measured baseline slightly overstates today's launcher. The hook command was run the way `settings.json` runs it, from `/` with that variable set, and wrote both entries.
2. Talk for about 10 turns of ordinary conversation, English and Italian, with no repo work, so the model needs no tools between turns. Then exit. Each launch is its own session in the hook log.
3. `python3 $WT/spikes/agent/baseline/analyze.py`. It analyses the most recent session only (`--list-sessions` shows all, `--session <id>` picks one, `--session all` takes every hooked session's time window together, so any unhooked VoiceMode sessions inside that span are counted too), reads `~/.voicemode/logs/events/` for that session's time window, and prints the hooked-call count, each turn, how many turns were skipped for inconsistent timings and how many fell back to VoiceMode's own timestamp for a missing hook entry (counted, not skipped; the lookups never reach past the next call's start), how many calls were dropped for a start with no end (a call with no `RECORDING_END` is skipped with the inconsistent turns; events of other tools are ignored), how many events another VoiceMode client tagged with its own session id were left out, how many unusable log lines were skipped (invalid JSON, not an object, a required field missing or of the wrong type), and the p50 and p90. The window and the most recent session come from the earliest and latest hook timestamps, whatever their order in the file. `python3 $WT/spikes/agent/baseline/test_analyze.py` runs the synthetic check of that pairing. It exits with an error when the hook log is empty or missing, rather than analysing every VoiceMode event ever logged.
4. Record the totals here.

Dry run on the User's VoiceMode logs from 2026-11-01: 26 turns, end of speech to first audio p50 8.2 s, p90 17.7 s; STT p50 1.4 s, model p50 3.8 s, TTS to first audio p50 1.7 s, plus the 1.0 s silence tail. These numbers come from the analyzer as first committed in f29b80a. With no hook log it took model time from VoiceMode's own tool events (one call's `TOOL_REQUEST_END` to the next call's `TOOL_REQUEST_START`) and could not separate other tools. The current analyzer refuses to run without hook entries and the VoiceMode log is not committed, so the committed files cannot reproduce them; f29b80a's `analyze.py` run on that log does. Indicative only, not the baseline: different voice, unknown model and effort, and turns that may include tool work.

## Audio

Ticket #5, first pass run on 2026-11-03 on the User's MacBook Pro (Apple M3 Pro, 18 GB, macOS 27.0.1), without the User. Every number here comes from synthetic speech: macOS `say` voices reading the same 30 clips and 40 Wake word takes the User will record (`spikes/audio/corpus.py`), plus public audiobooks for false accepts. Synthetic speech is clean and evenly paced, so it proves the pipelines and gives speed and memory, but **accuracy on the User's voice, Wake word recall, echo on the real speakers and Smart Turn on real pauses wait on the recording session**. Leaders named below are provisional unless the criterion doesn't depend on the User's voice.

**Versions:** Python 3.12.10 via `uv` (3.12 because `kokoro-mlx` requires `<3.13`), `parakeet-mlx` 0.6.0, `mlx` 0.32.4, `mlx-whisper` 0.4.3, `kokoro-mlx` 0.1.4 with `misaki` 0.9.4, `openwakeword` 0.6.0, `onnxruntime` 1.31.1, `livekit` 1.2.0 (WebRTC APM), `transformers` 5.22.1, `lingua-language-detector` 2.2.0. VoiceMode's services: whisper.cpp v1.9.5 server on :2022 running `ggml-small.bin`, Kokoro-FastAPI 0.3.0 on :8880. Models: `mlx-community/parakeet-tdt-0.6b-v3`, `mlx-community/whisper-large-v3-turbo`, `ggml-large-v3-turbo.bin`, `mlx-community/Kokoro-82M-bf16`, openWakeWord `hey_jarvis_v0.1`, `pipecat-ai/smart-turn-v3` (`v3.2-cpu` and `v3.0`).

Scripts: `spikes/audio/` (`uv run <script>` from there). `make_synthetic.py` renders the corpus; `bench_stt.py`, `bench_lang.py`, `bench_tts.py`, `bench_wake.py`, `bench_aec.py`, `bench_smart_turn.py`, `bench_memory.py` measure; `session.py` is the recording session; `report.py` appends its results here. Raw JSON goes to `spikes/audio/data/results/` (gitignored).

### Verdicts

| Criterion | Result |
|---|---|
| Recording session | **Kit ready, waiting on the User.** `cd spikes/audio && uv run session.py`, about 35 minutes with the User (25 without the #4 baseline step), then about 12 minutes of unattended analysis. |
| STT latency, partials, memory | **Measured.** `parakeet-mlx` is 10 to 25 times faster than either Whisper and the only engine fast enough for 0.4 to 0.8 s partials. |
| STT word error rate on the User's clips | **Waiting on the User.** Synthetic WER: parakeet 2.2 %, mlx-whisper turbo 2.2 %, whisper.cpp turbo 2.9 %, VoiceMode's small 7.6 %. Provisional leader: `parakeet-mlx`. |
| TTS | **Chosen: `kokoro-mlx` in-process.** First audio 222 ms (EN) and 240 ms (IT) against 358 and 371 ms for VoiceMode's server; `am_liam` and `im_nicola` present in both; speed 1.0. Doesn't depend on the User's voice. |
| Echo cancellation | **Pipeline proven, acoustic run waiting on the User.** On digital echo the WebRTC APM takes VAD speech frames from 87 % to under 0.5 % and keeps 3 of 3 Barge-ins. The Voice Processing I/O helper is built for the session. |
| Wake word recall | **Waiting on the User.** Synthetic: "Hey Jarvis" and "Hi Jarvis" 10/10, "Jarvis" 7/10, "Ehi Jarvis" 6/10 at threshold 0.5. The last two are at risk. |
| Wake word false accepts per hour | **Measured on public audio: 0 per hour at threshold 0.5** over 11.5 h of English and Italian audiobooks; 0.8 per hour at 0.3. A room run with a video playing is in the session. |
| Wake word licence | openWakeWord code is Apache-2.0; the pretrained `hey_jarvis` model is CC BY-NC-SA 4.0 (non-commercial: fine here, goes in the README). Porcupine not run: it needs a Picovoice AccessKey, which needs an account. |
| Smart Turn v3 | **Inference measured: 14.8 ms p50** (v3.2 cpu, 8.3 MB). Saves about 485 ms per complete turn against VoiceMode's 1000 ms silence threshold; whether it holds on the User's pauses waits on the session. |
| Memory | **Within budget.** Whole audio stack in one process 2.4 GB (parakeet) or 2.8 GB (mlx-whisper); with three idle `claude` processes 3.3 or 3.65 GB, against 4 GB. Needs an MLX cache cap. |
| Chosen STT, echo cancellation, Wake word | **Waiting on the User.** |

### STT

30 clips (15 EN, 15 IT; 20 short of 0.8 to 2.2 s, 10 long of 5.6 to 6.3 s), p50 of three runs after a warm-up. "2 s" is the p50 over short clips, "6 s" over long ones. Partials are the first 0.4, 0.6 and 0.8 s from speech onset of each short clip. WER is on lowercased text without punctuation, counting "Pump Kit" as "pumpkit", "42" as "forty two" and similar spellings as equal (`EQUIVALENTS` in `bench_stt.py`).

| Engine | 2 s | 6 s | Partials 0.4 / 0.6 / 0.8 s | WER all / EN / IT | Memory |
|---|---|---|---|---|---|
| `parakeet-mlx` tdt-0.6b-v3, in-process | 80 to 105 ms | 142 to 238 ms | 84 / 90 / 98 ms | 2.2 / 0.0 / 4.5 % | 1.2 GB MLX active, load 1.6 s |
| `mlx-whisper` large-v3-turbo, in-process | 1,258 ms | 1,320 ms | 1,243 / 1,234 / 1,256 ms | 2.2 / 2.7 / 1.5 % | 1.5 GB MLX active, load 5.4 s |
| whisper.cpp `ggml-small` (VoiceMode :2022) | 512 ms | 604 ms | 446 / 460 / 611 ms | 7.6 / 2.7 / 12.9 % | 0.74 GB footprint |
| whisper.cpp large-v3-turbo (spike-only :2023) | 2,155 ms | 2,255 ms | 2,139 / 2,155 / 2,146 ms | 2.9 / 1.4 / 4.5 % | 1.86 GB footprint |

Ranges for parakeet are two runs on different minutes of the same afternoon. Findings:

- **Whisper's cost is flat.** Both Whisper builds encode a 30 s window whatever the clip length, so a 0.4 s snippet costs as much as a 6 s clip. That rules Whisper out for Barge-in partials, which the Spec wants at about 400 and 700 ms into the User's speech.
- **Parakeet partials are usable.** At 0.4 s it often has the first word ("Wait.", "Cancel"); at 0.8 s the first two to four words ("What are you working on?", "Open a PR.", "Is the CI green?"). Enough for the hard-interrupt and Backchannel lexicons.
- **Parakeet reports no language.** The Hub picks the reply voice from the User's language, so with parakeet it needs text language detection. `lingua` restricted to EN and IT got 28/30 on parakeet's transcripts in 0.03 ms; both misses were short, mixed clips ("Merge pull request 42.", "La C verde su Pump Kit."). The Hub should keep the previous language when detection isn't confident. Whisper turbo's own detection got 30/30.
- **VoiceMode's server translates Italian unless asked not to.** whisper.cpp's server defaults to English when the request names no language, and returned English translations of the Italian clips (IT WER 96 %). The numbers above pass `language=auto`.
- The large-v3-turbo whisper.cpp server ran on :2023 from VoiceMode's binary and a downloaded model in `~/.cache/jarvis-spikes/whisper/`, and was stopped afterwards; VoiceMode's own config was not touched.

### TTS

The 12-word sentences in `corpus.py` (`TTS_SENTENCES`), speed 1.0, p50 of five runs after a warm-up.

| Engine | First audio EN (`am_liam`) | First audio IT (`im_nicola`) | Memory |
|---|---|---|---|
| `kokoro-mlx` in-process, `generate_stream` | 222 ms | 240 ms | 313 MB MLX active, load 0.6 s |
| VoiceMode Kokoro-FastAPI :8880, streamed PCM | 358 ms | 371 ms | 2.0 GB footprint |

- `kokoro-mlx` yields one chunk per sentence, so first audio is the whole sentence. That fits the Hub, which synthesises sentence by sentence anyway.
- Both voices exist in both engines; `kokoro-mlx` picks Italian G2P (espeak) from the `im_` prefix. Its output transcribed back through parakeet as the intended sentences.
- **VoiceMode's server restarts itself every 25 requests**: its launchd plist sets `UVICORN_LIMIT_MAX_REQUESTS=25`. The benchmark tripped it once; the first request after a restart took 0.83 to 0.90 s. In a Hub that sends one request per sentence, that would be a silent gap every few replies.
- `misaki` downloads its spaCy model with `pip` into whatever virtualenv is active, and did so into another repo's venv during this spike (reverted). The Hub must declare `en-core-web-sm` as a dependency, as `spikes/audio/pyproject.toml` now does.

### Echo cancellation

`bench_aec.py simulate` plays Jarvis's lines (Kokoro, EN and IT) through a toy room (35 ms delay plus a decaying tail), adds noise, overlays three User clips in the second half, and runs Silero VAD (512-sample frames, p > 0.5) on the raw mic and on the WebRTC APM output (echo cancellation, noise suppression and high-pass on, 10 ms frames, reference through `process_reverse_stream`).

| Path | VAD speech frames during Jarvis alone | User Barge-ins kept |
|---|---|---|
| Raw mic | 86.8 % | 3/3 |
| WebRTC APM, stream delay 0 to 40 ms | 0.0 % | 3/3 |
| WebRTC APM, stream delay 60 to 120 ms | 0.1 to 0.5 % | 3/3 |

Digital echo is linear and easy, so this only proves the pipeline. The session's speaker run measures the real thing: about a minute of Jarvis on the built-in speakers recorded with a sample-aligned duplex stream, the User silent for the first half and speaking five cued Backchannels and Interruptions in the second. The same run then repeats through macOS Voice Processing I/O (`vpio_playrec.swift`, compiled by the session; typechecked here but not run, since it plays aloud and needs the mic), so both options are measured in one sitting. `bench_aec.py analyse` reports Barge-in triggers (VAD speech held 200 ms) per minute on Jarvis alone and the User Barge-ins detected, for the raw mic, WebRTC at six stream delays and VPIO. Headphones mode needs none of this: with no echo path the raw mic is the input.

### Wake word

openWakeWord `hey_jarvis_v0.1`, ONNX, 80 ms frames; a take counts when its peak score reaches the threshold. Synthetic takes: 10 per phrasing from nine `say` voices at random rates (Italian voices for "Ehi Jarvis").

| Phrasing | @0.3 | @0.5 | @0.7 | Median peak |
|---|---|---|---|---|
| Hey Jarvis | 10/10 | 10/10 | 10/10 | 0.998 |
| Hi Jarvis | 10/10 | 10/10 | 10/10 | 0.998 |
| Ehi Jarvis | 7/10 | 6/10 | 4/10 | 0.674 |
| Jarvis | 8/10 | 7/10 | 7/10 | 0.967 |

`vad_threshold=0.5` changed nothing on these clean takes. If the User's "Ehi Jarvis" and bare "Jarvis" also fall short of 95 %, the Ticket's open question applies: Porcupine's built-in `jarvis` keyword (matches "…Jarvis" in any phrase, needs a free AccessKey and an account) or a custom openWakeWord model trained on synthetic "Ehi Jarvis" and "Jarvis" clips (no account, a few hours of training).

#### False accepts

11.5 hours of read speech that never says "Jarvis", streamed through the model in 80 ms frames, one detection per 2 s:

| Audio | Hours | @0.3 | @0.5 | @0.7 |
|---|---|---|---|---|
| LibriSpeech `dev-clean` (English, many speakers) | 5.39 | 0 / 0 | 0 / 0 | 0 / 0 |
| LibriVox *Il giornalino di Gian Burrasca* (Italian, one reader) | 6.15 | 5 / 3 | 0 / 0 | 0 / 0 |

Each cell is false accepts without / with `vad_threshold=0.5`. At threshold 0.5 that is 0 per hour over 11.5 hours; at 0.3 it is 0.8 per hour on Italian (0.5 with VAD), still under the Spec's 1 per hour. Model cost is 3.4 to 3.8 % of one core in real time. Read audiobooks are cleaner than a room with a video playing, so the session also records 5 minutes of that, and the User's own speech near "Jarvis" is untested.

### Smart Turn v3

ONNX on CPU, one inter-op thread, including Whisper feature extraction; audio is the last 8 s with zeros padded at the start, as Pipecat does (padding at the end, which the upstream sample script does, flips most verdicts to "complete"). Complete turns are the 30 clips; cut-off turns are the 10 long clips cut at the quietest word gap near their middle.

| Model | Inference p50 (max) | Complete judged complete | Cut-off judged incomplete |
|---|---|---|---|
| `smart-turn-v3.2-cpu.onnx` (8.3 MB) | 14.8 ms (26 ms) | 28/30 | 6/10 |
| `smart-turn-v3.0.onnx` (8.4 MB) | 14.6 ms (28 ms) | 24/30 | 8/10 |

With the Spec's 500 ms VAD silence, a complete turn ends 515 ms after the User stops, against 1000 ms for VoiceMode's fixed silence threshold today: about 485 ms saved per turn (285 ms against 800 ms, 985 ms against 1500 ms). The cost is a cut-off turn judged complete, which ends the User's turn early; 6/10 on synthetic cuts is too few to judge, and the User's own clips (with real pauses) decide it.

### Memory

`bench_memory.py`: one process loads the STT, `kokoro-mlx` with both voices, openWakeWord with Silero VAD, Smart Turn v3.2 and the WebRTC APM, runs each, then reuses STT and TTS three times. MLX's buffer cache is capped at 256 MB; uncapped, the same process reaches 3.9 to 4.8 GB footprint from freed buffers alone, so the Hub must call `mx.set_cache_limit`. The `claude` share is the median footprint of the six `claude` processes running at the time (291 MB), times three.

| Stack | Audio stack footprint | MLX peak during load | Plus three idle `claude` |
|---|---|---|---|
| parakeet STT | 2.42 GB | 2.77 GB | 3.30 GB |
| mlx-whisper STT | 2.78 GB | 3.10 GB | 3.65 GB |

Both are under the 4 GB audio-stack budget on the 18 GB machine. Using VoiceMode's servers instead would add their own processes (0.74 GB for whisper small, 1.86 GB for turbo, 2.0 GB for Kokoro).

### The recording session

`cd spikes/audio && uv run session.py` runs these steps in order; `--only <step>` runs one, and every step skips takes already on disk, so it can stop and resume. Recordings go to `spikes/audio/data/recordings/` (gitignored).

1. **Preflight** (2 min): built-in mic and speakers, a 3 s level check, and a stop if macOS gives the terminal no mic access.
2. **Baseline, Ticket #4** (about 10 min): points at #4's hook-based harness in `spikes/agent/baseline/` (steps in `## Agent SDK` above, Baseline) and waits.
3. **Clips** (about 10 min): the 30 sentences, Enter to start and stop, keep or redo.
4. **Wake word** (about 3 min): "Hey Jarvis", "Hi Jarvis", "Ehi Jarvis", "Jarvis", ten 2.5 s takes each on a cue.
5. **Speaker run** (about 9 min): the WebRTC pass, the Voice Processing I/O pass, then 5 minutes of the room with a video or podcast playing, for false accepts.
6. **Analysis** (about 12 min, unattended; a dry run on the synthetic corpus took 11.7 min): starts a temporary large-v3-turbo server on :2023, runs every bench on the recordings, stops the server, and appends a results block to this section with `report.py`.

## Talker in the Hub

Ticket #7, run on 2026-11-03 on the M3: `claude-agent-sdk` 0.2.177 on Python 3.12, `claude` CLI 2.1.317 through `cli_path`, Talker on `sonnet` (resolved to `claude-sonnet-5-5`) at `effort="low"` with thinking disabled (ADR 0005), Max login, `ANTHROPIC_API_KEY` unset.

### The claude.ai connectors, and why Phase 0's turn 0 was slow

`setting_sources=[]` does not keep out the claude.ai connectors of the logged-in account. They join the session after its first query: on this account the second turn's `init` listed 24 `mcp__claude_ai_*` tools (Claude Docs and Pumpkit, `post_now` among them) next to `tools=[]`. Their schemas add about 10k tokens to every request and change the prefix, so the turn after the first one misses the cache. In a probe on the Talker prompt without the fix, the warm-up wrote 1,284 tokens to the cache, the first real turn then wrote 10,253 and read nothing, and took 2.4 s to its first delta; later turns read 8.5-10.3k. That matches Phase 0: turn 0 slow, and talk turns reading 11-15k from the cache although the request was under 1k tokens. Phase 0's latency numbers therefore carried these extra tokens.

`strict_mcp_config=True` (the CLI's `--strict-mcp-config`) removes them. The Hub sets it on every session. With it, `init.tools` is exactly the Hub's tools on every turn (`tests/test_real_model.py` checks two turns), and a Talker request is about 1.3k tokens at the start of a launch, growing with the conversation (3.6k by turn 20). Keeping the connectors out also matters beyond latency: the Talker could otherwise see tools such as Pumpkit's `post_now`, which the Boundary reserves for the User.

The CLI also makes one Haiku request of about 1,185 input tokens on the first query of each session (seen in `model_usage`); it is the CLI's own, outside our control, and did not delay the reply.

### Does the warm-up warm the cache?

Yes, once the connectors are out. The Hub sends one `<event source="hub">` message at launch and hides the reply. Probe, with a nonce appended to the prompt so the cache started cold, two launches each way:

| | First real turn, first delta | Its cache read |
|---|---|---|
| No warm-up | 4.24 s, 1.16 s | 0 (wrote about 1,300) |
| Warm-up first | 0.73 s, 0.84 s | about 720 |

The warm-up itself takes 1.3-1.7 s (up to 4.6 s seen) before the prompt appears, and reads about 1,284 tokens when the prompt was cached in the last few minutes. After it, the first real turn reads about 720 tokens: the system prompt and setup, not the warm-up exchange. The full previous request is read only from the second real turn on (1.3k and up), so something in the first user turn's request changes after it; it costs about 600 tokens of cache writes once and no visible latency. In both 20-turn runs below, the first real turn was as fast as the rest (first delta 0.73 s and 0.80 s).

### Small talk latency in text mode

`env -u ANTHROPIC_API_KEY uv run jarvis-hub --text < docs/notes/talker-small-talk.txt` (20 lines, 10 English and 10 Italian, small talk only: the Talker has no tools yet), timed by the Hub from sending the message to the first text delta and to the end of the first sentence (`.?!` followed by a space or the end of a delta, or the end of a text block: Phase 0's definition). Per-turn rows go to `.jarvis/metrics.jsonl`. Run 1 used the first draft of `prompts/talker.md`; run 2 the committed one, which asks for a short opening sentence and states the User's name plainly (run 1's wording, "the voice of {user}'s assistant", left the model unsure what to call the User).

| Run | First delta p50 | p90 | First sentence p50 | p90 | First sentence ≤ 1.0 s |
|---|---|---|---|---|---|
| 1 (first prompt draft) | 0.83 s | 1.55 s | 1.16 s | 1.84 s | 1 of 20 |
| 2 (committed prompt) | 0.85 s | 3.21 s | 1.20 s | 3.73 s | 5 of 20 |

**Verdict: missed, narrowly.** Small talk reaches the first sentence in about 1.2 s at p50, against the target of about 1 s; the first delta arrives in about 0.85 s, so the model's time to first token is most of it. Run 2's p90 comes from four slow turns (2.7-8.1 s) during what looked like a slow patch on the API side; its middle 12 turns sat between 0.89 and 1.57 s. A shorter opening sentence helped the fast turns (5 under a second against 1) but not the median. What is left to close the gap belongs to the spoken-reply ticket: emitting the first clause early on `,;:` once it has six words, and the cached filler at 1.2 s. Haiku was 0.85 s on small talk in Phase 0 but fails the speak-first rule (ADR 0005). The Haiku rerun with a firmer speak-first prompt that #4 asked for needs `delegate`, so it moves to the Tasks ticket (#8).

### Interrupting from the keyboard

Ctrl-C in the terminal reaches the `claude` CLI as well, because it runs in the Hub's process group, and the CLI exits. So in text mode Ctrl-C ends the conversation (exit 130), not a reply. `Hub.interrupt()` works through the backend (tested with the fake and against the real model); a keyboard or spoken trigger for it needs the CLI out of the terminal's process group, which the SDK does not offer.
