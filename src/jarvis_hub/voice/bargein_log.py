"""The Barge-in decision log: one `metrics.jsonl` row per Barge-in, so thresholds can be tuned from real data.

It lives in the metrics log, not a file of its own, so one reader (`jarvis-hub stats`) and one writer serve both, and
a row's `at` orders it among the voice transitions around it. Rows carry the User's words (the partials) and the
words Jarvis had spoken, which is why `.jarvis/` is git-ignored; no audio goes in them (see `clips.py`).

A row (`kind: "bargein"`; times in ms from the onset unless named; `onset`, `since` and `decided_at` are on the
input clock, so they line up with a clip's sidecar):

    onset, since       the Barge-in's onset, and the end of the guard it counted from (None for "overheard-stop")
    detected_ms        when the minimum speech had been held
    partials           [{text, ms, speech_s, silent_s}], as the decision saw them
    decision           resume | stop | wake | overheard-stop | ended (the reply finished while paused)
    decided_ms         onset to decision; decided_at is the same moment on the clock
    extended           the decision waited past its deadline for one more partial
    wake_hits          Wake word hits while it was open
    played             what Jarvis had said when the pause began
    repeated, next     for a resume, whether the User likely repeated themselves, and what came next (`bargein` or
                       `turn`); the rule is `repeated_themselves` and `BargeInLog.observe`
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis_hub.voice.bargein import Partial, interrupts, still_talking
from jarvis_hub.voice.state import (
    Action,
    Barge,
    BargeIn,
    Event,
    Heard,
    Interrupt,
    Overheard,
    ReplyDone,
    SendTurn,
    Timing,
    Voice,
    WakeWord,
    barge_in_from,
    without_wake_phrase,
)

# After Jarvis resumes, the User speaking again this soon (turn heard, or a new Barge-in) can be a repeat.
REPEAT_WINDOW_S = 8.0


def repeated_themselves(cut_off: bool, next_turn: str) -> bool:
    """Whether the turn the User speaks next, soon after Jarvis resumed, shows the resume was wrong: it cut Jarvis off
    (the User stopped the voice again, by a Wake word or Barge-in), or it holds a hard interrupt ("wait", "no"). What
    a resume was over is only Backchannels or noise, so the words of the two never overlap to compare."""
    return cut_off or interrupts(next_turn)


@dataclass
class _Open:
    onset: float
    since: float | None
    detected: float
    played: str
    wake_hits: list[float] = field(default_factory=lambda: [])


@dataclass
class _Resumed:
    """A resume waiting to learn whether the User repeated themselves."""

    row: dict[str, Any]
    at: float
    last: Partial | None


class BargeInLog:
    def __init__(self, record: Callable[[dict[str, Any]], None], notice: Callable[[str], None], timing: Timing) -> None:
        self._record = record
        self._notice = notice
        self._timing = timing
        self._open: _Open | None = None
        self._resumed: _Resumed | None = None

    def observe(self, before: Voice, event: Event, after: Voice, actions: Sequence[Action], played: Callable[[], str]) -> None:
        """Call after every `step`, with the voice before and after it. `played` is read only when a pause begins."""
        if self._resumed is not None and event.at - self._resumed.at > REPEAT_WINDOW_S:
            self._flush(repeated=False, nxt=None)
        match event:
            case BargeIn(onset, at) if before.barge is None and after.barge is not None:
                if (resumed := self._resumed) is not None:
                    # Speech that ran on through the resume (okay okay okay…) is one run, not a second try.
                    again = resumed.last is None or not still_talking(resumed.last)
                    self._flush(repeated=again, nxt="bargein" if again else None)
                self._open = _Open(onset, barge_in_from(after, self._timing), at, played())
            case WakeWord(at) if self._open is not None:
                self._open.wake_hits.append(at)
            case Heard(text, _, _) if self._resumed is not None:
                cut_off = any(isinstance(a, SendTurn) and a.interrupted for a in actions)
                repeated = repeated_themselves(cut_off, without_wake_phrase(text))
                self._flush(repeated=repeated, nxt="turn")
            case _:
                pass
        if before.barge is not None and after.barge is None and self._open is not None:
            self._decided(before.barge, event, actions)
        elif isinstance(event, Overheard) and any(isinstance(a, Interrupt) for a in actions):
            self._overheard_stop(before, event, played())

    def close(self) -> None:
        """Writes a resume still waiting: nothing followed it."""
        self._flush(repeated=False, nxt=None)

    def _decided(self, barge: Barge, event: Event, actions: Sequence[Action]) -> None:
        assert self._open is not None
        opened, self._open = self._open, None
        partials = (*barge.partials, event) if isinstance(event, Partial) else barge.partials
        if any(isinstance(a, Interrupt) for a in actions):
            decision = "wake" if isinstance(event, WakeWord) else "stop"
        else:
            decision = "ended" if isinstance(event, ReplyDone) else "resume"
        row = {
            "kind": "bargein",
            "onset": round(opened.onset, 3),
            "since": None if opened.since is None else round(opened.since, 3),
            "detected_ms": _ms(opened.detected - opened.onset),
            "partials": [_partial(p, opened.onset) for p in partials],
            "decision": decision,
            "decided_ms": _ms(event.at - opened.onset),
            "decided_at": round(event.at, 3),
            "extended": barge.last_call,
            "wake_hits": [_ms(at - opened.onset) for at in opened.wake_hits],
            "played": opened.played,
        }
        if decision == "resume":
            self._resumed = _Resumed(row, event.at, partials[-1] if partials else None)
        else:
            self._write({**row, "repeated": None, "next": None})

    def _overheard_stop(self, before: Voice, event: Overheard, played: str) -> None:
        since = barge_in_from(before, self._timing)
        self._write(
            {
                "kind": "bargein",
                "onset": None,
                "since": None if since is None else round(since, 3),
                "detected_ms": None,
                "partials": [{"text": event.text, "ms": None, "speech_s": None, "silent_s": None}],
                "decision": "overheard-stop",
                "decided_ms": None,
                "decided_at": round(event.at, 3),
                "extended": False,
                "wake_hits": [],
                "played": played,
                "repeated": None,
                "next": None,
            }
        )

    def _flush(self, *, repeated: bool, nxt: str | None) -> None:
        if (resumed := self._resumed) is not None:
            self._resumed = None
            self._write({**resumed.row, "repeated": repeated, "next": nxt})

    def _write(self, row: dict[str, Any]) -> None:
        try:
            self._record(row)
        except Exception as e:
            self._notice(f"Could not write metrics: {e}")


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def _partial(p: Partial, onset: float) -> dict[str, Any]:
    return {"text": p.text, "ms": _ms(p.at - onset), "speech_s": round(p.speech_s, 3), "silent_s": round(p.silent_s, 3)}
