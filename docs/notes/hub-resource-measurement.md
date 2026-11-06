# Whole-Hub resource observation (#42)

The resource observer attaches to a running Hub and samples that actual process and its observed descendants. It does not start Jarvis, initialize audio, warm models or invoke Claude. It uses Python's standard library and the operating system's `ps`. Existing `spikes/audio/bench_memory.py` and `bench_claude_idle.py` remain component measurements; they do not establish the selected configuration's whole-Hub footprint.

No target-hardware startup, idle CPU, audio-stack footprint or final whole-Hub peak has been measured by this change. The final recognition configuration is still a prerequisite for accepting #42. The observer is preparation for those runs, not a budget pass.

## Run a local observation

Run the Hub only when the physical-audio and subscription warm-up run is authorized. After readiness and the background warm-up, identify the actual Python `jarvis-hub` process using Activity Monitor or a local `ps` inspection. Verify the PID belongs to this checkout. Do not select `uv`, the shell launcher, the observer or an unrelated Claude process. The observer trusts the supplied PID and cannot determine whether it is the intended Hub.

In another terminal, from the same checkout:

```sh
uv run python -m jarvis_hub.resources --pid HUB_PID --duration 1800 --interval 0.5 --workload idle --output .jarvis/resources/idle-run-01.json
```

Replace `HUB_PID` with the verified number. Use a new output path for every run; an existing file is never overwritten. `--workload` records `idle`, `interactive` or `synthetic`, rather than inferring activity from CPU. Output remains local and gitignored. The report contains numeric process accounting and process start times, not command lines, environment, audio or transcripts. Review aggregate results before sharing them.

Record the checkout revision, hardware/memory, macOS version/build, dependency versions (`uv tree`), CLI version, selected engines/model revisions, efforts, devices, cache settings, headphones/speakers and workload manifest alongside the local result. The result itself records Python and OS versions, architecture, resolved `ps` path and queried fields. macOS supplies `ps` with the OS rather than a separately versioned package. The adapter requests `pid,ppid,lstart,rss,time` with the C locale; local `man ps` defines RSS in 1024-byte units and `time` as accumulated user plus system CPU. No `-S` exited-child accounting is requested.

For idle CPU, finish warm-up, downloads and pending Tasks/news first; keep the prescribed 30-minute idle window and inspect the existing metrics separately for unwanted model turns. For an active memory run, use `--workload interactive` and a fixed reviewed workload including EN/IT requests, generation/playback, recognition candidates and child-process startup/shutdown. Record any interference from other development/GPU work. Neither workload label nor low CPU proves idle behavior.

## Read the report

- `sampled_peak_tree_rss_bytes` is the largest simultaneous root-plus-observed-children RSS sum. It is not the sum of each process's independent peak. `sampled_peak_root_rss_bytes` and each sample's root/children RSS remain separate.
- RSS counts resident mappings. Shared pages can appear in several processes' RSS; the tree sum is an accounting measure, not a unique physical-memory total. It does not include all Metal allocations and must not be added to MLX active/cache/peak or `phys_footprint`.
- `observed_interval_cpu_s` sums CPU-counter differences only for identities present in consecutive snapshots. `observed_mean_cpu_percent_one_core` divides that observed CPU by `cpu_observation_window_s`, between the first and last observations. A value of 100% means one core; several busy processes may exceed 100%. These are lower bounds when process lifetimes are missed.
- `cpu_s_at_first_observation` records the separate lifetime CPU counters first seen for the root and discovered children. They can include work from before attachment or discovery and are never treated as interval CPU. Each sample exposes first-seen CPU, departed process count and whether observed membership changed. Even unchanged membership cannot rule out a child born and exited between samples.
- Each process is identified by PID plus its `lstart`. Previously observed descendants remain tracked after reparenting. A reused PID with a different start time is excluded unless newly discovered through a tracked parent. `lstart` has one-second precision, so same-second PID reuse cannot be distinguished. Children which detach before ever being observed cannot be attributed to the Hub.
- `elapsed_s`, `interval_s` and `snapshot_duration_s` record actual timing. The requested interval is not a guarantee. `ps` traverses processes rather than taking an atomic memory snapshot; small races, process exits and quantized CPU counters limit precision. The observer's own process is normally outside the supplied tree, but its system load and `ps` overhead still affect the measurement. Check these durations and repeat with a larger interval if polling interferes.

Duration is bounded to 0.1–3600 seconds and interval to at least 0.1 seconds, with at most 10001 requested snapshots and 100000 retained process records. Excessive requests fail; a long run with many descendants may need a larger interval or shorter blocks. A missing initial PID, invalid snapshot, failed/timed-out `ps` or regressing CPU counter aborts rather than inventing zero usage. A failed observation is not evidence of compliance. Check command exit status; a file from an interrupted or failed write is not a completed result.

## Startup, audio budget and remaining gates

`startup_included` is always false: attachment excludes launcher/import/model-loading time and any earlier memory peak. Time a fresh cached launch independently, from invocation to the usable listening/speech readiness status and nonverbal signal. Record speech, listening and model readiness separately; the Talker still warms in the background. Do not time a conversational response or report only `ears_loaded.load_ms` as full startup. Target usable cached readiness within ten seconds; a failed resource initialization must remain a failed readiness result with its actionable on-screen status.

For the approximate 4 GiB audio-stack budget, obtain a justified audio-specific measurement of the final operational stack and state exactly which process allocations it covers. The Hub also holds orchestration, language/runtime state and agent connections; its RSS is not an isolated audio-stack allocation. Do not subtract another process's text-only RSS to estimate audio use. macOS `footprint -p HUB_PID` can provide a separate physical-footprint observation using the existing spike method, but a slow point-in-time footprint does not establish an exact lifetime peak or isolate audio allocations. Report tool, time, units and scope; do not add it to RSS or Metal.

Before closing #42, record cached startup, the final configuration's sampled whole-Hub/child peak, separate justified audio-stack accounting against the approximate budget, and idle CPU with sampling/workload conditions. Include failures, root exits, missing child lifetimes, sampling gaps and any budget miss. Diagnose a measured avoidable loading/cache-growth defect before changing engines or reducing recognition behavior.

Source inspection for this preparation found existing lazy engine loading, a 256 MiB MLX buffer-cache limit in the STT/TTS paths and bounded capture/reference queues. It did not establish a new loading leak or measure final coexistence memory. Tests use injected process snapshots/clock and a harmless sleeping Python process; a text-launch regression rejects attempted audio construction/loading. None of these tests exercise physical devices or real models or close the live gates.
