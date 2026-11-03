# Phase 0 spikes

Results of the Phase 0 spikes for the voice Hub (Spec #3). Each section belongs to one Ticket: `## Agent SDK` to #4, `## Audio` to #5. The throwaway scripts live under `spikes/`, never in Hub code.

## Agent SDK

Ticket #4, run on 2026-11-03 on the M3.

**Versions:** `claude-agent-sdk` 0.2.177 (Python 3.13 via `uv`), driving the installed `claude` CLI 2.1.317 at `~/.local/bin/claude` through `cli_path`. The SDK also ships its own CLI binary (2.1.317 here) and uses it when `cli_path` is unset, so the Hub sets `cli_path` explicitly. Model aliases resolved to `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`.

Scripts: `spikes/agent/caps.py`, `matrix.py`, `latency.py`, and the baseline harness in `spikes/agent/baseline/`. Raw output: `spikes/agent/results/`. Run each with `env -u ANTHROPIC_API_KEY uv run python <script>` from `spikes/agent/`.

### Verdicts

| Criterion | Result |
|---|---|
| Baseline, end of speech to first audio | **Waiting on the User.** Harness built and dry-run on old logs; the measured run needs the User speaking (one session with #5). |
| SDK on the Max login, no API key | **Confirmed.** |
| Streamed text deltas | **Confirmed.** |
| `interrupt()` mid-stream | **Confirmed.** |
| String system prompt replaces the default | **Confirmed.** |
| `setting_sources=[]` loads nothing | **Confirmed**, with built-in leftovers noted below. |
| In-process tool callable | **Confirmed.** |
| `tools=[]` plus in-process MCP only | **Confirmed.** |
| Effort and thinking matrix | **Done.** Every value accepted by every model; Haiku needs `effort="low"` to stop thinking. |
| Talker latency ≤ 1.0 s p50 to first sentence | **Missed by both.** Haiku 1.21 s, Sonnet 1.06 s. Recommend Sonnet at low effort as the Talker default (see below). |

### Subscription, not API

`ANTHROPIC_API_KEY` was removed from the environment of every run. Evidence the calls drew on the Max subscription:

- the `init` message reports `apiKeySource: "none"`;
- `get_server_info()["account"]` reports `subscriptionType: "Claude Max"`, `apiProvider: "firstParty"`;
- the stream carries `RateLimitEvent`s for the subscription windows (`five_hour` at 17%, `seven_day` at 73% during the run), with `overageStatus: "rejected"`, so nothing spilled into paid overage.

`ResultMessage.total_cost_usd` is still filled in (about $0.002 for a two-turn Haiku chat). It is a notional figure, not a charge. The Hub should log usage from `usage` and the rate-limit events, and must not present `total_cost_usd` as spend.

### SDK capabilities

All with `ClaudeSDKClient`, one session, two or more turns.

- **Two turns.** Turn 2 recalled turn 1 ("41, plus one" gave "42"), same `session_id`.
- **Text deltas.** `include_partial_messages=True` yields `StreamEvent`s whose `event` is the raw API event; text arrives as `content_block_delta` with `delta.type == "text_delta"`.
- **Interrupt.** Asked to count to 300, interrupted after about 40 characters (at "33"): the `ResultMessage` arrived 10 ms after `interrupt()`, with `subtype: "error_during_execution"` and `terminal_reason: "aborted_streaming"`. The next `query()` on the same client answered normally. The Hub should read `terminal_reason`, not `is_error`, to tell an interrupt from a failure.
- **String system prompt.** `system_prompt="Your name is Zorblax…"` gave "Zorblax", and the whole request was 902 input tokens: Claude Code's default prompt is gone.
- **`setting_sources=[]`.** Run in this repo, whose project settings carry the SessionStart hook: no hook ran, no user plugins (sdlc, voicemode and the rest), no MCP servers, 56 slash commands; request size 879 tokens. With `setting_sources` omitted: the hook ran, 10 plugins, 9 MCP servers (VoiceMode, PostHog, Pumpkit, claude.ai connectors), 119 slash commands, 66,222 tokens. Still present with `[]`: four built-in plugins (`cc-plugin-*`), 20 bundled skills and the five built-in agent types. They are inert for the Talker, which has no Skill or Agent tool. For the Orchestrator, pass `skills=[]` if the bundled skill listing should stay out of its prompt.
- **In-process tool.** A `@tool` registered through `create_sdk_mcp_server("jarvis", …)` was called as `mcp__jarvis__lookup_code`; the Python handler ran in-process and its result reached the reply.
- **No built-in tools.** With `tools=[]` and that server, `init.tools` was exactly `["mcp__jarvis__lookup_code"]`. Asked to run `ls`, the model answered "NO_SHELL" without any tool call.

### Effort and thinking per model

`matrix.py` sent one short arithmetic prompt per cell (`effort` alone, `thinking` alone, and `effort="low"` with thinking disabled). It ran twice with the same pattern. "Thinking" below means thinking tokens were produced on that prompt.

| | default | `low` | `medium` | `high` | `xhigh` | `max` | `thinking` disabled | adaptive | enabled (budget 2048) | `low` + disabled |
|---|---|---|---|---|---|---|---|---|---|---|
| Haiku | thinks | **no thinking** | thinks | thinks | thinks | thinks | **still thinks** | thinks | thinks | **no thinking** |
| Sonnet | none | none | none | none | none | thinks | none | none | none | none |
| Opus | none | none | none | none | thinks | thinks | none | none | none | none |

- **Every value is accepted by every model.** No error, no warning on stderr, including `xhigh` and `max` on Haiku and Sonnet. The SDK documents `xhigh` as falling back to `high` outside Opus. So the CLI rejects nothing: the Hub's check is only the `JARVIS_MAX_EFFORT` cap, plus mapping `xhigh` to `high` for non-Opus models if it wants honest logs.
- **Haiku 5.5 ignores `thinking={"type": "disabled"}` on its own** and keeps thinking (45–75 tokens on this prompt). Only `effort="low"` turns thinking off. The Talker on Haiku therefore needs `effort="low"`, with or without the thinking setting.
- Haiku at `low` answered the arithmetic wrong in every run (6 of 6, 11:25 for 12:25). Irrelevant to small talk, worth knowing before giving Haiku `low` any Subagent work.
- The fixed thinking budget was accepted everywhere, but Sonnet and Opus produced no thinking on this prompt, so whether the budget is honoured on these models is unproven. The SDK treats a budget as on/off (adaptive) on newer models; use `effort` to steer them.

### Talker latency

`latency.py`: one warm `ClaudeSDKClient` per configuration, a short Jarvis-style system prompt kept byte-identical across turns, the in-process `delegate` and `status` tools, `tools=[]`, `setting_sources=[]`, one warm-up turn not counted, then the same 20 utterances (10 English, 10 Italian: small talk, "open a PR", "what's running", "stop", "aspetta, apri invece una issue" and so on). Time is measured from `client.query()` to the first text delta and to the end of the first sentence (`.?!` followed by a space, or the end of a text block). Prompt caching held: cache reads grew turn by turn from the second turn.

| Talker | First delta p50 | p90 | First sentence p50 | p90 | Tool called before any text |
|---|---|---|---|---|---|
| Haiku, `effort="low"`, thinking disabled | 1.17 s | 2.18 s | 1.21 s | 2.18 s | 9 of 20 turns, including all 6 delegations |
| Sonnet, `effort="low"`, thinking disabled | 0.70 s | 2.31 s | **1.06 s** | 2.31 s | 2 of 20 turns, both `status` |

Split by kind of turn (first sentence p50):

- Small talk and short replies: Haiku 0.86 s, Sonnet 1.16 s.
- Delegating and status turns: Haiku 1.82 s, Sonnet 0.74 s.

**Verdict.** Neither meets the ≤ 1.0 s p50 target. Haiku is faster when it only talks, but it called `delegate` before saying anything on every delegating turn, despite the prompt's speak-first rule, so the User would wait for the tool round trip (about 1.8 s) before hearing "Right away, sir". Sonnet obeyed the rule on every delegation ("Right away, sir." in 0.5–1.1 s, p50 0.65 s) and came within 60 ms of the target overall.

**Recommendation:** make Sonnet at `effort="low"`, thinking disabled, the Talker default, replacing the Haiku default in the Spec, because the speak-first rule is what keeps acknowledgments under a second. Keep the model in `JARVIS_TALKER_MODEL` and rerun this script against the real Talker prompt in Ticket #7: if a firmer speak-first prompt fixes Haiku, Haiku wins back the default. The Phase 2 mitigations still apply to close the remaining gap: early first-clause emission and the 1.2 s cached filler. The cost: Sonnet draws more of the Max limits than Haiku per Talker turn.

Two findings for the sentence splitter (Ticket on spoken replies): text blocks before and after a tool call arrive with no space between them ("Right away, sir.The task is underway"), so the end of a text block must count as a sentence end; and the first turn of a session is slow (2.3–3.1 s) while the conversation cache is cold, so the Hub should warm the Talker at launch.

### Baseline: today's launcher

**Status: waiting on the User's recording session** (shared with Ticket #5). It cannot be faked: it needs the User speaking.

How it is measured. `./jarvis` runs `converse` in a loop: each call speaks, then records until VoiceMode hears its silence threshold (1000 ms), transcribes, and returns; the model then makes the next call, which synthesises and plays. For each turn, `spikes/agent/baseline/analyze.py` takes

- end of speech = `RECORDING_END` − the silence threshold, from VoiceMode's event log;
- first audio = `TTS_FIRST_AUDIO` of the next `converse` call;

and splits the gap into silence tail, STT, tool return, model time (PostToolUse of one `converse` to PreToolUse of the next, from the hook log) and TTS time to first audio. Turns where the model ran other tools in between are counted, and reported separately as "talk-only".

Steps for the session:

1. From this worktree (or wherever this branch is checked out): `./jarvis --settings spikes/agent/baseline/settings.json`. The settings file adds `PreToolUse`, `PostToolUse` and `UserPromptSubmit` hooks that append timestamps to `spikes/agent/baseline/hooks.jsonl` (gitignored).
2. Talk for about 10 turns of ordinary conversation, English and Italian, with no repo work, so the model needs no tools between turns. Then exit.
3. `python3 spikes/agent/baseline/analyze.py`. It reads the hook log and `~/.voicemode/logs/events/`, limits itself to the hook log's time window, and prints each turn and the p50 and p90.
4. Record the totals here.

Dry run: the analyzer was run on the User's VoiceMode logs from 2026-11-01 (no hook log then, so model time falls back to VoiceMode's own tool events and other tools can't be separated). It found 26 turns: end of speech to first audio p50 8.2 s, p90 17.7 s; STT p50 1.4 s, model p50 3.8 s, TTS to first audio p50 1.7 s, plus the 1.0 s silence tail. These are indicative only (different voice, unknown model and effort, possibly turns with tool work), not the baseline.
