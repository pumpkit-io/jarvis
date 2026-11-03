"""Print every number the notes' ## Audio section quotes from the synthetic and benchmark results.

    uv run summarise.py

Reads results/ only, so anyone can check the notes against the committed files.
"""

import json
import statistics

from common import RESULTS

STT = {"parakeet": "parakeet", "mlx-whisper": "mlx-whisper", "server-2022": "whisper.cpp small :2022",
       "server-2023": "whisper.cpp turbo :2023"}


def load(name: str) -> dict:
    return json.loads((RESULTS / f"{name}.json").read_text())


def stt() -> None:
    runs = {k: load(f"stt-{k}-synthetic") for k in STT}
    clips = runs["parakeet"]["clips"]
    for kind in ("short", "long"):
        secs = [c["seconds"] for c in clips.values() if c["kind"] == kind]
        print(f"clips {kind}: {len(secs)}, {min(secs)} to {max(secs)} s")
    print("\nengine | load ms | 2 s p50 | 6 s p50 | partials 0.4/0.6/0.8 p50 | WER all/en/it | memory MB")
    for k, r in runs.items():
        lat, part, w, m = r["latency_ms_p50"], r["partials"], r["wer_pct"], r["memory_mb"]
        print(f"{STT[k]} | {r['load_ms']} | {lat['short_all']} | {lat['long_all']} | "
              f"{' / '.join(str(part[s]['ms_p50']) for s in ('0.4s', '0.6s', '0.8s'))} | {w['all']} / {w['en']} / {w['it']} | {m}")
    print("\nparakeet speed-up (other engine's time / parakeet's), per measure:")
    p = runs["parakeet"]
    for k in ("mlx-whisper", "server-2022", "server-2023"):
        r = runs[k]
        ratios = {"2 s": r["latency_ms_p50"]["short_all"] / p["latency_ms_p50"]["short_all"],
                  "6 s": r["latency_ms_p50"]["long_all"] / p["latency_ms_p50"]["long_all"]}
        ratios |= {s: r["partials"][s]["ms_p50"] / p["partials"][s]["ms_p50"] for s in ("0.4s", "0.6s", "0.8s")}
        long_clip = [r["clips"][i]["ms_p50"] / p["clips"][i]["ms_p50"] for i in p["clips"] if p["clips"][i]["kind"] == "long"]
        print(f"  vs {STT[k]}: " + ", ".join(f"{n} {v:.1f}x" for n, v in ratios.items())
              + f"; per 6 s clip {min(long_clip):.1f} to {max(long_clip):.1f}x")
    lang = load("lang-synthetic")
    print("\nlanguage from transcript:", {e: (v["correct"], v["ms_p50"], v["wrong"]) for e, v in lang["engines"].items()})


def tts() -> None:
    r = load("tts")
    for side in ("inprocess", "server"):
        s = r[side]
        print(f"\nTTS {side}: voices {s['voices_present']}, memory {s['memory_mb']}, load {s.get('load_ms')} ms")
        for lang, v in s["per_lang"].items():
            print(f"  {lang}: first audio p50 {v['first_audio_ms_p50']} ms, first request {v.get('first_request_ms')} ms, chunks {v.get('chunks')}")


def aec() -> None:
    r = load("aec-synthetic")
    print(f"\nAEC synthetic ({r['seconds']} s): raw {r['raw_mic']}")
    for d, s in r["webrtc"].items():
        print(f"  {d}: {s['jarvis_only']['speech_frame_pct']} % speech frames, barge-ins {s['user_barge_ins_detected']}")


def wake() -> None:
    r = load("wake-recall-synthetic")
    for vad, v in r["by_vad"].items():
        print(f"\nwake recall {vad}:")
        for phrase, t in v["per_phrasing"].items():
            print(f"  {phrase}: {t['recall@0.3']} / {t['recall@0.5']} / {t['recall@0.7']}, median peak {t['peak_median']}")
    hours, totals = 0.0, {}
    for name in ("librispeech", "librivox-it"):
        fa = load(f"wake-false-accepts-{name}")
        hours += fa["hours"]
        print(f"false accepts {name}: {fa['hours']} h, totals {fa['false_accepts_total']}, per hour {fa['false_accepts_per_hour']}, "
              f"realtime factor {fa['realtime_factor_no_vad']}")
        for vad, h in fa["false_accepts_total"].items():
            for t, n in h.items():
                totals[(vad, t)] = totals.get((vad, t), 0) + n
    print(f"false accepts over both ({hours:.2f} h): "
          + ", ".join(f"{vad} @{t}: {n} ({n / hours:.2f}/h)" for (vad, t), n in totals.items()))


def smart_turn() -> None:
    r = load("smart-turn-synthetic")
    for m, v in r["models"].items():
        print(f"\nsmart turn {m}: {v['size_mb']} MB, p50 {v['inference_ms_p50']} ms, max {v['inference_ms_max']} ms, "
              f"complete {v['complete_judged_complete']}, cut {v['cut_judged_incomplete']}, "
              f"end of turn {v['end_of_turn_ms_with_smart_turn']} ms, saved {v['saved_ms_vs_fixed_silence']}")


def memory() -> None:
    for stt_name in ("parakeet", "mlx-whisper"):
        r = load(f"memory-{stt_name}")
        feet = [c["footprint"] for c in r["claude_processes_seen"]]
        print(f"\nmemory {stt_name}: audio {r['audio_stack_footprint_mb']} MB, mlx peak {r['mlx_peak_mb']} MB, "
              f"+3 claude {r['audio_plus_three_claude_mb']} MB, claude median {statistics.median(feet)} MB of {len(feet)}, "
              f"cache cap {r['mlx_cache_limit_mb']} MB, machine {r['machine_total_mb']} MB")


if __name__ == "__main__":
    for section in (stt, tts, aec, wake, smart_turn, memory):
        section()
