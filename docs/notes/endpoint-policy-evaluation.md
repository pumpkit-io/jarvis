# Endpoint replay: no qualifying policy selected

Ticket #34 is **partial and not ready**. The complete-thought behavior gate remains unfulfilled. This change supplies a repeatable frame/lifecycle comparison and failed results; it does not implement the ticket's complete-thought behavior or change active endpoint settings. None of the tested operating points is selected for runtime use.

## Historical replay and privacy

The replay feeds timestamped 10 ms PCM through the actual `InputPipeline`, native Silero VAD, native Smart Turn v3.2, Parakeet and the pure voice lifecycle. A scripted reply finishes 400 ms after each sent request, allowing a premature first fragment to expose a second request. The replay advances voice deadlines with `Tick`: deadlines earlier than new input expire first, while input ready exactly at a deadline wins the event race. A request that starts inside the Follow-up window keeps its continuation after that window would otherwise end. This is a controlled lifecycle condition, not a measured model reply. No microphone, speakers, conversational backend or GitHub mutation is involved. Wake detection is an inert injected detector; these cases start listening deliberately and do not measure wake recall or conversational directedness.

The historical development material was the existing eight genuine pause recordings, four completed controls and 30 recorded commands, including ten longer requests. The private validation worktree and recordings were only read. Personal transcripts and diagnostic dumps remain under this worktree's gitignored `.jarvis/endpoint/`; tracked results contain reviewed counts, timing distributions and failing sample identifiers. No recordings or personal transcript hypotheses are published. These native aggregate measurements were produced before the merged capture-queue fence (#55); they are historical policy comparisons, not a rerun of the current Hub.

The replay differs from the prior offline score table by using actual runtime VAD, audio cropping and turn ownership. It confirms premature commitments rather than assuming the old model scores establish runtime behavior. All 42 development samples were processed; no failed input or excluded take was dropped from these denominators. One command (`en07`, a farewell sample) has zero admitted work requests under the existing lifecycle. That missing work-request count is reported separately and is not treated as a valid response-latency sample; its endpoint remains in the endpoint timing count. No response audio is generated or response-latency pass claimed by this replay.

The independent set has 12 synthetic EN/IT cases authored without access to model outputs or development tuning: four completed controls, four incomplete-prefix continuations, two ambiguous continuations and two repeated-pause cases. Labels and text were frozen first. Candidate policies were frozen before held-out inference. Kokoro synthesized individual segments with the declared pauses; these generated pauses are controlled probes, not independently recorded human prosody. A complete-looking prefix followed by future speech is labeled `cannotinfer`, not a semantic error that a text model could know in advance.

## Historical development comparison

End-wait is measured from runtime VAD's final speech stamp to endpoint commitment in audio time. p50 is the median; p90 uses nearest rank. These numbers exclude STT, model generation, playback and device delays. Premature fragments are counted explicitly; their short waits are not evidence of successful latency. All compared models and runtime dependencies retain their existing versions.

| Policy | Genuine pauses cut / 8 | Control wait p50 / p90 |
|---|---:|---:|
| Current Smart Turn v3.2, threshold 0.5 | 5 | 507 / 508 ms |
| Acoustic threshold 0.985, two-second maximum silence | 1 | 1255 / 2008 ms |
| Acoustic threshold 0.987, 1.5-second maximum silence | 0 | 1005 / 1508 ms |
| Acoustic/text joint threshold 0.5, two-second maximum | 4 | 508 / 2006 ms |
| Acoustic/text joint threshold 0.8, 1.5-second maximum | 0 | 1507 / 1508 ms |
| Fixed 1.5-second silence | 0 | Every endpoint waits 1500–1508 ms |

