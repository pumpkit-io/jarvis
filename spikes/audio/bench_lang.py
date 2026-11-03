"""Language of an utterance from its transcript, for STT engines that don't report one.

    uv run bench_lang.py --source synthetic

Parakeet returns text only, and the Hub picks the reply voice from the User's language,
so this checks English-or-Italian detection on each engine's transcripts of the clips.
"""

import argparse
import json
import time

from lingua import Language, LanguageDetectorBuilder

from common import RESULTS, p50, write_result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "recordings"], default="synthetic")
    args = ap.parse_args()
    detector = LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.ITALIAN).with_preloaded_language_models().build()
    code = {Language.ENGLISH: "en", Language.ITALIAN: "it"}
    result = {"source": args.source, "engines": {}}
    for path in sorted(RESULTS.glob(f"stt-*-{args.source}.json")):
        stt = json.loads(path.read_text())
        wrong, times = [], []
        for clip_id, clip in stt["clips"].items():
            t = time.perf_counter()
            guess = detector.detect_language_of(clip["hyp"])
            times.append((time.perf_counter() - t) * 1000)
            if code.get(guess) != clip["lang"]:
                wrong.append(f"{clip_id}: {clip['hyp']!r} -> {code.get(guess)}")
        result["engines"][stt["engine"]] = {"correct": f"{len(stt['clips']) - len(wrong)}/{len(stt['clips'])}",
                                            "ms_p50": round(p50(times), 2), "wrong": wrong}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"-> {write_result(f'lang-{args.source}', result)}")


if __name__ == "__main__":
    main()
