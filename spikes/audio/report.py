"""Write the recording session's numbers into docs/notes/phase-0-spikes.md, at the end of ## Audio.

    uv run report.py            # from results/*recordings*.json and the speaker run
    uv run report.py --print    # show the block without writing it

The block sits between two HTML comments, so a rerun replaces it instead of adding another.
"""

import argparse
import datetime
import json
import os
import re
from pathlib import Path

from common import HERE, RESULTS

NOTES = Path(os.environ.get("JARVIS_AUDIO_NOTES", HERE.parent.parent / "docs" / "notes" / "phase-0-spikes.md"))
START = "<!-- recording-session results: start (written by spikes/audio/report.py) -->"
END = "<!-- recording-session results: end -->"


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
        if "failed" in s:
            rows.append(f"| {name} | failed: {s['failed']} | - |")
            return
        rows.append(f"| {name} | {s['jarvis_only']['triggers']} ({s['jarvis_only']['per_minute']}) | {s.get('user_barge_ins_detected', '-')} |")

    row("raw mic", r["raw_mic"])
    for delay, s in r["webrtc"].items():
        row(f"WebRTC APM {delay}", s)
    row("macOS Voice Processing I/O", r.get("macos_vpio", {"failed": "not recorded"}))
    return rows


def smart_turn_block() -> list[str]:
    r = load("smart-turn-recordings")
    if not r:
        return ["No Smart Turn results on recordings."]
    out = []
    for m, v in r["models"].items():
        line = (f"- `{m}`: {v['inference_ms_p50']} ms p50 ({v['inference_ms_max']} ms max); complete turns judged complete "
                f"{v['complete_judged_complete']}, synthetic cuts of long clips judged incomplete {v['cut_judged_incomplete']}")
        if "real_pause_judged_incomplete" in v:
            line += (f"; cut at a real pause judged incomplete {v['real_pause_judged_incomplete']}, "
                     f"whole pause takes judged complete {v['pause_takes_judged_complete']}")
        out.append(line + ".")
    pauses = [p for p in r.get("pause_s", {}).values() if p is not None]
    if r.get("pause_s"):
        out.append(f"- Pauses found in {len(pauses)}/{len(r['pause_s'])} pause takes"
                   + (f", {min(pauses)} to {max(pauses)} s long." if pauses else "."))
    return out


def block() -> str:
    today = datetime.date.today().isoformat()
    lines = [f"### Recording session results ({today})", "",
             "Measured by `spikes/audio/session.py` on the User's own recordings. Numbers: `spikes/audio/results/*-recordings.json`, "
             "`aec-speaker-run.json` and `wake-false-accepts-video.json`; transcripts stay in `spikes/audio/data/results/` (local).",
             "", "**STT**", "", *stt_block(), *lang_block(), "", "**Wake word** (openWakeWord `hey_jarvis`, takes detected)", "", *wake_block(),
             "", "**Echo cancellation** (built-in speakers)", "", *aec_block(), "", "**Smart Turn v3** (User's clips and pause takes)", "",
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
    NOTES.write_text(place(NOTES.read_text(), text))
    print(f"wrote the results block into {NOTES}")


def place(notes: str, text: str) -> str:
    """Put the block between its markers, replacing an earlier one, or at the end of ## Audio."""
    marked = f"{START}\n{text.rstrip()}\n{END}\n"
    if START in notes and END in notes:
        a, b = notes.index(START), notes.index(END) + len(END)
        return notes[:a] + marked.rstrip("\n") + notes[b:]
    start = re.search(r"^## Audio$", notes, re.M).start()
    nxt = notes.find("\n## ", start + 1)
    end = len(notes) if nxt == -1 else nxt + 1
    head, tail = notes[:end].rstrip("\n") + "\n\n", notes[end:]
    return head + marked + ("\n" + tail if tail else "")


if __name__ == "__main__":
    main()
