# Wake candidate comparison for #29

No tested operating point currently qualifies for runtime adoption. This evidence ticket supplies a reproducible comparison and a measured blocker for #37; it does not establish reliable idle activation.

The comparison reuses the 80 scored wake takes (20 for each phrase) and 13 negative room/video files (3900 seconds with sound) retained by the validation checkout. Failed recording attempts remain separate and untouched. The completed openWakeWord sweep is reused from `b575016:spikes/audio/results/wake-threshold-sweep-recordings.json`; it was not repeated. Only aggregate counts are copied into `wake-candidate-openwakeword-baseline.json`. New replay results contain no personal transcript, clip-level hypothesis or recording path. Original recordings and all uncommitted validation work remain in place.

The joint requirement is at least 19/20 on **each** phrase and at most one false wake/hour. The runner refuses to count missing positive takes or less than an hour of negatives as a joint pass. These are exploratory data already used for tuning, so even a joint pass would require independent confirmation.

| Candidate / operating point | Hey Jarvis | Hi Jarvis | Ehi Jarvis | Jarvis | False wakes/hour | Joint gate |
|---|---:|---:|---:|---:|---:|---|
| openWakeWord .05, no VAD | 20/20 | 19/20 | 20/20 | 19/20 | 4.62 (5/3900 s) | Fail |
| openWakeWord .15, no VAD | 20/20 | 19/20 | 18/20 | 17/20 | .92 (1/3900 s) | Fail |
| openWakeWord .2, no VAD | 19/20 | 19/20 | 18/20 | 13/20 | 0 (0/3900 s) | Fail |
| Sherpa .25, score 1 | 14/20 | 16/20 | 8/20 | 13/20 | 0 (0/3900 s) | Fail |
| Sherpa .1, score 2 (exploratory) | 17/20 | 16/20 | 13/20 | 15/20 | 0 (0/3900 s) | Fail |

All 16 openWakeWord threshold/VAD operating points remain in the baseline aggregate file. None meets the joint gate. Its .5 runtime threshold is unchanged.

The new candidate is sherpa-onnx 2.0.1 with the English GigaSpeech Zipformer 3.3M model dated 2024-01-01, fp32 encoder/decoder/joiner, one CPU thread and the BPE keyword `JARVIS` (`▁JA R VI S`). Detecting the common name allows all four phrasings without requiring four separate matches. The initial operating point uses the library defaults of threshold .25 and score 1. The runner supplies 80 ms blocks and one second of leading/trailing silence on positive takes. Negatives remain continuous within each original file with one second of trailing silence. Hits within a two-second burst count once, matching the existing wake benchmark. Streams reset between files. Quiet-minute exclusion reuses the existing benchmark; these files contain no excluded quiet minutes.

One exploratory threshold .1/score 2 replay improved some counts but still missed every phrase gate. It is retained separately in `wake-candidate-sherpa-exploratory.json`, with the same corpus fingerprint and model hashes. Its RTF was .0152 and end-of-run process RSS 356.6 MiB. These two points do not exhaust possible Sherpa settings.

Default Sherpa replay took .0159 seconds of compute per second of input; process RSS at report time was 268.3 MiB. These are standalone component observations under concurrent development load, not whole-Hub idle CPU, peak memory or end-to-end activation delay. The fp32 ONNX files occupy approximately 13.2 MiB; the downloaded archive includes int8 alternatives that were not tested. Result files record model hashes and the corpus fingerprint so subsequent comparisons can check identical input and weights.

## Candidate feasibility

- [openWakeWord](https://github.com/dscripka/openWakeWord) works on this Mac through ONNX. Its code is Apache 2.0; bundled pretrained models are CC BY-NC-SA 4.0. Upstream describes pretrained support as English-only and provides custom training. No account is needed for the existing candidate. The existing comparison measured roughly 2.73% of one core for its no-VAD sweep. A custom model covering Italian pronunciation needs a separately trained model and untouched confirmation samples; no custom model is silently presumed to exist.
- [Sherpa CPU wheels](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/python/install.rst) support macOS arm64; installation and inference succeeded here. [Its code is Apache 2.0](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE); the README inside the official GigaSpeech model archive declares Apache License 2.0. [Upstream KWS instructions](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html) provide the credential-free archive and custom-keyword mechanism. This model is English, so Italian-accented recall is an empirical question, not promised multilingual support. All audio stays local.
- [Porcupine's Python quick start](https://picovoice.ai/docs/quick-start/porcupine-python/) supports macOS arm64 but requires a Picovoice account and AccessKey; custom keywords use its Console. No account, credentials, service agreement or free-tier entitlement was created or assumed. Its binary/model licensing and actual footprint must be checked against the intended use before adoption. It is an unmeasured alternative, not a passing result.

## Reproduce without recording or playback

From the repository root, install optional evaluation packages without changing runtime dependencies:

```sh
uv run --with sherpa-onnx==2.0.1 --with sentencepiece==0.2.2 --with psutil==7.2.2 python spikes/audio/bench_wake_candidate.py --help
mkdir -p .jarvis/wake-comparison
curl -L --fail -o .jarvis/wake-comparison/model.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
tar xf .jarvis/wake-comparison/model.tar.bz2 -C .jarvis/wake-comparison
uv run --with sherpa-onnx==2.0.1 --with sentencepiece==0.2.2 --with psutil==7.2.2 python spikes/audio/bench_wake_candidate.py --recordings /path/to/saved/recordings --model .jarvis/wake-comparison/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01
uv run --with psutil==7.2.2 pytest -q spikes/audio/test_wake_candidate.py
```

The archive SHA-256 measured here is `f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a`. Keep model extraction and generated keyword files under the local gitignored folder. `--recordings` reads the saved evidence in place. The runner uses the existing `common.write_result` interface, which writes aggregate JSON to `spikes/audio/results` and local copies under gitignored `spikes/audio/data/results`; `JARVIS_AUDIO_RESULTS` can redirect aggregate output. No conversational model, microphone, speakers or GitHub mutation is involved.

## Decision and confirmation plan

Retain the current runtime detector provisionally. Its failure is explicit; switching to the measured Sherpa default would lose recall without evidence of a benefit. The next credential-free options are a custom openWakeWord head trained for the required pronunciations, or further Sherpa keyword/model development. Keep training and tuning audio separate from confirmation audio. Porcupine evaluation requires an explicit account/credential decision first.

After selecting and freezing a candidate, model, keyword representation and operating point, independently test 20 new takes per required phrase and at least one new hour of room negatives. Report every miss, false wake, failed attempt and exclusion. Then exercise wake-and-request in one breath through the actual Hub, checking preservation of the request's beginning and latency. Only that independent run can establish #37's wake gate. This comparison makes no new physical acceptance claim and authorizes no recording session.
