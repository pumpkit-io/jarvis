# Phase 0 spikes

Results of the Phase 0 spikes for the voice Hub (Spec #3). Each section belongs to one Ticket: `## Agent SDK` to #4, `## Audio` to #5. The throwaway scripts live under `spikes/`, never in Hub code.

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

`ResultMessage.total_cost_usd` is still filled in (about $0.001 for a two-turn Haiku chat). It is a notional figure, not a charge. The Hub should log usage from `usage` and the rate-limit events, and must not present `total_cost_usd` as spend.

### SDK capabilities

All with `ClaudeSDKClient`, one session, two or more turns.

- **Two turns.** Turn 2 recalled turn 1 ("41, plus one" gave "42"), same `session_id`.
- **Text deltas.** `include_partial_messages=True` yields `StreamEvent`s whose `event` is the raw API event; text arrives as `content_block_delta` with `delta.type == "text_delta"`.
- **Interrupt.** Asked to count to 300, `interrupt()` was called once more than 40 characters had streamed (`chars_at_interrupt` 41), yet 89 characters had arrived by the time the `ResultMessage` came (last line "33"), so 48 characters of deltas arrived after the call. The `ResultMessage` followed 11 ms after `interrupt()`, with `subtype: "error_during_execution"` and `terminal_reason: "aborted_streaming"`. The next `query()` on the same client answered normally. The Hub should read `terminal_reason`, not `is_error`, to tell an interrupt from a failure. For Barge-in this means the Hub must drop every text delta that arrives after it calls `interrupt()` and stop its TTS queue itself; it cannot rely on the stream going quiet at once.
- **String system prompt.** `system_prompt="Your name is Zorblax…"` gave "Zorblax", and the whole request was 906 input tokens: Claude Code's default prompt is gone.
- **`setting_sources=[]`.** Run in this repo, whose project settings carry the SessionStart hook: no hook ran, no user plugins (sdlc, voicemode and the rest), no MCP servers, 56 slash commands; request size 883 tokens. With `setting_sources` omitted: the hook ran, 10 plugins, 9 MCP servers (VoiceMode, PostHog, Pumpkit, claude.ai connectors), 119 slash commands, 66,262 tokens. Still present with `[]`: four built-in plugins (`cc-plugin-*`), 20 bundled skills and the five built-in agent types. They are inert for the Talker, which has no Skill or Agent tool. For the Orchestrator, pass `skills=[]` if the bundled skill listing should stay out of its prompt.
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

`latency.py`: one warm `ClaudeSDKClient` per configuration, a short Jarvis-style system prompt kept byte-identical across turns, the in-process `delegate` and `status` tools, `tools=[]`, `setting_sources=[]`, one warm-up turn not counted, then the same 20 utterances (10 English, 10 Italian: small talk, "open a PR", "what's running", "stop", "aspetta, apri invece una issue" and so on), each tagged `talk` or `work` by what the User asks for, not by what the model did. Time is measured from `client.query()` to the first text delta and to the end of the first sentence (`.?!` followed by a space, or the end of a text block). Cache reads (`cache_read` in the rows, counted turns 1-19; turn 0 read 1,052) split in two. Talk turns read 11.9-15.1k tokens (Haiku) and 11.1-13.4k (Sonnet). Most work turns read about 10-13k more: 23.6-29.5k for 8 of Haiku's 10, 22.1-26.3k for 7 of Sonnet's 10. The other work turns read like talk turns: Haiku 14,971 and 16,959 (turn 1, tool before text), Sonnet 11,660 (turn 1, tool after text), 11,888 and 13,280. The rows do not record whether a work turn called a tool, only whether one came before the first text, so the jump is not tied to tool calls here; one possible cause is a second API call in the turn, which these rows cannot confirm. Each series grows slowly. `api_ms` in the results is the per-turn delta of `duration_api_ms`, which is cumulative on a persistent client. The committed JSON came from the run before per-turn deltas were added, so the first counted turn has none; a rerun would fill that row.

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

The first counted turn is slow for both models and sits inside the headline numbers above, which are kept as recorded. Without it (19 turns):

| Talker | First delta p50 | p90 | First sentence p50 | p90 |
|---|---|---|---|---|
| Haiku | 0.97 s | 2.06 s | 1.06 s | 2.06 s |
| Sonnet | 0.66 s | 2.26 s | 1.06 s | 2.26 s |

Without it Haiku is at 1.06 s p50, still above the target. Sonnet's p50 is unchanged at 1.06 s (1.055 s).

**Verdict.** Neither meets the ≤ 1.0 s p50 target. Haiku is faster when it only talks, but it called a tool before saying anything on 9 of the 10 work turns: 6 delegations, the 2 `status` turns (where a tool first is expected) and the cancel. That ignores the prompt's speak-first rule on delegations, so the User would wait for the tool round trip (work p50 about 1.75 s) before hearing "Right away, sir". Sonnet spoke first on all 6 true delegations (0.52–1.06 s) and called a tool first only on the two `status` turns. Its two slower speak-first work turns were the "Cancel the last task" refusal (1.4 s) and the "Aspetta" clarifying question (1.5 s). Its work p50 is 0.94 s, and overall it missed the target by 62 ms.

**Recommendation:** make Sonnet at `effort="low"`, thinking disabled, the Talker default, replacing the Haiku default in the Spec, because the speak-first rule is what keeps acknowledgments under a second. Keep the model in `JARVIS_TALKER_MODEL` and rerun this script against the real Talker prompt in Ticket #7: if a firmer speak-first prompt fixes Haiku, Haiku wins back the default. The Phase 2 mitigations still apply to close the remaining gap: early first-clause emission and the 1.2 s cached filler. The cost: Sonnet draws more of the Max limits than Haiku per Talker turn.

Two findings for the sentence splitter (Ticket on spoken replies): text blocks before and after a tool call arrive with no space between them ("Right away, sir.The task is underway"), so the end of a text block must count as a sentence end; and the first counted turn of a session is slow (2.3 s Sonnet, 3.1 s Haiku). `latency.py` already sends a warm-up turn before it, yet that first counted turn still read only 1,052 tokens from the cache, against 11-12k on the next turns with no tool call. So the warm-up as done did not warm the cache, and the cause is unknown. Open question for Ticket #7: find what warms the Talker's cache at launch, and measure the first real turn against it.

### Baseline: today's launcher

**Status: waiting on the User's recording session** (shared with Ticket #5). It cannot be faked: it needs the User speaking.

How it is measured. `./jarvis` runs `converse` in a loop: each call speaks, then records until VoiceMode hears its silence threshold (1000 ms), transcribes, and returns; the model then makes the next call, which synthesises and plays. For each turn, `spikes/agent/baseline/analyze.py` takes

- end of speech = `RECORDING_END` − the silence threshold, from VoiceMode's event log;
- first audio = `TTS_FIRST_AUDIO` of the next `converse` call;

and splits the gap into silence tail, STT, tool return, model time (PostToolUse of one `converse` to PreToolUse of the next, from the hook log) and TTS time to first audio. Turns where the model ran other tools in between are counted, and reported separately as "talk-only".

Steps for the session. `./jarvis` follows its symlink and starts in the main checkout, where `spikes/` only exists after merge, so give absolute paths. From any directory, with `WT` the absolute path of the checkout holding this branch:

1. `JARVIS_BASELINE_DIR=$WT/spikes/agent/baseline jarvis --settings $WT/spikes/agent/baseline/settings.json`. The settings add `PreToolUse`, `PostToolUse` and `PostToolUseFailure` hooks (a failed `converse` call fires the last instead of `PostToolUse`) that run `python3 "$JARVIS_BASELINE_DIR/hook.py"` and append timestamps and the session id to `hooks.jsonl` beside it (gitignored). Without `JARVIS_BASELINE_DIR` set (or pointing at a folder with no `hook.py`) the command prints a warning to stderr and exits 0, so a missing variable cannot block tool calls; it also means nothing is logged. Each hook starts `python3` (about 30-50 ms per hook, two per turn), so the measured baseline slightly overstates today's launcher. The hook command was run the way `settings.json` runs it, from `/` with that variable set, and wrote both entries.
2. Talk for about 10 turns of ordinary conversation, English and Italian, with no repo work, so the model needs no tools between turns. Then exit. Each launch is its own session in the hook log.
3. `python3 $WT/spikes/agent/baseline/analyze.py`. It analyses the most recent session only (`--list-sessions` shows all, `--session <id>` picks one, `--session all` takes every hooked session's time window together, so any unhooked VoiceMode sessions inside that span are counted too), reads `~/.voicemode/logs/events/` for that session's time window, and prints the hooked-call count, each turn, how many turns were skipped for inconsistent timings (a missing hook entry; the lookups never reach past the next call's start) and the p50 and p90. `python3 $WT/spikes/agent/baseline/test_analyze.py` runs the synthetic check of that pairing. It exits with an error when the hook log is empty or missing, rather than analysing every VoiceMode event ever logged.
4. Record the totals here.

Dry run: an earlier version of the analyzer was run on the User's VoiceMode logs from 2026-11-01 (no hook log then, so model time fell back to VoiceMode's own tool events and other tools couldn't be separated; that fallback is gone, the analyzer now refuses to run without hook entries). It found 26 turns: end of speech to first audio p50 8.2 s, p90 17.7 s; STT p50 1.4 s, model p50 3.8 s, TTS to first audio p50 1.7 s, plus the 1.0 s silence tail. These are indicative only (different voice, unknown model and effort, possibly turns with tool work), not the baseline.