A review found that the first replay version never emitted `Tick`, incorrectly leaving a Follow-up window open across a ten-second gap. The corrected replay admits only the first request in that regression and reaches idle; a separate exact-deadline regression preserves newly arriving speech. Ninety native reruns (the 42-sample baseline and all 12 held-out samples under each of four compared policies) confirmed identical request counts, premature-commit flags and endpoint waits for the existing short-pause results. These reruns check the lifecycle correction; they are not fresh independent model validation.

The current policy also cut one of ten longer requests into two requests. Its eight genuine pause recordings yielded 13 requests, instead of eight. The apparently successful conservative development points did not generalize.

The fixed 1.5-second comparator had no premature commitments among all 42 development recordings, with all-endpoint p50 1504 ms and p90 1508 ms. It imposes its longest wait on every completed turn and therefore does not satisfy the adaptive requirement. It also cut seven of 12 independent synthetic cases, showing that it is not a complete natural-pause solution.

## Local text candidate and independent results

[Namo Turn Detector v1 Multilingual](https://huggingface.co/videosdk-live/Namo-Turn-Detector-v1-Multilingual/tree/59245aac1f0a0f170277ebecc32ddaea418083b4) declares Apache 2.0 and English/Italian support. Its quantized ONNX model and tokenizer run locally on CPU without credentials or a service. The reference snapshot is `59245aac1f0a0f170277ebecc32ddaea418083b4`, about 310 MiB including tokenizer/resources. It receives local Parakeet text; no candidate speech or transcript is sent to the weight host. Evaluation uses optional `tokenizers==0.23.3`, leaving runtime dependencies unchanged.

This is a completion classifier, independent of #31's addressee experiments. It has no authority to admit a turn, execute work or grant an Approval. The experiment's joint score is the minimum of acoustic and text scores; text is evaluated only when the acoustic score reaches 0.5. The fused comparison ends early when acoustic completion reaches 0.987 or text completion reaches 0.9. Its binary decision scores are experimental decisions, not native Smart Turn probabilities or calibrated confidence. The Namo adapter reads class index 1 as complete, matching the published ONNX interface. Verifying that mapping does not establish classifier accuracy.

| Frozen independent policy | Incomplete-prefix cases cut / 4 | Repeated-pause cases cut / 2 | Four completed controls |
|---|---:|---:|---|
| Acoustic threshold 0.987, two-second maximum | 4 | 2 | Two near 500 ms, two near 2000 ms |
| Joint threshold 0.8, two-second maximum | 3 | 2 | All near 2000 ms |
| Strong-signal fused comparison, two-second maximum | 4 | 2 | Two near 500 ms, two near 2000 ms |

These counts describe pipeline outcomes, not standalone Namo or acoustic-classifier error rates. The synthetic `en06` request transcript says “Could you check weather?” where the authored prefix says “Could you check whether”. Parakeet also adds terminal punctuation to incomplete synthetic prefixes, including `it03`. This can change the linguistic evidence available to a text classifier; an end decision on a transformed transcript does not isolate a classifier mistake on the intended words. Counterfactual scoring of corrected transcripts is not claimed here.

The corrected replay reports each commit's cause. Across premature first commits in the held-out set, the acoustic and fused policies each have four completion-score commits and two forced silence fallbacks. The joint-margin policy has three combined-score commits and two silence fallbacks. For example, `en04` and `it04` have joint scores below 0.8 but still commit at the two-second fallback. The fixed comparator's seven premature commits are fixed-silence decisions. A score-based commit can still involve STT errors, and a fallback commit is not evidence that the classifier called the prefix complete.

All three policies happened to preserve the two ambiguous cases. That does not establish an ability to predict unobserved future continuation. Both endpoint prediction and the maximum-silence fallback can contribute to cuts; a correct incomplete assessment is insufficient when a fallback commits before resumed speech is detected. No held-out case was used to retune these operating points. There were zero file/processing failures in these comparisons.

The [complete aggregate](../../spikes/audio/results/endpoint-comparisons.json) preserves other commands, longer requests, missing/zero-request identifiers and all failed case identifiers. Native inference times in optional full replay output are component timings; the replay does not establish meaningful first-audio percentiles or physical acceptance. Synthetic speech cannot establish the quality of a replacement detector on the User's real voice.

The [LiveKit multilingual text detector](https://huggingface.co/livekit/turn-detector) was screened but not downloaded: its [model license](https://huggingface.co/livekit/turn-detector/blob/main/LICENSE) restricts standalone use and requires the LiveKit Agents framework. Adding that framework is outside this focused repair. A stalled Xet download of Namo was stopped and recovered using ordinary HTTP; transport failure was not treated as model-quality evidence.

## Text-only completeness: faster readout, failed independent gate

A separate frozen linguistic-closure study used public synthetic EN/IT text, not the personal recordings. Each DEV/heldout set has 16 scenarios but only 14 unique observed inputs, including repeated complete-looking prefixes whose future continuation is unknowable. Only observed_text and available linguistic_context reached the worker. Case-specific punctuation provenance, expected labels, IDs, rationale and future-scenario metadata were excluded. Teaching examples and policy stayed fixed; no transcript spelling rescue was used.

The 0.6B model labeled every DEV input COMPLETE. A cached Qwen3-4B-Instruct-2507-4bit screen improved late raw labels to 15/16 but missed the fixed 150-ms deadline on every case. Moving experiment-only prefix-cache SHA verification outside the decision timer left real tokenization, cache copying, IPC, generation and parsing timed; 12/16 then arrived on time. A version-pinned last-position readout avoided unnecessary subsequent-token generation while retaining the same native A/B/C token decision, teaching examples and model. All 16 DEV decisions arrived within 150 ms, median 75.4 ms and maximum 133.6 ms; raw labels were unchanged. Public cached/full-prompt parity and immutable prefix checks passed.

The exact last-position candidate then ran the independently frozen heldout once, with no prompt/model/deadline changes: all 16 arrived within 150 ms (median 75.4 ms, maximum 134.8 ms), but one of four clearly unfinished observations became COMPLETE and one of eight completed scenarios became INCOMPLETE. Three of four uncertain references stayed UNCERTAIN; one became INCOMPLETE and none COMPLETE. This is 13/16 scenario matches or 11/14 unique observations. The false completion fails the endpoint quality gate, while the false incomplete would stall a finished request. No retry or heldout tuning followed.

The [textual aggregate](../../spikes/audio/results/textual-completeness.json) preserves raw and deadline-effective confusion matrices, all timeout counts, source/freeze hashes and method boundaries. Native templates, tokenizers and readout/instrumentation differ, so this is not a capacity-only experiment. The maintained MLX model/generation code supports the last-position calculation, but model-specific integration remains experimental. Full decision-plus-diagnostic heldout cycle median 141.8 ms/p95 206.1 ms includes the separately recorded post-decision hash; the parent waited for that acknowledgment before another request. These component decisions establish neither continuous input throughput nor whole-Hub latency. MLX peak 3.074 GB and process RSS peak 2.473 GB are separate overlapping component measures, not additive or a whole-stack budget pass.

## Current harness and remaining gate

The benchmark carries forward the reviewed deadline fix from source commit 60d6312: earlier deadlines expire before later input; input ready exactly on a deadline wins. Score-based versus forced-silence commitment is attributed from captured-through timestamps. In the current capture API, a repeated capture(True) can retire an active turn, so the port synchronizes capture only when the enabled state changes, matching VoiceMode. Six injected-engine regressions cover split/retained requests, empty input, timeout expiry, exact-deadline continuation, cause attribution and percentile definitions. No native models or old personal recordings were rerun for this port.

Merged #55 separately fixes frame/endpoint FIFO commitment ordering and bounds inference backlog. This pure synchronous audio-time replay does not recreate Capture's worker queue, physical buffering or the admitted-request scope resolver. It must not be presented as fresh validation of those paths. Existing Hub and input tests retain their own behavior contracts.

The remaining #34 gate is genuine and independent mid-sentence pauses retaining their opening and continuation as one request while finished controls have a prompt response path. Historical conservative acoustic/Namo points failed independent synthetic cases; the new text-only candidate failed its independent linguistic gate. No endpoint predictor, threshold, silence default or runtime completeness adapter is adopted. The current capture epoch, duration/retention status and #55 protections remain authoritative; this evidence does not replace them. Postcommit continuation/admission work remains #38/#40, and no closure classifier can predict an unseen qualifier. Fresh runtime pipeline, engine coexistence, independent references and physical acceptance remain pending. The heldout evidence is now spent and cannot be retuned then claimed independent again.

## Reproduce without devices or subscription turns

From the repository root, reuse the existing private recordings directory as `--data`; do not copy it into tracked files. `--out` contains aggregate timing/count evidence only. The evaluator creates no Tasks or real agent sessions.

```sh
uv run python spikes/audio/bench_endpoint.py --data /path/to/existing/recordings --method baseline --out .jarvis/endpoint/baseline.json
uv run python spikes/audio/bench_endpoint.py --data /path/to/existing/recordings --method acoustic-margin --extension-ms 1000 --out .jarvis/endpoint/acoustic-development.json
uv run python spikes/audio/make_endpoint_samples.py --out .jarvis/endpoint-heldout
uv run python spikes/audio/bench_endpoint.py --data .jarvis/endpoint-heldout --method acoustic-margin --out .jarvis/endpoint/heldout-acoustic.json
uv run python spikes/audio/bench_endpoint.py --data .jarvis/endpoint-heldout --method fixed-1500 --out .jarvis/endpoint/heldout-fixed.json
```

Download public Namo weights separately, then run completely local inference with that directory. New replay outputs name the expected reference revision separately from SHA256 hashes of the local `model_quant.onnx` and `tokenizer.json` files and the class-1 softmax scorer version. A compatible custom directory is permitted; its actual upstream revision is not inferred from its path. Historical aggregate artifacts retain their original provenance and are not rewritten as newly verified file-hash measurements. This replay never downloads or sends text to a remote assessment service:

```sh
HF_HUB_DISABLE_XET=1 uv run python -c 'from huggingface_hub import snapshot_download; print(snapshot_download("videosdk-live/Namo-Turn-Detector-v1-Multilingual", revision="59245aac1f0a0f170277ebecc32ddaea418083b4", allow_patterns=["model_quant.onnx", "tokenizer.json", "LICENSE"]))'
HF_HUB_OFFLINE=1 uv run --with tokenizers==0.23.3 python spikes/audio/bench_endpoint.py --data .jarvis/endpoint-heldout --method joint-margin --model /path/to/snapshot --out .jarvis/endpoint/heldout-joint.json
HF_HUB_OFFLINE=1 uv run --with tokenizers==0.23.3 python spikes/audio/bench_endpoint.py --data .jarvis/endpoint-heldout --method fused --model /path/to/snapshot --out .jarvis/endpoint/heldout-fused.json
uv run python -m pytest -q spikes/audio/test_endpoint_benchmark.py
```

A fresh runtime environment may first need the existing public openWakeWord resource download (`uv run python -c 'import openwakeword.utils; openwakeword.utils.download_models(["hey_jarvis"])'`) for its native Silero model. This is a setup requirement, not a successful endpoint sample. The ordinary regression tests inject engines and never download models.

## Evidence-port verification

The evidence-only port to main passed six injected endpoint replay tests, the full default suite (1,657 passed, 18 skipped), and Pyright (zero errors/warnings). Current source was selected through PYTHONPATH with initialized dependencies and no sync. No native audio/model inference was repeated for this port. The default pytest paths omit opt-in spikes, so the six replay regressions were run explicitly in addition to the full suite.
