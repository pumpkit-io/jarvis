"""Phase 0 baseline: end of the User's speech to Jarvis's first audio with today's launcher. Throwaway.

Joins VoiceMode's event log with the hook log from hook.py. For each spoken turn:
  end of speech  = RECORDING_END - VoiceMode's silence threshold (recording stops after that much silence)
  first audio    = TTS_FIRST_AUDIO of the next converse call
and the gap is split into silence tail, STT, tool return, model, and TTS time to first audio.

Run: python3 analyze.py [--hooks hooks.jsonl] [--events ~/.voicemode/logs/events] [--silence-ms N] [--since ISO]
"""

import argparse
import glob
import json
import os
import statistics
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_GAP_S = 300


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def load_events(events_dir, since, until):
    rows = []
    for path in sorted(glob.glob(os.path.join(events_dir, "voicemode_events_*.jsonl"))):
        for line in open(path):
            e = json.loads(line)
            t = ts(e["timestamp"])
            if since <= t <= until:
                rows.append((t, e["event_type"], e.get("data") or {}))
    return sorted(rows, key=lambda r: r[0])


def silence_threshold_ms(conv_dir, since, until):
    found = []
    for path in glob.glob(os.path.join(conv_dir, "exchanges_*.jsonl")):
        for line in open(path):
            x = json.loads(line)
            if x.get("type") == "stt" and since <= ts(x["timestamp"]) <= until:
                ms = ((x.get("metadata") or {}).get("silence_detection") or {}).get("silence_threshold_ms")
                if ms:
                    found.append(ms)
    return statistics.median(found) if found else 1000


def converse_calls(events):
    calls, cur = [], None
    for t, kind, data in events:
        if kind == "TOOL_REQUEST_START":
            cur = {"start": t}
        elif cur is not None:
            cur.setdefault(kind, t)
            if kind == "TOOL_REQUEST_END":
                calls.append(cur)
                cur = None
    return calls


def first_after(times, t):
    return next((x for x in times if x >= t), None)


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hooks", default=os.path.join(HERE, "hooks.jsonl"))
    ap.add_argument("--events", default=os.path.expanduser("~/.voicemode/logs/events"))
    ap.add_argument("--conversations", default=os.path.expanduser("~/.voicemode/logs/conversations"))
    ap.add_argument("--silence-ms", type=float)
    ap.add_argument("--since", help="ISO time; defaults to the first hook entry")
    ap.add_argument("--until", help="ISO time; defaults to the last hook entry")
    args = ap.parse_args()

    hooks = [json.loads(line) for line in open(args.hooks)] if os.path.exists(args.hooks) else []
    since = ts(args.since) if args.since else (hooks[0]["t"] - 1 if hooks else 0)
    until = ts(args.until) if args.until else (hooks[-1]["t"] + 60 if hooks else float("inf"))
    silence = (args.silence_ms or silence_threshold_ms(args.conversations, since, until)) / 1000

    is_converse = lambda h: (h.get("tool") or "").endswith("converse")
    pre = sorted(h["t"] for h in hooks if h["event"] == "PreToolUse" and is_converse(h))
    post = sorted(h["t"] for h in hooks if h["event"] == "PostToolUse" and is_converse(h))
    other_pre = sorted(h["t"] for h in hooks if h["event"] == "PreToolUse" and not is_converse(h))

    calls = converse_calls(load_events(args.events, since, until))
    turns = []
    for a, b in zip(calls, calls[1:]):
        if "STT_COMPLETE" not in a or "TTS_FIRST_AUDIO" not in b:
            continue  # no speech heard, or the next call did not speak
        if b["start"] - a["TOOL_REQUEST_END"] > MAX_GAP_S:
            continue  # the two calls belong to different sessions
        end_speech = a["RECORDING_END"] - silence
        returned = first_after(post, a["STT_COMPLETE"]) or a["TOOL_REQUEST_END"]
        next_pre = first_after(pre, returned) or b["start"]
        turns.append({
            "total": b["TTS_FIRST_AUDIO"] - end_speech,
            "silence_tail": silence,
            "stt": a["STT_COMPLETE"] - a["RECORDING_END"],
            "tool_return": returned - a["STT_COMPLETE"],
            "model": next_pre - returned,
            "tts_first_audio": b["TTS_FIRST_AUDIO"] - next_pre,
            "other_tools": sum(returned <= t <= next_pre for t in other_pre),
            "hooked": bool(post),
        })

    print(f"silence threshold {silence * 1000:.0f} ms, {len(turns)} turns, hook entries {len(hooks)}")
    print("total_s  silence  stt  return  model  tts  other_tools")
    for t in turns:
        print(f"{t['total']:6.2f}  {t['silence_tail']:6.2f}  {t['stt']:4.2f}  {t['tool_return']:5.2f}  "
              f"{t['model']:5.2f}  {t['tts_first_audio']:4.2f}  {t['other_tools']}")
    for label, subset in (("all turns", turns), ("talk-only turns", [t for t in turns if not t["other_tools"]])):
        if subset:
            line = ", ".join(f"{k} p50 {pct([t[k] for t in subset], .5):.2f}"
                             for k in ("total", "stt", "model", "tts_first_audio"))
            print(f"{label} (n={len(subset)}): total p90 {pct([t['total'] for t in subset], .9):.2f}; {line}")


if __name__ == "__main__":
    main()
