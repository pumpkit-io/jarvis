"""Phase 0 baseline: end of the User's speech to Jarvis's first audio with today's launcher. Throwaway.

Run steps and the meaning of each count are in docs/notes/phase-0-spikes.md.
"""

import argparse
import glob
import json
import os
import statistics
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_GAP_S = 300


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def read_jsonl(path, bad, required=()):
    """Yield each JSON object in a JSONL file; append `path` to `bad` for every line that is unusable."""
    with open(path, errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad.append(path)
                continue
            if isinstance(row, dict) and all(k in row for k in required):
                yield row
            else:
                bad.append(path)


def load_events(events_dir, since, until, bad):
    rows = []
    for path in sorted(glob.glob(os.path.join(events_dir, "voicemode_events_*.jsonl"))):
        for e in read_jsonl(path, bad, ("timestamp", "event_type")):
            t = ts(e["timestamp"])
            if since <= t <= until:
                rows.append((t, e["event_type"], e.get("data") or {}))
    return sorted(rows, key=lambda r: r[0])


def silence_threshold_ms(conv_dir, since, until, bad):
    found = []
    for path in glob.glob(os.path.join(conv_dir, "exchanges_*.jsonl")):
        for x in read_jsonl(path, bad, ("timestamp",)):
            if x.get("type") == "stt" and since <= ts(x["timestamp"]) <= until:
                ms = ((x.get("metadata") or {}).get("silence_detection") or {}).get("silence_threshold_ms")
                if ms:
                    found.append(ms)
    return statistics.median(found) if found else 1000


def converse_calls(events):
    """Group converse events into calls; returns (calls, dropped).

    A start with no end is dropped and leaves a None, so its neighbours are not paired.
    """
    calls, cur, dropped = [], None, 0
    for t, kind, data in events:
        if kind.startswith("TOOL_REQUEST_") and data.get("tool_name") != "converse":
            continue
        if kind == "TOOL_REQUEST_START":
            if cur is not None:
                calls.append(None)
                dropped += 1
            cur = {"start": t}
        elif cur is not None:
            cur.setdefault(kind, t)
            if kind == "TOOL_REQUEST_END":
                calls.append(cur)
                cur = None
    if cur is not None:
        calls.append(None)
        dropped += 1
    return calls, dropped


def first_after(times, t, limit=float("inf")):
    return next((x for x in times if t <= x <= limit), None)


def compute_turns(calls, pre, post, other_pre, silence):
    """Split each spoken turn between consecutive calls; returns (turns, skipped, fallbacks).

    Hook lookups stop at the next call's start so a missing entry cannot borrow a later call's hook.
    """
    turns, skipped, fallbacks = [], 0, 0
    for a, b in zip(calls, calls[1:]):
        if a is None or b is None:
            continue
        if "STT_COMPLETE" not in a or "TTS_FIRST_AUDIO" not in b:
            continue  # no speech heard, or the next call did not speak
        if b["start"] - a["TOOL_REQUEST_END"] > MAX_GAP_S:
            continue  # the two calls belong to different sessions
        if "RECORDING_END" not in a:
            skipped += 1
            continue
        end_speech = a["RECORDING_END"] - silence
        hook_returned = first_after(post, a["STT_COMPLETE"], b["start"])
        returned = hook_returned or a["TOOL_REQUEST_END"]
        hook_next_pre = first_after(pre, returned, b["start"])
        next_pre = hook_next_pre or b["start"]
        turn = {
            "total": b["TTS_FIRST_AUDIO"] - end_speech,
            "silence_tail": silence,
            "stt": a["STT_COMPLETE"] - a["RECORDING_END"],
            "tool_return": returned - a["STT_COMPLETE"],
            "model": next_pre - returned,
            "tts_first_audio": b["TTS_FIRST_AUDIO"] - next_pre,
            "other_tools": sum(returned <= t <= next_pre for t in other_pre),
        }
        if min(turn[k] for k in ("total", "stt", "tool_return", "model", "tts_first_audio")) < 0:
            skipped += 1
            continue
        fallbacks += hook_returned is None or hook_next_pre is None
        turns.append(turn)
    return turns, skipped, fallbacks


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
    ap.add_argument("--session", help="session id from --list-sessions, or 'all'; default: the most recent session. 'all' covers the span from the first to the last hook entry, so unhooked VoiceMode sessions inside it are included")
    ap.add_argument("--list-sessions", action="store_true")
    args = ap.parse_args()

    bad_lines = []
    all_hooks = list(read_jsonl(args.hooks, bad_lines, ("session", "t", "event"))) if os.path.exists(args.hooks) else []
    if not all_hooks:
        sys.exit(f"no hook entries in {args.hooks}: record a session with the hook settings first "
                 "(see docs/notes/phase-0-spikes.md), or the analysis would cover every VoiceMode event ever logged")
    sessions = {}
    for h in all_hooks:
        sessions.setdefault(h["session"], []).append(h["t"])
    if args.list_sessions:
        for sid, times in sorted(sessions.items(), key=lambda kv: kv[1][0]):
            print(f"{sid}  {datetime.fromtimestamp(times[0]):%Y-%m-%d %H:%M:%S}  {len(times)} hook entries")
        return
    chosen = args.session or max(sessions, key=lambda sid: sessions[sid][-1])
    hooks = all_hooks if chosen == "all" else [h for h in all_hooks if h["session"] == chosen]
    if not hooks:
        sys.exit(f"session {chosen} not in the hook log; see --list-sessions")
    since, until = hooks[0]["t"] - 1, hooks[-1]["t"] + 60
    silence = (args.silence_ms if args.silence_ms is not None else silence_threshold_ms(args.conversations, since, until, bad_lines)) / 1000

    is_converse = lambda h: (h.get("tool") or "").endswith("converse")
    pre = sorted(h["t"] for h in hooks if h["event"] == "PreToolUse" and is_converse(h))
    # A converse call that errors fires PostToolUseFailure instead of PostToolUse.
    post = sorted(h["t"] for h in hooks if h["event"] in ("PostToolUse", "PostToolUseFailure") and is_converse(h))
    other_pre = sorted(h["t"] for h in hooks if h["event"] == "PreToolUse" and not is_converse(h))

    calls, dropped = converse_calls(load_events(args.events, since, until, bad_lines))
    turns, skipped, fallbacks = compute_turns(calls, pre, post, other_pre, silence)

    print(f"session {chosen}: {len(hooks)} hook entries ({len(pre)} converse calls hooked, {len(other_pre)} other tool calls), "
          f"silence threshold {silence * 1000:.0f} ms, {len(turns)} turns, "
          f"{skipped} skipped with inconsistent timings, {fallbacks} used VoiceMode timestamps for a missing hook entry, "
          f"{dropped} calls dropped for a missing end, {len(bad_lines)} unparseable log lines skipped")
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
