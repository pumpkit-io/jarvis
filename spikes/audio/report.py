"""Append the recording session's numbers to docs/notes/phase-0-spikes.md, at the end of ## Audio.

    uv run report.py            # from data/results/*recordings*.json and the speaker run
    uv run report.py --print    # show the block without writing it
"""

import argparse
import datetime
import json
import re

from common import HERE, RESULTS

NOTES = HERE.parent.parent / "docs" / "notes" / "phase-0-spikes.md"


def load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def stt_block() -> list[str]:
    rows = ["| Engine | WER all / EN / IT | 2 s clip p50 | 6 s clip p50 | 0.4 / 0.6 / 0.8 s partial p50 |", "|---|---|---|---|---|"]
    for engine in ["parakeet", "mlx-whisper", "server-2022", "server-2023"]:
        r = load(f"stt-{engine}-recordings")
        if not r:
            continue
        w, lat, part = r["wer_pct"], r["latency_ms_p50"], r["partials"]
        rows.append(f"| {r['engine']} | {w['all']} / {w['en']} / {w['it']} % | {lat['short_all']} ms | {lat['long_all']} ms | "
                    f"{' / '.join(str(part[k]['ms_p50']) for k in ['0.4s', '0.6s', '0.8s'])} ms |")
    return rows if len(rows) > 2 else ["No STT results on recordings."]


def lang_block() -> list[str]:
    r = load("lang-recordings")
    if not r:
        return []
    return ["", "Language from the transcript (lingua, EN or IT): "
            + "; ".join(f"{engine} {v['correct']}" for engine, v in r["engines"].items()) + "."]


def wake_block() -> list[str]:
    r = load("wake-recall-recordings")
    out = []
    if r:
        out += ["| Phrasing | vad 0 @0.3 / 0.5 / 0.7 | vad 0.5 @0.3 / 0.5 / 0.7 |", "|---|---|---|"]
        no_vad, vad = r["by_vad"]["vad_threshold=0.0"]["per_phrasing"], r["by_vad"]["vad_threshold=0.5"]["per_phrasing"]
        for phrase in no_vad:
            cells = [" / ".join(t[f"recall@{x}"] for x in ("0.3", "0.5", "0.7")) for t in (no_vad[phrase], vad[phrase])]
            out.append(f"| {phrase} | {cells[0]} | {cells[1]} |")
    fa = load("wake-false-accepts-video")
    if fa:
        out += ["", f"False accepts with a video playing in the room ({fa['hours']} h): {json.dumps(fa['false_accepts_total'])}."]
    return out or ["No Wake word results on recordings."]


def aec_block() -> list[str]:
    r = load("aec-speaker-run")
    if not r:
        return ["No speaker run results."]
    rows = ["| Path | Barge-in triggers on Jarvis alone (per min) | User Barge-ins detected |", "|---|---|---|"]

    def row(name, s):
        rows.append(f"| {name} | {s['jarvis_only']['triggers']} ({s['jarvis_only']['per_minute']}) | {s.get('user_barge_ins_detected', '-')} |")

    row("raw mic", r["raw_mic"])
    for delay, s in r["webrtc"].items():
        row(f"WebRTC APM {delay}", s)
    if "macos_vpio" in r:
        row("macOS Voice Processing I/O", r["macos_vpio"])
    return rows


def smart_turn_block() -> list[str]:
    r = load("smart-turn-recordings")
    if not r:
        return ["No Smart Turn results on recordings."]
    return [f"- `{m}`: {v['inference_ms_p50']} ms p50; complete turns judged complete {v['complete_judged_complete']}, "
            f"cut-off turns judged incomplete {v['cut_judged_incomplete']}." for m, v in r["models"].items()]


def block() -> str:
    today = datetime.date.today().isoformat()
    lines = [f"### Recording session results ({today})", "",
             "Measured by `spikes/audio/session.py` on the User's own recordings. Raw JSON: `spikes/audio/data/results/` (local).",
             "", "**STT**", "", *stt_block(), *lang_block(), "", "**Wake word** (openWakeWord `hey_jarvis`, takes detected)", "", *wake_block(),
             "", "**Echo cancellation** (built-in speakers)", "", *aec_block(), "", "**Smart Turn v3** (User's clips)", "",
             *smart_turn_block(), "",
             "Next: update the verdict table above with the chosen STT, TTS, echo cancellation and Wake word.", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    text = block()
    if args.print:
        print(text)
        return
    notes = NOTES.read_text()
    start = re.search(r"^## Audio$", notes, re.M).start()
    nxt = notes.find("\n## ", start + 1)
    end = len(notes) if nxt == -1 else nxt + 1
    head, tail = notes[:end].rstrip("\n") + "\n\n", notes[end:]
    NOTES.write_text(head + text + ("\n" + tail if tail else ""))
    print(f"appended to {NOTES}")


if __name__ == "__main__":
    main()
