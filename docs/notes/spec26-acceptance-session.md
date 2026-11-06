# Spec #26 acceptance session

This is the preparation for #44, not a report that its checks passed. Microphone and speaker checks were explicitly deferred during unattended development. The final integrated configuration, independent references and physical measurements remain required. Existing #3/#4/#5/#21 obligations stay open.

## Freeze and preflight

Name the integrated commit, lockfile/dependency versions, macOS/hardware, Claude CLI/SDK, selected engines/models/revisions, prompt hashes, operating points and retention limits. Record headphones/AEC choice, devices, output level, distance, room and background load. Finish downloads before timing. Keep full environment/account data local.

Run the existing fake regression and type checks on that exact source tree, or reuse its fresh green results. Default tests open no devices and spend no model quota. Text mode still warms the Talker, so launching it is not a no-call preflight. The recording tool's preflight opens a microphone; do not run it during a device-free preparation.

Select a ready admission path before acceptance trials. A disabled, unavailable or failed classifier cannot establish directed-interruption behavior. Optional cloud admission requires explicit activation and may send candidate transcripts, including background speech, with bounded context; raw audio stays local. Confirm the actual provider/payload and inform anyone participating. Development/fake decisions do not prove semantic quality.

Preserve the existing recordings, old baseline and Orchestrator comparison. Rerun only gates invalidated by the selected configuration or a specific unresolved reproduction. Do not repeat the full recording sequence or overwrite old takes to manufacture evidence.

## Local evidence

Create a fresh local `.jarvis/human-checks/<run>/` folder. Before timing, freeze the attempted sample IDs, languages, categories, intended actions and exclusions. Runtime IDs can be bound afterward without changing eligibility. Record every attempt, retry, failure, missing audio and exclusion reason. Keep personal audio, full transcripts, hypotheses, account details and logs local; publish reviewed aggregates only.

Retained audio clips can be pruned by count, age and size limits. Preserve still-needed evidence before enabling clip retention. Ordinary postprocessed clips do not establish sample-accurate ADC/DAC or raw/reference alignment. Prove a common time basis for actual speech end and first request-specific output before claiming physical latency. If alignment is unavailable, that gate is unmeasured.

## Independent wake and commands

- Collect 20 fresh takes each of Hey Jarvis, Hi Jarvis, Ehi Jarvis and bare Jarvis under declared conditions. Require at least 19/20 for each phrase, plus at most one false wake over a fresh background hour. Used development recordings cannot serve as independent confirmation.
- Include wake-only and one-breath wake-plus-request takes, with a long opening beyond the ordinary pre-roll. Confirm one chime, no clipped opening and one intended request. A late confirmation must not revive an older typed request or interrupt playback.
- Independently adjudicate spoken references before assigning command errors. Use fresh EN/IT cancellation, negation, PR/CI vocabulary, repo names and numeric targets. Retain disagreement history; do not rewrite references to fit hypotheses.
- Report WER, critical-word/target retention, resulting action and clarification separately. Uncertain consequential wording must be clarified. A transcript guess cannot grant an Approval. A changed recognizer requires contextual-admission revalidation with its actual transcripts.

## Conversation and speakers

Run these checks with a human present. Compare targeted failures with headphones, keeping other conditions comparable.

- Ten minutes of Jarvis-only built-in-speaker playback, including pauses, sentence boundaries and transitions: zero self-interruptions. Preserve local raw/post-cancellation/reference timing for suspected echo; low residual energy alone does not establish attribution.
- Negative EN/IT scenarios during replies, Announcements and Follow-up windows: Backchannels, coughs/noise, unrelated dialogue with interrupt vocabulary, quoted/reported wake or stop words, multiple nearby speakers and the User addressing another person about the same topic. Require zero unwanted audible pauses, stops, admissions or work starts. Uncertain or unavailable assessment preserves playback.
- Positive cases: clearly directed voice-only stops, corrections without a Wake word, Backchannel then correction, ambiguous speech followed by a directed fragment, long corrections, thinking pauses and resumed speech around first audio. Confirm the accepted opening survives, obsolete replies stop, affected Tasks are resolved correctly, unrelated Tasks survive and each request is handled once. Report missed interruptions and onset-to-stop timing; no numeric interruption-delay gate was specified.
- Repeat relevant cases during Announcements and Quiet mode. Retain heard versus unheard news and avoid duplicate reports. Typed voice-mode input stays silent; whole end controls return idle silently; generated farewells finish once without a cached acknowledgment.

