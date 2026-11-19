# E5: reference Client latency

Ticket #116, Spec #91. Status: **Waiting on User and provider setup**. No real Wi-Fi or LTE samples have been collected. Synthetic tests are software checks only and are never evidence in these results.

Wi-Fi: 0/20 samples, 0 matching first clips, 0 completed audio replies. Release-to-first-clip median and min/max: **unmeasured**. Local body-handoff-to-first-clip median and min/max: **unmeasured**. Upload duration median and min/max: **unmeasured**.

LTE: 0/20 samples, 0 matching first clips, 0 completed audio replies. Release-to-first-clip median and min/max: **unmeasured**. Local body-handoff-to-first-clip median and min/max: **unmeasured**. Upload duration median and min/max: **unmeasured**.

The 2.5-second release-to-first-audio target has not been verified on either route. Receipt of a complete clip is a software proxy and does not measure physical speaker or AirPods playback.

## Method

Keep the real Hub awake on its Mac and home connection. Run the reference Client on a separate machine/device with Tailscale access. For Wi-Fi, connect that Client machine to Wi-Fi. For LTE, connect the separate Client machine to the User's phone hotspot with cellular uplink, or use a cellular Client device. A Mac contacting its own Hub while joined to a phone hotspot is not evidence of an LTE roundtrip. The script records the User-supplied route label; it cannot verify the carrier route. Record Client/Hub machines, network/uplink, Tailscale routing, software revisions, provider/voice configuration and any physical playback method alongside the results before interpreting them. Keep the unrelated existing Tailscale Serve mapping unchanged.

Use the same ordered set of 20 prepared utterances on both routes, including English and Italian. Each input is an uncompressed 16 kHz mono PCM16 WAV, at most five minutes, accepted by the existing reference Client. Store a JSON array of exactly 20 paths in a private manifest; relative paths resolve beside that manifest. Repeating a path is allowed, but record that choice. The manifest order defines sample numbers 1 through 20. Prepared WAV release is a software proxy after `talk_begin`, rather than a physical Talk-key release.

The runner starts the reference Client's concurrent WebSocket reader before upload. It uses `ReferenceClient.upload()` and `UploadTiming.report()`, including the actual post-write-and-drain local body handoff timestamp. The HTTP result may arrive after a clip and is diagnostic only. Request/reply/stream correlation excludes older speech and Announcements. It waits for the canonical reply and matching speech completion, disconnection or the sample deadline; a deadline never fabricates a completed reply.

Each route begins with a 61-second quiet upload window and waits at least 6.1 seconds after each sample finishes before the next. This stays below the v1 limit of ten new uploads per minute and keeps one transcription at a time for the paired Client. Keep that identity exclusive during a run; do not create extra identities to bypass limits. Reference-client transport retries preserve the same Talk and idempotency key. A rejection, lost upload result or incomplete canonical reply records an uncertain sample and stops the batch. Reconcile cached history before any further input, including after a rate-limit rejection; do not automatically resend it as a new Talk. Missing audio after a completed canonical reply records unavailable/partial audio and may continue to the next sample.

## Explicit opt-in commands

These commands spend real model usage. Run them only after the User authorizes the measurements and the real Hub/providers and separate Client route are ready. No Hub launch or service configuration is part of this script. Pair using [the reference Client instructions](../../tools/README.md) first. On the separate Client machine, copy both `tools/latency_e5.py` and `tools/reference_client.py` into the same directory and install Python 3.12 plus `aiohttp>=3.14.5,<4`, or run from a checkout with the locked dependencies.

On Wi-Fi, with private paths outside the checkout:

```sh
uv run --frozen python tools/latency_e5.py run \
  --allow-model-usage --network wifi \
  --route 'CLIENT MACHINE; Wi-Fi uplink; actual Tailscale route' \
  --manifest /private/e5/utterances.json \
  --records /private/e5/wifi.jsonl
```

Move only the separate Client to the verified cellular route, then run:

```sh
uv run --frozen python tools/latency_e5.py run \
  --allow-model-usage --network lte \
  --route 'CLIENT MACHINE; PHONE hotspot cellular uplink; actual Tailscale route' \
  --manifest /private/e5/utterances.json \
  --records /private/e5/lte.jsonl
```

Replace route placeholders with actual observations. `--credentials`, `--clips` and `--timeout` override the defaults shown by `run --help`. The default sample deadline is 120 seconds, a measurement limit rather than a speech-duration limit. Choose a larger deadline before a run if needed and record it. The runner saves received clips without playing them or claiming they were heard. Credentials and cache defaults match the reference Client; E5 clips default to `~/.local/share/jarvis-reference/e5-clips`.

Each command exclusively creates its JSONL output with mode 0600, flushes every sample to disk, and refuses an existing filename. Keep the original output even after partial runs; an interrupted file can contain fewer than 20 samples. A batch interrupted before a sample outcome is written has no record for that attempt; reconcile before repeating it and preserve that fact in the run notes. JSONL records include UTC start time, run identity, source-file SHA256 hashes, PCM hash, route label, monotonic timing, protocol identities and audio outcome. These files and the reference Client's cache/audio are private; do not commit them. Record the checkout revision separately. Pairing tokens are never included in E5 records.

Offline report, without a Hub, providers or model usage:

```sh
uv run --frozen python tools/latency_e5.py report \
  /private/e5/wifi.jsonl /private/e5/lte.jsonl > /private/e5/report.json
```

The report separates sample, canonical-completed, first-clip, audio-completed, partial and missing-clip counts. Every metric has its own count, median, minimum and maximum. Missing timing remains null. Partial audio retains any actual first matching clip timing. A receipt-proxy assessment requires all 20 samples and all 20 matching first-clip timings for that route; a subset median cannot establish success. Physical release/playback still requires separate observation even with a full set. Duplicate network/sample records and mixed real/synthetic input are rejected. To report a partial run, pass that original file alone; do not combine an uncertain run with a replacement and silently treat it as one complete series.

Software tests invoke the public runner with a deterministic fake clock/measurement adapter and execute the report on synthetic JSONL. They exercise 40-record grouping, pacing, missing/partial outcomes, literal statistics, provenance rejection, opt-in and stopping after uncertain input. Existing reference Client loopback tests verify the actual upload boundary, early clips before the HTTP result and unrelated-stream exclusion. Neither suite contributes real values above.

## Provisional decision

Retain current PCM clips and full uploads pending real Wi-Fi and LTE evidence. There is no measured basis to open a compression or streamed-upload follow-up or claim the 2.5-second target met. After the authorized 20+20 runs, assess release latency together with upload duration, handoff latency, missing outcomes and physical playback evidence, then record whether retaining PCM/full uploads remains justified or a focused follow-up is needed.
