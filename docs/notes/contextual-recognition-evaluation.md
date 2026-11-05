# Local contextual recognition (#31)

No tested operating point is selected for runtime integration. This ticket adds an isolated assessment contract and reproducible local experiments. It does not connect classifier decisions to conversational admission, Tasks, Approvals or playback cancellation. The existing runtime policy remains for the later policy tickets to replace.

## Evaluation method

The inputs are a bounded candidate transcript, the latest admitted User turn and text Jarvis actually played, correlated by candidate and turn identifiers. A local instruction model assesses addressee and conversational intent. There is no phrase-list classifier, word-count rule, speaker enrollment or cloud assessment.

All scenarios here are synthetic text, pre-labeled by the implementing agent before inference. They include English and Italian corrections without Wake words, clearly addressed stops, Backchannels, speech addressed to another named person, quoted Wake words, nearby dialogue and missing-context ambiguities. These labels are independent of the candidate model's answers. They are not independently human-adjudicated recordings. A dialogue written in text does not establish performance on actual multiple speakers.

Every final candidate assessment runs while the existing `tests.test_voice_mode.rig_for` Hub fixture speaks a scripted reply with fake audio and backend. The fixture advances playback after the assessment. Rows record that the disconnected worker did not mutate player state, admit its candidate, or start an Orchestrator session. Those observations establish isolation, not interruption accuracy: prospective false stops/admissions are counted separately from the semantic verdicts. No real model backend, GitHub action or physical device is used by this Hub replay. Public local weight inference runs only in the explicitly invoked evaluator.

The first 24 cases (`assessment-cases.json`) are development data. Initial free-generation runs rejected malformed output. Their zero false-directed counts cannot establish good semantics: they also missed every directed request. The 1.7B model emitted JSON with `classification: direct`, including for Backchannels. Empty thinking envelopes and an eight-token output cap also affected framing. Those failed experiments remain in the development results; they are not used as held-out accuracy evidence.

To remove format rejection from the semantic comparison, the final decoder constrains one generated token to A (directed), B (unrelated), or C (uncertain), using the model's logits over those labels. This is output grammar, not an addressee heuristic. It reports no calibrated confidence. Four examples from a separate six-case calibration set establish the label convention. A Qwen3 empty thinking envelope is supplied when its tokenizer ignores the non-thinking flag, followed by an assistant `Decision: ` prefix. The prompt, examples, prefix and operating rule were frozen before the fresh 24-case held-out run. No further tuning uses that held-out set.

## Qwen3 evidence

| Candidate and stage | False directed / negatives | Missed directed / positives | Invalid | Warm roundtrip p50 / p90 |
|---|---:|---:|---:|---:|
| Qwen3 0.6B, few-shot calibration | 3/4 | 0/2 | 0/6 | 141.6 / 144.4 ms |
| Qwen3 1.7B, few-shot calibration | 0/4 | 0/2 | 0/6 | 368.9 / 381.9 ms |
| Qwen3 1.7B, fresh held-out | 3/16 | 0/8 | 0/24 | 370.2 / 378.6 ms |

Calibration includes the four supplied examples, so its apparent success is not generalization evidence. The smaller model failed calibration and was not advanced to held-out validation. The larger model failed the untouched set on an Italian Backchannel and two clearly different addressees (`h10`, `h12`, `h13`). Accepting those A verdicts would falsely pause/stop a reply or admit unrelated speech. All eight genuine directed requests received A. There were no timeouts. The candidate therefore fails the accepted priority of leaving unrelated speech uninterrupted; it is not selected for runtime use.

The 1.7B held-out cold roundtrip was 2289.4 ms, including process/model startup. The normal evaluator budget is two seconds; the first explicit evaluation call allows 180 seconds for cold loading, separately reported. Cold loading would need an explicit readiness/preparation lifecycle before any future runtime adoption. Warm percentiles cover all 23 calls after the first, using nearest rank for p90 and the median for p50. They exclude no failed classification.