## Meaningful-response latency

Freeze at least 30 eligible EN/IT direct-answer and delegated-work attempts before scoring. A balanced 15 EN/15 IT and 15 direct/15 delegated plan is useful, but those splits are not additional Spec requirements. Keep warm-up, typed turns, Announcements, chimes and interrupted trials separately categorized. Never replace a slow primary trial with an easier one.

Measure actual end of speech to first request-specific audio. Targets are p 50 at most 1500 ms and p 90 at most 2500 ms, plus delegated acknowledgment p 50 at most 1500 ms. Use nearest rank, report the size of each distribution and retain all failed/missing attempts. Missing latency is unavailable, never zero. A percentile over survivors does not prove the complete workflow passed.

Capture/backlog, endpoint/fence wait, STT, contextual assessment, generation and TTS/playback remain separate per-turn evidence. Use the same `run_id`, request/revision and Hub turn where available. Do not mix monotonic clocks across launches or subtract component medians. Current `first_meaningful_audio_ms` is a playback-queue estimate and must remain distinct from physical timing. No filler or readiness cue counts as meaningful response.

After the block, existing `./jarvis stats --voice`, `--bargein` and plain stats with the recorded `--since`/`--until` bounds are read-only diagnostics. Keep their raw metrics locally. The fixed old ten-prescribed-prompt baseline and its separate long-delay failure remain comparison evidence under their original timing limitations; do not rerun them just to reconstruct context.

## Readiness, resources, idle and Tasks

- With models cached, time launch to usable model, speech and listening status. Target readiness is within 10 seconds, with background Talker warm-up retained. A speech tone alone is not whole-system readiness; first-download time is separate. Verify text-only mode loads no audio and unavailable status gives an actionable next step.
- Sample the actual Hub and descendants through the workload and eligible idle interval, recording tools/versions/cadence/units and missed-process limitations. Startup-excluded attachment does not measure startup peak or readiness. Report simultaneous sampled whole-tree RSS and audio-stack accounting separately; RSS, physical footprint, Metal allocation and cache overlap and must not be added. The approximate audio-stack budget is 4 GB; no numeric idle-CPU cap was specified.
- After warm-up, observe 30 minutes with no pending Tasks, Watchers, Checks, news or recovery work. Record model-turn counters and boundaries. Require zero conversational turns and zero presence prompts. If work arrives, retain that attempt and start a new eligible interval after it settles; do not subtract calls and declare the old interval idle.
- Run two concurrent read-only Tasks on distinct Org repos and verify both reports arrive. Record Task/repo IDs and begin/report times. For each retained role, verify the subscription route from safe account/init/rate-limit evidence, reusing valid prior evidence. A cost field alone establishes neither API billing nor subscription use. Record the admission classifier's provider separately. Keep credentials/utilization private and never exhaust usage to test recovery.

## Evidence ledger

For every gate, report passed, failed or unmeasured with the exact configuration, sample count, failures/exclusions, percentile method and reviewed aggregate evidence. Fake, component, saved-file, real-model and physical results retain their distinct conditions. No fake result substitutes for physical timing or independent semantic accuracy.

At preparation time, final speaker isolation, independent wake/command behavior, integrated directed interruptions,30-turn physical latency, cached whole-Hub startup/resources,30-minute real idle and concurrent read-only Task reporting remain unmeasured on the final configuration. Failed local recognition/completeness candidates remain failures; their component speed does not select them. Update this ledger only from actual evidence. Preparing this document does not close #44, #26 or earlier unresolved acceptance issues.
