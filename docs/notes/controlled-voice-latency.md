# Controlled meaningful-audio latency

`./jarvis stats --latency-plan plan.json --latency-observations observations.json --metrics metrics.jsonl` produces an offline JSON report. It reads a fixed byte snapshot of each input and records their hashes. No Hub, model or audio device is started. Existing usage, Barge-in and `--voice` trace reports retain their behavior. Time filters and other report flags cannot be combined with this controlled sample.

Preparation uses two files because the runtime creates request identities during the conversation. Freeze and independently archive the plan hash before collection; add only bindings and observations afterward. A hash binds file contents, not the truth of its declared creation time. Keep the complete metrics snapshot and plan, including failed and slow trials. Personal transcripts, recordings and raw logs remain local; review any aggregate report before publication.

## Plan before collection

A version-1 plan declares `declared_at` with a timezone, `measurement` as physical/queue_estimate/synthetic, and an ordered `samples` array. Each sample has an opaque `sample_id`, `language` en/it for eligible speech, and a category: direct_answer, delegated_work, typed, announcement, warmup, chime, cached or other. Direct-answer and delegated-work samples are always eligible. Other categories are excluded up front; `other` also needs `exclusion_reason` setup/exploratory/ineligible_source. Post-run eligibility overrides are rejected.

```json
{
  "version": 1,
  "declared_at": "2026-11-06T10:00:00Z",
  "measurement": "physical",
  "samples": [
    {"sample_id": "en01", "language": "en", "category": "direct_answer"},
    {"sample_id": "it01", "language": "it", "category": "delegated_work"},
    {"sample_id": "ready-cue", "language": null, "category": "chime"}
  ]
}
```

This illustrates the schema, not a completed or sufficient sample. Predeclare at least 30 eligible turns with both languages and both request categories. Use the existing physical verification procedure to control model/engine settings and device conditions. Chimes, cached speech, warm-up, typed turns and Announcements need their own categories; they cannot supply meaningful-response latency.

## Bind observations afterward

The observations object contains `plan_sha256` (SHA256 of the exact plan bytes) and `samples`. Each observed sample names its `sample_id` and an outcome: completed, failed or not_attempted. Omitted entries remain missing/unknown. Failed or not-attempted entries do not shrink eligibility. Optional `failure_reason` uses capture_failure/backend_failure/no_audio/interrupted/superseded/not_request_specific/unknown; missing reasons remain unknown, never inferred from silence. A completed eligible sample supplies exact `run_id`, positive integer `turn`, `request_id`, nonnegative integer `revision` and `request_specific: true` after human review. These join the Hub user result and generated Speech row; no timestamp-only or cross-run ownership fallback is allowed. Excluded cues may be recorded as completed without a Hub turn.

Optional `physical` evidence contains an opaque `clock_id`, `reviewed: true`, `speech_end_s` and `first_audio_s`, both measured on that one physical recording/clock. Use the first request-specific generated speech, not a chime, filler or cached sentence. The adapter checks finite/nonnegative values and order; it cannot independently certify the recording or human judgment. App `perf_counter` timestamps and wall-clock log timestamps are not substituted for this physical clock.

For example, a completed binding has this shape; the IDs and times below are fictional schema examples:

```json
{
  "sample_id": "en01", "outcome": "completed", "request_specific": true,
  "run_id": "RUN", "turn": 2, "request_id": "REQUEST", "revision": 0,
  "physical": {"clock_id": "recording-en01", "reviewed": true, "speech_end_s": 1.0, "first_audio_s": 2.0}
}
```

Duplicate sample/correlation IDs, wrong hashes and malformed plans are rejected. Ambiguous speech rows, mismatched revision/end timestamps, stopped/failed replies and invalid timing remain unsuccessful evidence. Unbound response events and malformed metric rows are counted; no earliest/fastest event is selected from duplicates. Reported physical timing on an explicitly failed trial stays visible while completed-only percentiles exclude that trial and its failure blocks the target result.

## Reading the result

Queue-estimate and supplied-physical distributions are separate, with total, EN/IT, direct-answer and delegated-work counts. Nearest rank sorts the valid completed values and selects one-based rank `ceil(p/100*n)` for p50/p90. Fractional milliseconds are retained. Missing evidence is null/unavailable, never zero. Counts preserve the full plan, excluded categories, reported outcomes, invalid/missing/failed/not-attempted samples and unbound events.

`physical_latency_targets` is met only for a plan declared physical, at least 30 eligible turns, both languages/categories, complete valid reviewed physical evidence for every eligible turn, no invalid/ambiguous/unbound evidence, p50 <=1500 ms, p90 <=2500 ms and delegated p50 <=1500 ms. Queue-only or synthetic data cannot satisfy it. `full_ticket_acceptance` remains not_assessed: complete-thought handling, contextual admission, meaningfulness, availability, all stage measurements and physical conversation still need their separate gates.

Per-turn components retain their source definitions. Hub wait includes endpoint/STT/delivery; it is not isolated agent queue time. Generation-to-sentence and sentence-to-first-chunk differences use one turn and one clock. Chunk-to-audio is a playback-queue estimate. Unattributable capture backlog and contextual assessment remain unavailable; the report does not guess them from run-level summaries or subtract component medians. No performance optimization is included in this preparation.

## Preserved old baseline

At [b575016, Agent SDK notes](https://github.com/pumpkit-io/jarvis/blob/b575016/docs/notes/phase-0-spikes.md), the fixed ten prescribed VoiceMode attempts, including recognition mistakes, gave p50 5.455 s and p90 5.877 s. All thirteen exchanges, including initial blank audio, an extra Italian correction and closing, gave p50 5.473 s and p90 7.712 s. The extra correction took 66.23 s, including approximately 61.88 s in the analyzer's STT interval; it remains a separate failure outside the fixed prescribed sample. No missing-hook fallbacks, dropped calls or unusable log lines were reported.

The analyzer estimates speech end by subtracting the logged 1000 ms silence tail. This is not sample-accurate physical alignment or an automatically comparable new-Hub measurement. These reviewed aggregate values and their sample definition are repeated in the report; raw hooks/transcripts are neither copied nor rerun. The retained local baseline metadata supplies provenance, not new evidence. Actual #43 acceptance remains open pending integrated measurements and the User's physical session.
