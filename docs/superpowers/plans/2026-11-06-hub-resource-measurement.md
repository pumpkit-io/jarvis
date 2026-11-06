# Hub resource measurement implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this bounded plan directly in the t42 worktree. Steps use checkbox syntax for tracking.

**Goal:** Provide reproducible external sampling of the actual Hub and its process tree, while leaving final target-hardware readiness, idle CPU and memory acceptance unmeasured.

**Architecture:** A standard-library command attaches to an explicitly supplied Hub PID; it never starts Jarvis, models or audio. macOS/Linux `ps` supplies process identity, RSS and cumulative CPU for one system snapshot per interval. The observer retains discovered descendant identities after reparenting, records per-sample membership and computes a sampled simultaneous RSS peak and CPU deltas. Existing component benchmarks remain separate references.

**Tech stack:** Python 3.12 standard library, system `ps`; optional manually invoked macOS `footprint` remains separate evidence rather than a per-interval blocking command.

**Spec:** GitHub #42 and parent #26, particularly whole-Hub accounting, injected seams and separately recorded physical acceptance.

## Global constraints

- No physical audio, Claude, model loading, cloud/GitHub calls or live acceptance runs in implementation/tests.
- No change to selected recognition behavior, startup ordering, background Talker warm-up, text-only engine loading or bounded candidate behavior.
- Do not add overlapping RSS/Metal/cache/physical-footprint numbers. Sampled aggregate RSS is not an audio-stack footprint budget verdict.
- Local per-process samples stay under `.jarvis/`; no command lines, environment or transcript capture.
- Final selected configuration and target-hardware measurements remain outstanding. No component benchmark or synthetic test closes #42.

## Review focus

- PID reuse must not attach an unrelated process to the Hub's tree: identify by PID plus process start time.
- A known child reparented after launch remains observed, while an unrelated process with another identity is excluded.
- A child born and exited between polls is unobserved: document this limit and never claim an exact lifetime peak.
- Missing root, malformed system output or failed sample must fail visibly rather than report zero usage or pass.
- RSS is a simultaneous process sum, not the sum of individual peaks; shared pages may be counted in multiple processes. CPU sums can exceed 100% on multiple cores.

## Task 1: process-tree observer and report

**Files:** Create `src/jarvis_hub/resources.py`; create `tests/test_resources.py`.

**Public seam proposed for root review:** `sample_resources(pid, duration_s, interval_s, snapshot, now, sleep)` returns a JSON-compatible aggregate report, with a supplied whole-system process snapshot adapter. The command `python -m jarvis_hub.resources --pid PID --duration SECONDS --interval SECONDS --output .jarvis/resources/RUN.json` uses the real adapter. This is a measurement-adapter seam adjacent to #42's existing Hub/adapter testing decision; request root agreement before tests.

- [ ] Write one failing observer test with injected complete process snapshots/clock: root plus changing children, simultaneous RSS peak distinct from sum-of-peaks, cumulative CPU delta and observed child departure.
- [ ] Implement the minimal identity-aware tree observer, sample metadata and report schema. Include root/child RSS separately, timestamps/actual intervals, sampled peak, CPU coverage and explicit unobserved-transient limitation.
- [ ] Add one failing case at a time for reparenting, PID reuse, missing root, incomplete/failed samples and invalid duration/interval; implement each through the public observer seam.
- [ ] Exercise the command with a synthetic snapshot adapter or harmless Python child process only, verifying useful JSON, no command-line/env capture and nonzero errors. No invocation of Jarvis or audio engines.
- [ ] Test the `ps` adapter with controlled output including day/hour CPU formats and process disappearance. Tool/OS/Python version and sampling metadata accompany every report.

## Task 2: measurement instructions and existing loading regression

**Files:** Create `docs/notes/hub-resource-measurement.md`; modify `docs/notes/voice-hub-verification.md`; extend `tests/test_text_mode.py` only if existing coverage does not already demonstrate no audio engine construction/loading.

- [ ] Document selecting the actual Hub PID (not uv/launcher), workload labels, cached startup timing from launch to existing usable status/nonverbal cue, and separate model warm-up/readiness evidence. The observer attaches after startup; its report explicitly excludes startup and cannot certify the ten-second gate or startup peak.
- [ ] Document sampling with process lifecycle limits, minimum idle workload duration, one-core CPU interpretation, interference and measurement overhead, and recording actual tools/versions/configuration/revision separately.
- [ ] Describe separate audio-stack footprint/MLX observations and total Hub+children RSS. Never derive audio memory by subtracting a text process or add RSS and Metal. The live audio footprint and approximate 4 GiB comparison remain unmeasured until a justified whole-path measurement is available.
- [ ] Inspect existing lazy engine initialization and bounded caches/queues; add a text-only injected-seam regression only if it adds meaningful missing coverage. Fix resource lifecycle only if a concrete failure is reproduced; do not speculate about engine replacement.
- [ ] Run focused tests, then `uv run pytest -q` and `uv run pyright` once at integration. Record all skipped physical/model tests and remaining gates.
- [ ] Send root the complete diff and verification. No push or PR until root review; later draft PR must say preparation is implemented and final configuration/hardware gates remain open.

## Execution record

Root approved the measurement-adapter seam and attach-only first slice before implementation. The observer, CLI, documentation and text-only loading regression are implemented. The attach-only limitation remains: no launcher/PID signaling or startup measurement path was added because final configuration and separately authorized live measurement are still pending.

Vertical checks were observed failing before implementing process accounting, input limits, parser, CLI, record bounds, CPU coverage and mean CPU reporting. Reparent/PID-reuse and existing text-only behavior were checked as preservation cases. Final local verification: 1678 passed, 18 skipped; Pyright 0 errors/warnings; diff whitespace check clean. A harmless sleeping Python process was the only live sampling target. No device, Claude/model, auth or cloud run occurred. Root review and any later draft PR remain pending; no push has occurred.
