# Cancel playback echo before inference queues capture

Ticket #32, 2026-11-05. The adapter replay reproduced a timing defect independently of any claim about the User's physical speaker failures.

Previously, `InputPipeline.push` cancelled echo on the same thread that runs Wake word, VAD, Smart Turn and STT. A blocking inference call stopped cancellation while the microphone continued queuing frames. Capture retains up to two seconds by default; the output reference retains only the latest half second. After a stall, old capture frames could consume newer reference blocks. Even without reference overflow, draining all currently queued playback before processing older microphone frames changes their temporal relationship.

The regression drives `SoundPlayer`'s real fill callback, `Capture`, `WebRtcEchoCancellation` and `InputPipeline`. Only the device stream, recognition and native DSP are scripted. The DSP subtracts the expected synthetic playback in chronological order, so the test checks adapter ordering rather than WebRTC's acoustic accuracy. Playback uses 24 kHz, 960-sample output blocks; microphone input uses timestamped 16 kHz, 160-sample frames. Inference is held while 1.4 seconds of playback crosses a sentence boundary, a fade on cancellation and subsequent silence. A second case adds a synthetic user correction during playback.

With the original capture implementation, the Jarvis-only case raised a conversational event and the overlapping correction did not arrive as the expected complete request. Both failed with the original code restored temporarily. With the change, playback-only capture raises no Wake word, Barge-in or completed request; overlapping user speech arrives once with its expected content. The equivalent headphones cases pass with echo cancellation disabled and no playback mixed into the microphone.

## Repair

`Capture` now cancels echo serially as each microphone frame arrives, before it joins the bounded inference queue. Inference sees already-clean frames through `InputPipeline.push_clean`; direct adapter replays can still use `push`, which performs cancellation synchronously. Echo processing continues even when the inference queue is full and drops an input frame, keeping the reference timeline advancing. Failed cancellation drops that frame and reports the error instead of admitting unprocessed playback.

Raw microphone silence remains distinct from cancellation output, preserving the notice for denied microphone access without mistaking successfully removed playback for a silent device. The existing playback buffer, fade behavior, reference buffer limit, WebRTC selection and headphones setting are unchanged. This repair does not introduce muting during playback or discard genuine overlapping user speech as an echo policy.

A local native WebRTC processing check used 1,000 synthetic 10 ms capture/reference pairs without devices: p50 0.068 ms, p99 0.093 ms, maximum 0.456 ms per capture processing call (NumPy linear percentiles). This supports placing cancellation before inference on the tested Mac; it is not a physical latency or cancellation-quality measurement, and excludes callbacks held by another thread or the operating system.

Reproduce the adapter checks without devices or model calls:

```bash
uv run pytest -q tests/test_capture.py -k playback_is_removed
uv run pytest -q tests/test_capture.py tests/test_input_pipeline.py tests/test_player.py
```

## Physical acceptance still pending

The connected AEC was not disconnected. This replay attributes a reproducible inference/backlog defect, not the entire reported live self-capture problem. WebRTC's adaptation, actual DAC/ADC delays, room acoustics, OS callback scheduling and residual echo still require speaker evidence. Output callback submission time is not actual speaker playback time. The provisional macOS voice-processing initialization failure remains missing comparison evidence; no replacement or quality conclusion follows from it.

When physical audio and conversational runs are authorized, use the existing voice verification checklist and opt-in local clip retention. Keep raw/post-cancellation/reference evidence and personal transcripts local. Compare built-in speakers with `./jarvis --headphones`, using matching requests and sentence-boundary, silence and cancellation transitions. Include a directed correction during playback and controlled inference load. Correlate playback-reference submission, capture timestamps/backlog and admitted input; report false events and dropped frames rather than treating a quieter output as success. The final gate remains zero self-interruptions over ten minutes of built-in-speaker speech. No such physical pass is claimed here.
