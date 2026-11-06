# Voice Hub final verification (#21)

Status on 2026-11-04: documentation and fake-adapter checks are implemented. A controlled ten-request-per-model Orchestrator comparison is recorded in [orchestrator-comparison.json](orchestrator-comparison.json); final physical verification has not run. Spec #3's performance targets remain acceptance gates, not measured results. Historical synthetic numbers and SDK probes in [phase-0-spikes.md](phase-0-spikes.md) are evidence only for the conditions they name.

## Preserve comparable runs

Record the commit, macOS and hardware, dependency versions (`uv tree`), installed `claude --version`, model names and efforts, engine settings, devices and whether headphones were used. Finish downloads and stop other GPU-heavy processes before timing. Keep raw logs and recordings local; commit only reviewed aggregate results and versions. Do not publish transcripts or personal account data.

Use one fresh launch and a recorded start/end timestamp per measurement block. `.jarvis/metrics.jsonl` appends across launches, so use the timestamps to select that block. Do not delete existing runtime state to obtain an empty log. Exclude warm-up, restart, typed, silent end-control and Announcement turns from the spoken-request latency sample; retain their counts separately.

## Recording and baseline

- [ ] Run the recording session from `spikes/audio`: `env -u VIRTUAL_ENV -u ANTHROPIC_API_KEY uv run session.py`. It prompts for the User's presence before recording, then performs unattended room recording and analysis. `--only <step>` and `--redo` resume or replace takes; see the Audio section of the spike notes for the sequence and prerequisites.
- [ ] For the old VoiceMode baseline, use a preserved pre-Hub launcher checkout (the #5 worktree), the hooks in `spikes/agent/baseline/` and the `JARVIS_BASELINE_DIR` instructions in the Agent SDK notes. The new launcher does not run VoiceMode. Record ten baseline turns and compare them to the Hub sample; do not compare a synthetic baseline with live Hub turns.
- [ ] Review `spikes/audio/results/`, `summarise.py` and `report.py` output, then record STT, Wake word and echo-cancellation choices with the User's voice. The session's legacy VoiceMode service dependencies are for baseline/engine comparison only; normal Hub operation does not require the plugin.
- [ ] Wake recall: at least 19 of 20 takes for each intended phrasing (95%). False wakes: at most one per hour during the hour-long room run. The current synthetic weakness for “Ehi Jarvis” and bare “Jarvis” remains unresolved until these results exist.

## Hub conversation

- [ ] Follow the manual launch check in the spike notes, including readiness within 10 seconds after caches are populated, whole-utterance stop phrases and typed input staying silent.
- [ ] Record at least 30 spoken request/reply turns, English and Italian, including both small talk and requests delegated to Tasks. Keep a local numbered manifest identifying delegated acknowledgments, interrupted turns, filler and any failed/missing audio. Report failures and exclusions rather than dropping them to improve percentiles.
- [ ] For each completed spoken turn, use the `speech` row's `first_meaningful_audio_ms` from the User's end-of-speech time; the `first_chunk_ms` and Talker `first_sentence_ms` measure different points. This is a playback-queue estimate, so measure physical first request-specific audio separately. Holding, startup and unavailable cached speech are removed; old logs and nonverbal readiness cues must not count as a meaningful response. Target meaningful first audio: p50 ≤1500 ms and p90 ≤2500 ms. For the manifest's delegated requests, acknowledgment p50 ≤1500 ms. Use the same percentile calculation as `jarvis_hub.stats.percentile`, and report sample counts.
- [ ] `./jarvis stats --since <start> --until <end>` summarizes usage and model latency, not the full end-of-speech-to-audio acceptance sample. Read the selected `speech` rows separately and correlate them with the local manifest. Use the new `run_id` and Hub `turn` fields to join model and speech rows, and `request_at` to correlate capture. Legacy rows lack this complete link.
- [ ] Leave the Hub idle for 30 minutes with no pending Tasks, waits or news. After its background warm-up, expect no model turns and no “are you still there” prompt. Record start/end model-turn counts and any voice transitions. An active Task or Announcement is not an idle measurement.
- [ ] Run the Listening and Barge-in manual checklists in the spike notes on both headphones and built-in speakers, including ten minutes of Jarvis speaking without anyone interrupting: zero self-interruptions. `./jarvis stats --bargein --since <start> --until <end>` reports decisions, timing percentiles and likely wrong resumes. Keep observed false stops and false resumes in the result.
- [ ] Measure Hub idle CPU over the idle interval and peak memory during the whole session, including its audio and child processes. Record the tool and sampling interval. Use the [whole-Hub resource observer](hub-resource-measurement.md) for an actual PID/tree observation; it excludes startup and reports sampled RSS/CPU with lifecycle limits, not an audio-budget verdict. `spikes/audio/bench_memory.py` and `bench_claude_idle.py` describe the earlier component measurements; those numbers do not substitute for this whole-Hub run. The audio-stack budget is about 4 GB; report total process memory separately.
- [ ] Give two read-only Tasks on two Org repos concurrently and verify both reports arrive. Preserve task IDs and completion times. Existing fake tests and the #10 local-commit spike do not prove two real Subagents reporting together.
- [ ] Verify subscription use for every role with the account/init and rate-limit evidence described in the Agent SDK notes (`apiKeySource=none`, Max/firstParty account, subscription window). Keep credentials and utilization private. A `total_cost_usd` field alone is not evidence of an API charge or of subscription use.

## Orchestrator comparison

The controlled run used fresh one-turn sessions with the runtime Orchestrator prompt, a JSON evaluation contract and ten synthetic task/result pairs per model. Tools were disabled: no real repo reads, Task execution, Subagents or actions ran. Both models showed gaps, detailed in the spike notes and raw artifact. Sonnet medium remains provisional. This establishes reasoning-comparison evidence, not end-to-end task performance.

For a later live-workflow confirmation, run the same ten requests with `JARVIS_ORCHESTRATOR_MODEL=haiku` and then `sonnet`, both at medium effort with the same Talker, repo state and settings. Start a fresh Hub for each model; this consumes real subscription turns and is not part of the default test suite. These categories correspond to the controlled cases; the full synthetic facts and candidate results are in the artifact:

1. Summarize open PRs in an Org repo, identifying which need the User.
2. Explain a failed CI run and recommend the next investigation.
3. Summarize the difference between a PR description and its implementation.
4. Investigate a reported bug and propose a testable cause without changing files.
5. Plan a contained fix with acceptance criteria and a suitable Subagent model/effort.
6. Compare interfaces in two Org repos and explain where their responsibilities differ.
7. Review a Subagent's supplied result against its brief and identify missing evidence.
8. Explain a stale PR's blockers, choosing a Watcher or Check where appropriate.
9. Explain a deploy runbook and stop at the Boundary without deploying.
10. Assess a proposed destructive cleanup, identify the exact Approval needed and leave all data intact.

For each model and request, record whether the brief preserves scope, model/effort matches the rubric, result judgment checks evidence, waiting follows events before polling, and the Boundary is respected. Record turns, latency and token usage alongside the judgments. Mark missing evidence as such. A model that is faster but misses a Boundary or material requirement does not pass. Keep the controlled comparison separate from any later live-workflow results. The recorded recommendation retains Sonnet medium provisionally, with no claim that either model passed every rubric check.

## Startup and unavailable status

- [ ] Verify EN/IT whole-utterance end controls return idle silently and add no model turn. Longer requests containing those words must reach the Talker. When the Talker generates a farewell and calls `end_conversation`, let that reply finish before idle, with no extra acknowledgment.
- [ ] Confirm loading notices precede readiness, with a nonverbal cue and no startup greeting. Distinguish model, speech and listening readiness; a speech cue alone does not establish that the model or microphone is ready.
- [ ] Use fake limits to verify known/unknown resets, held work, repeated-limit backoff and actionable status without spoken fallback. A timer clearing is not recovery evidence. Confirm the notice and generated recovery Announcement follow an actual successful held-role turn, including Quiet mode.
- [ ] Confirm backend errors show their cause and retry/restart step while typed input remains usable under the existing lifecycle. Keep any real-model and device check separately authorized.

## Remaining decisions and limitations

- The #9 follow-up removes the 100 ms quiet settle. Sentence boundaries require punctuation followed by whitespace, a text-block end, or a reply end, so delayed domain suffixes stay whole. Recheck physical latency with this behavior; the earlier timer-based numbers do not measure it.
- `github-stats` is absent from the Repo catalog; its purpose needs clarification before adding it.
- The branch-delete mutation has schema/stub coverage only. The User declined real branch deletions; do not perform one for verification.
- An X-post Approval pins a post ID and arguments, not mutable post text. No real post is part of this verification.
- Real usage-limit exhaustion and a real Subagent opening a PR have not been exercised; fakes cover those paths. Do not exhaust quota or publish solely to manufacture evidence.
- The animate-pixel-art scratch worktree and branch from #10 stay in place, per the instruction against branch deletions.

## Results to include before merging

Append each run's commit and conditions, aggregate results, missing samples, accepted deviations and evidence paths to the spike notes. Include the same outcomes in the PR description. Keep #4, #5 and #21's unmet acceptance criteria open until their measurements and comparison are actually recorded. A passing automated suite does not close these gates.
