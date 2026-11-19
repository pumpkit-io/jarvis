# Reference Client

`reference_client.py` is an executable Python Client for [protocol v1](../docs/protocol/client-v1.md). It pairs, streams JSON events to stdout, sends typed lines, uploads a WAV as a Talk, and saves received speech clips as playable WAV files. It uses only Python 3.12 and `aiohttp`; it does not load speech models or open a microphone. Running a typed line or Talk against a real Hub does spend model usage.

From this checkout, use `uv run --frozen python tools/reference_client.py`. On another machine, copy this file and install `aiohttp>=3.14.5,<4` into a Python 3.12 environment, then run `python reference_client.py`. The Hub must already be running with remote access configured. Use its actual Tailscale HTTPS origin; plain HTTP is accepted only for loopback tests. This tool neither starts the Hub nor configures Tailscale.

## Pair and send input

Generate a pairing code on the Hub, then enter it at the hidden prompt:

```sh
uv run --frozen python tools/reference_client.py pair https://YOUR-MAC.YOUR-TAILNET.ts.net --name 'Reference CLI'
uv run --frozen python tools/reference_client.py say 'How is the refactor going?'
uv run --frozen python tools/reference_client.py talk /absolute/path/utterance.wav
```

`--code CODE` is available for automation; interactive entry avoids exposing the code in shell history and process arguments. Pairing output never prints the code or device token. Credentials default to `~/.config/jarvis-reference/credentials.json`, written atomically with mode 0600. The event cache sits beside that file, separately keyed by origin and Client identity. A refused/revoked token is erased and the command exits with a request to pair again.

Global options go before the subcommand:

```sh
uv run --frozen python tools/reference_client.py \
  --credentials /tmp/reference-test/credentials.json \
  --output /tmp/reference-test/clips --timeout 180 \
  talk /tmp/utterance.wav
```

Choose credential, cache and output locations outside tracked files. Do not commit these files. The defaults are outside the checkout. The cache contains conversation text and shown details; treat it as private. New credential/cache directories use mode 0700, and credentials, cache and saved audio use mode 0600. This reference tool deliberately saves clips for inspection. The Hub and phone continue to keep their live audio only in memory.

Input must be an uncompressed 16,000 Hz mono PCM16 WAV containing more than zero and at most 300 seconds of audio (9,600,000 PCM bytes). The tool validates the format and complete frame count, strips the WAV container, and uploads byte-identical raw PCM on each retry. Convert other recordings explicitly:

```sh
ffmpeg -i INPUT -ar 16000 -ac 1 -c:a pcm_s16le OUTPUT.wav
```

Reply clips are individual 24,000 Hz mono PCM16 WAV files, named using a safe encoding of the speech identity and clip index. Their paths and protocol reply/stream identities are printed in `saved_clip` records. On macOS, `afplay /path/from/saved_clip.wav` plays one. Saving or receiving a clip never sends a playback report claiming that the User heard it; close the one-shot command when finished to release the remote floor.

## Observe and reconnect

```sh
uv run --frozen python tools/reference_client.py --timeout 600 listen
```

All subcommands consume WebSocket events concurrently with HTTP operations. `say` waits for the matching final reply; `talk` also waits for matching speech to end when speech is enabled. `listen` prints events until interrupted or its deadline expires. Text deltas are emitted immediately as JSON lines, rather than held until reply completion. A deadline before the canonical reply completes reports uncertainty; inspect history before starting a replacement request. Once the canonical Talk reply succeeds, missing or unfinished audio at the same deadline produces a successful command with an explicit audio outcome and the available metrics. It does not discard the recovered text or start another Talk.

Disconnect the network during a reply, then restore it. The Client reconnects with exponential backoff and jitter, completes replay and fresh presence, and resumes from its durable cursor. Every event is cached before advancing that cursor and streamed at most once. Restarting `listen` also recovers missed text. `history` explicitly prints the cached events offline without a new model call; after uncertainty, reconnect with `listen` to recover canonical text, then inspect it with `history` before starting a replacement request. A lost in-flight WebSocket command is resent with its original identity and payload; a lost upload response retries the same Talk, command id and body within 30 seconds of the first attempt. It never queues fresh input while disconnected. `outcome_unknown` instructs you to reconcile history, rather than creating a new request automatically.

Live speech is unavailable on reconnect. A Talk whose reply or stream was already active on the lost connection, or whose reply is recovered through replay, finishes with `audio_status: unavailable` (or `partial` when matching clips were saved) and `audio_reason: disconnected`. A new live reply after reconnect still waits for its own speech ending. An incomplete header/binary pair is discarded without advancing past its header, and completed clips are validated against their stream, index and byte count. The tool does not automatically synthesize missed speech or replay saved clips. Replayable text survives sequence gaps; replay completion safely advances over unavailable live events. A separate cache is kept for each pairing identity.

## Latency instrumentation for E5

Each successful `talk` prints an `upload_metrics` JSON record. All times use the local monotonic `perf_counter` clock and are comparable only within that process:

- `release_upload_start` is when this reference tool releases its prepared WAV for upload, after `talk_begin` succeeds. It is a software proxy for a Talk release, not a physical phone gesture.
- `body_handoff_end` is recorded after the owned aiohttp payload awaits both body write and transport drain. It is local handoff completion, not confirmation that the Hub received the last byte. The `on_request_chunk_sent` trace hook runs before writing/draining and is deliberately not used.
- `first_matching_clip_received` is sampled when a complete validated binary clip arrives. Matching uses the uploaded Talk's returned request id, canonical reply events and the speech stream/reply identity. Earlier Announcements and old request streams are excluded, even when the clip arrives before the HTTP result or before its text event.
- `release_to_first_clip_seconds`, `handoff_to_first_clip_seconds` and `upload_duration_seconds` report both latency boundaries and upload duration separately. `http_response_end` is diagnostic only. `attempts` keeps each retry's start and handoff; the summary uses the first completed body handoff, so retry time remains in release latency.

`upload_metrics.reply_id` identifies the canonical reply even when no clip arrives. `audio_status` is `complete`, `unavailable`, `partial`, or `not_requested`; `audio_reason` is null on complete audio, `disconnected` for lost live audio, `deadline` when audio did not finish within the command deadline, or the protocol speech-stop reason. `partial` preserves actual matching first-clip measurements and saved clips while recording that the audio did not finish. The command waits for a legitimately late first clip and every subsequent clip until its owned stream ends or the configured command deadline expires; it adds no speech duration limit. It never synthesizes or plays missed audio automatically. A missing matching clip produces null timing values. These are clip-receipt measurements, not speaker/AirPods playback measurements. The reusable `ReferenceClient.upload()` and `UploadTiming.report()` interfaces keep E5 on this same workflow. Collecting 20 Wi-Fi and 20 LTE runs, physical release/playback evidence, medians, spread and a compression decision belongs to #116 and remains opt-in, Waiting on User. Default loopback tests use fake providers and temporary files; their timing values are not real device results.

## Local proof without providers

```sh
uv run --frozen --offline pytest -q tests/test_reference_client.py
uv run --frozen --offline pyright tools/reference_client.py tests/test_reference_client.py
```

The suite executes the CLI against the real loopback Door with fake Hub agents and audio. It also exercises delayed HTTP responses, early and unrelated clips, identical upload retries, and reconnect during a streamed reply. Executable Talk regressions cover recovered canonical text with lost or partial audio, late synthesis, synthesis without a live stream, and WAV rejection through the command interface. No real Hub launch, device, account or model is used.