Assessment roundtrip includes worker IPC, prompt construction and inference. Per-call inference and process/MLX memory measurements remain in result JSON. Audio onset, capture delay, STT delay/errors and real onset-to-stop time are unavailable here and recorded as null. These component figures cannot establish the physical latency goals.

## Materially different local candidate

A final bounded comparison used [Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct), a non-thinking instruction model, through the [MLX community 4-bit conversion](https://huggingface.co/mlx-community/Qwen2.5-1.5B-Instruct-4bit). Both source and conversion declare Apache 2.0. The 3B variant was screened but not downloaded: its [official card declares qwen-research terms](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct), which differ from the permissive models in this comparison. No new license agreement or account was created.

The Qwen2.5 candidate used its own six-case calibration file and a second fresh 24-case held-out file, labeled before either inference run. The four few-shot examples came only from its calibration file. Qwen2 has no thinking envelope; it uses the same explicit Decision prefix and one-token A/B/C output grammar. The operating point was frozen before held-out evaluation and was not adjusted after seeing failures.

| Stage | False directed / negatives | Missed directed / positives | Invalid | Warm p50 / p90 |
|---|---:|---:|---:|---:|
| Qwen2.5 1.5B, calibration | 2/4 | 1/2 | 0/6 | 349.8 / 358.0 ms |
| Qwen2.5 1.5B, fresh held-out | 15/16 | 1/8 | 0/24 | 352.0 / 382.4 ms |

This model fails both background-speech rejection and a genuine Italian correction (`j06`). Its malformed-output count and timeout count are zero, so the semantic errors are visible rather than hidden by formatting rejection. The cold held-out roundtrip was 2510.6 ms. All 24 Hub replay rows preserved playback progress and showed no candidate admission, Task start or player-state mutation, because policy integration is deliberately disconnected.

Held-out resource observations: Qwen3 1.7B peaked at 1604.0 MiB of MLX allocation and 1374.0 MiB of worker RSS; Qwen2.5 1.5B peaked at 1549.8 MiB and 1273.6 MiB respectively. These measures overlap and must not be summed. They do not include a measured whole-Hub coexistence run with all speech engines. Default tests do not load these weights.

## Selection and unresolved decision

The evidence fallback in #31 is fulfilled: the tested approaches, valid semantic errors, costs, integration boundary and alternatives are explicit. Downstream semantic integration is **not ready**. Choosing Qwen3 1.7B's current operating point would accept three unrelated cases out of sixteen; choosing Qwen2.5's would accept fifteen and miss one directed request. Neither tradeoff meets ADR 0014. Rejecting all results as uncertain would preserve playback while dropping genuine corrections, so it is not a solution.

The unresolved decision is which further local recognition approach merits implementation and independent validation. Options include a contextual model trained/calibrated for this task, a different local model/decoder with stronger addressee evidence, or an audio-context approach that can use information missing from transcripts. The models used different fresh held-out sets, so their counts do not establish a head-to-head ranking. Both exact operating points fail; neither is a viable selection. Further investigation must use fresh labeled validation and measure false stops/admissions alongside missed requests. The current worker can host such an approach without changing its authority boundary.

This result does not show that local recognition is impossible or that a cloud model would solve the missing-context problem. Cloud assessment remains a separate privacy/service decision requiring its own evidence and authorization. No more models were explored after this bounded comparison.

## Worker integration contract

`voice.assessment.AssessmentWorker` accepts a picklable local recognizer and one `Candidate` at a time. It never receives a Hub, tool client, player or Approval capability. Its result is evidence only:

- `candidate_id` and `turn_id` must still match the caller's current owner when the result returns. A stale result is uncertain.
- The full input is bounded to 6000 characters, including identifiers and context. Excess input is rejected as uncertain with `context_limit`; the opening is never silently truncated. The evaluation adapter separately refuses prompts above 2048 tokens.
- A busy worker returns uncertain without queuing another candidate. Cancellation, a timeout or worker death disposes of the process; a later request can start a fresh worker. Native inference never runs on the capture thread or event loop.
- Malformed output and unavailable local weights return uncertain. The deadline includes IPC and is checked again before accepting a ready result. No model is asked to phrase operational failures.
- The caller must treat uncertain, busy, stale, limit and failure results as continued playback. No callback in this module can stop speech, admit a request or create work. Later wiring must preserve that boundary and retain candidate audio separately.

The ordinary tests inject stand-in recognizers. They demonstrate that a stalled process does not prevent timestamped synthetic frames from traversing the existing InputPipeline and completing STT, while the Hub also receives input and plays its reply. They exercise unavailable and invalid candidates, context limits, stale ownership, bounded admission, timeout replacement and the absence of policy authority even for a directed verdict. These tests prove lifecycle behavior rather than semantic accuracy.

## Platform, terms and remaining evidence

[MLX LM](https://github.com/ml-explore/mlx-lm) is MIT licensed and runs locally on Apple Silicon. These experiments use mlx-lm 0.32.2 and existing MLX 0.32.4. Optional evaluation dependencies do not change runtime dependencies. [Qwen3 0.6B MLX](https://huggingface.co/Qwen/Qwen3-0.6B-MLX-4bit) and [Qwen3 1.7B MLX](https://huggingface.co/Qwen/Qwen3-1.7B-MLX-4bit) declare Apache 2.0. Revisions, source hashes and per-call resource observations are recorded. Public downloads need no account or key; inference reads a local directory. Candidate text is never sent to the weight host.

The existing private recording session lacks independently labeled addressee context for the required multi-speaker, quoted-wake and User-to-another-person scenarios. This run preserves that evidence untouched and does not manufacture labels from room recordings. Acoustic context is also intrinsically important: the same words can be directed to Jarvis or to another person. A text-only model cannot recover an unobserved addressee. No result here establishes physical directedness, STT robustness, speaker-separated context or whole-Hub resource acceptance.

No cloud fallback is selected or authorized. A later candidate needs a frozen contract, fresh labeled negative and directed scenarios, actual pipeline STT, onset-to-decision measurements and resource coexistence with the speech engines. Timeout uncertainty preserves playback but does not solve missed interruptions. A broad claim that local recognition is impossible would exceed these experiments.

## Reproduce

Run from the repository root. The public model downloads are separate from assessment; the replay command requires an existing local directory and can run with Hugging Face offline mode. Pin the recorded revisions when downloading:

- `Qwen/Qwen3-0.6B-MLX-4bit`: `173234aa840d113125e9f2271100ddbaf16c9620`
- `Qwen/Qwen3-1.7B-MLX-4bit`: `21457c6f51ed54a7c16e988c0844db973815c137`
- `mlx-community/Qwen2.5-1.5B-Instruct-4bit`: `8b403126fc14f14cfc99bb4cfa72ecbc129ea677`

```sh
uv run --with mlx-lm==0.32.2 python -c 'from huggingface_hub import snapshot_download; print(snapshot_download("mlx-community/Qwen2.5-1.5B-Instruct-4bit", revision="8b403126fc14f14cfc99bb4cfa72ecbc129ea677"))'
HF_HUB_OFFLINE=1 uv run --with mlx-lm==0.32.2 python -m spikes.audio.bench_assessment --model /path/to/downloaded/model --calibration spikes/audio/assessment-qwen25-calibration.json --cases spikes/audio/assessment-qwen25-heldout.json --output .jarvis/semantic/replay.json
uv run pytest -q tests/test_assessment.py spikes/audio/test_assessment_benchmark.py
```

For Qwen3, use `assessment-tuning.json` and `assessment-heldout.json` instead. The initial free-generation development interface remains available with `--mode generated`; it is not the final constrained classifier. Development and calibration failures are consolidated in `assessment-development-summary.json`. Full intermediate local dumps remain under `.jarvis/semantic/development-results`. Full held-out rows, timing/memory samples and identifiers remain in the two held-out result files. Labels, examples and output grammar are explicit in the repository; changing any of them requires fresh held-out cases before making a new accuracy claim.
