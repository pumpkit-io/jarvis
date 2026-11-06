"""`jarvis-hub stats`: what the metrics log says about usage and latency, per role, over a time range."""

import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeGuard, TypeVar, cast

ROLES = ("talker", "orchestrator", "subagent")
# What `voice/bargein_log.py` writes as a Barge-in's decision.
DECISIONS = ("resume", "stop", "wake", "overheard-stop", "ended")
_USAGE = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
_AGO = re.compile(r"(\d+)([mhdw])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


@dataclass
class Totals:
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_ms: int = 0
    # Of the turns that logged a duration.
    durations: list[int] = field(default_factory=lambda: [])


@dataclass
class Stats:
    roles: dict[str, Totals] = field(default_factory=lambda: {})
    # Milliseconds from the User's line to the Talker's first sentence.
    first_sentence: list[int] = field(default_factory=lambda: [])
    limit_hits: int = 0
    # Rows that are not a JSON object, have no readable time, or are a turn with a field missing or of the wrong type.
    skipped: int = 0


def parse_when(text: str, now: datetime, *, end: bool = False) -> datetime:
    """A time flag: how long ago (`30m`, `24h`, `7d`, `2w`), or a date or date and time (local unless it has an offset).
    A bare date is that day's start, or with `end` the next day's, so the range takes the whole day in."""
    text = text.strip()
    if match := _AGO.fullmatch(text):
        return now - timedelta(**{_UNITS[match[2]]: int(match[1])})
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is neither a time ago like 24h or 7d nor a date like 2026-11-01") from None
    if end and _is_date(text):
        when += timedelta(days=1)
    return when if when.tzinfo else when.astimezone()


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


_Number = TypeVar("_Number", int, float)


def percentile(values: list[_Number], p: float) -> _Number | None:
    """Nearest rank: the smallest value that at least p percent of the values do not exceed."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def read_rows(path: Path) -> Iterator[object]:
    """Every non-blank line of the log, parsed; a line that is not JSON (a write cut short) comes as None."""
    if not path.exists():
        return
    yield from parse_rows(path.read_text())


def parse_rows(text: str) -> Iterator[object]:
    """Parse a fixed metrics snapshot, retaining malformed lines as unavailable evidence."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            yield None


def summarise(rows: Iterable[object], since: datetime | None = None, until: datetime | None = None) -> Stats:
    stats = Stats()
    for row in rows:
        at = _at(row)
        if not isinstance(row, dict) or at is None:
            stats.skipped += 1
            continue
        row = cast(dict[str, Any], row)
        if (since and at < since) or (until and at >= until):
            continue
        if row.get("kind") == "usage_limit":
            stats.limit_hits += 1
        if "usage" not in row:
            continue
        turn = _turn(row)
        if turn is None:
            stats.skipped += 1
            continue
        role, usage, duration = turn
        totals = stats.roles.setdefault(role, Totals())
        totals.turns += 1
        totals.input_tokens += usage["input_tokens"]
        totals.output_tokens += usage["output_tokens"]
        totals.cache_read_tokens += usage["cache_read_tokens"]
        totals.cache_creation_tokens += usage["cache_creation_tokens"]
        totals.duration_ms += duration
        totals.durations.append(duration)
        if role == "talker" and _is_int(first := row.get("first_sentence_ms")):
            stats.first_sentence.append(first)
    return stats


def _at(row: object) -> datetime | None:
    """The row's time, as UTC when it has no offset; None when it has none that parses."""
    at = cast(dict[str, Any], row).get("at") if isinstance(row, dict) else None
    if not isinstance(at, str):
        return None
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _turn(row: dict[str, Any]) -> tuple[str, dict[str, int], int] | None:
    """A session result's role, usage and duration, or None when one of them is missing or of the wrong type."""
    role, usage, duration = row.get("role"), row.get("usage"), row.get("duration_ms")
    if role not in ROLES or not isinstance(usage, dict) or not _is_int(duration):
        return None
    usage = cast(dict[str, Any], usage)
    if not all(_is_int(usage.get(name)) for name in _USAGE):
        return None
    return role, usage, duration


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def render(stats: Stats, since: datetime | None, until: datetime | None) -> str:
    def stamp(when: datetime | None, none: str) -> str:
        return none if when is None else when.astimezone().strftime("%Y-%m-%d %H:%M")

    out = [f"Jarvis usage from {stamp(since, 'the start')} to {stamp(until, 'now')}", ""]
    table: list[tuple[str, ...]] = [("role", "turns", "input", "output", "cache read", "cache write", "duration")]
    total = Totals()
    for role in ROLES:
        if (t := stats.roles.get(role)) is None:
            continue
        table.append(_cells(role, t))
        total.turns += t.turns
        total.input_tokens += t.input_tokens
        total.output_tokens += t.output_tokens
        total.cache_read_tokens += t.cache_read_tokens
        total.cache_creation_tokens += t.cache_creation_tokens
        total.duration_ms += t.duration_ms
    footer = [f"Usage limit hits: {stats.limit_hits}"]
    if stats.skipped:
        footer.append(f"Skipped {stats.skipped} malformed rows.")
    if len(table) == 1:
        return "\n".join([*out, "No turns logged in this range.", "", *footer])
    table.append(_cells("total", total))
    out += _align(table, left=1)
    out += ["", *footer, "", "Latency (ms)"]
    latency: list[tuple[str, ...]] = [("role", "metric", "n", "p50", "p90")]
    latency.append(_latency("talker", "first sentence", stats.first_sentence))
    for role in ROLES:
        if (t := stats.roles.get(role)) is not None:
            latency.append(_latency(role, "turn duration", t.durations))
    return "\n".join([*out, *_align(latency, left=2)])


def _cells(role: str, t: Totals) -> tuple[str, ...]:
    return (
        role,
        f"{t.turns:,}",
        f"{t.input_tokens:,}",
        f"{t.output_tokens:,}",
        f"{t.cache_read_tokens:,}",
        f"{t.cache_creation_tokens:,}",
        _span(t.duration_ms),
    )


def _latency(role: str, metric: str, values: list[int]) -> tuple[str, ...]:
    p50, p90 = percentile(values, 50), percentile(values, 90)
    return (role, metric, str(len(values)), "-" if p50 is None else f"{p50:,}", "-" if p90 is None else f"{p90:,}")


def _span(ms: int) -> str:
    seconds = round(ms / 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes}m {seconds:02d}s"


def _align(rows: list[tuple[str, ...]], left: int) -> list[str]:
    """Rows as columns, the first `left` of them left-aligned and the rest right-aligned."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(c.ljust(w) if i < left else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths))).rstrip()
        for row in rows
    ]


@dataclass
class BargeStats:
    decisions: dict[str, int] = field(default_factory=lambda: {})
    # Milliseconds from the onset to the decision, per decision; the overheard stops have no onset.
    decided: dict[str, list[int]] = field(default_factory=lambda: {})
    # Milliseconds from the onset to the first partial transcript.
    first_partial: list[int] = field(default_factory=lambda: [])
    extended: int = 0
    # Resumes the log could judge, and those that the User likely repeated themselves after.
    judged_resumes: int = 0
    wrong_resumes: int = 0
    # Rows that are no JSON object, have no readable time, or are a Barge-in with a field missing or of the wrong type.
    skipped: int = 0


def summarise_bargein(rows: Iterable[object], since: datetime | None = None, until: datetime | None = None) -> BargeStats:
    stats = BargeStats()
    for row in rows:
        at = _at(row)
        if not isinstance(row, dict) or at is None:
            stats.skipped += 1
            continue
        row = cast(dict[str, Any], row)
        if row.get("kind") != "bargein" or (since and at < since) or (until and at >= until):
            continue
        decision, decided, partials = row.get("decision"), row.get("decided_ms"), row.get("partials")
        repeated = row.get("repeated")
        if (
            decision not in DECISIONS
            or not (decided is None or _is_int(decided))
            or not isinstance(partials, list)
            or not (repeated is None or isinstance(repeated, bool))
        ):
            stats.skipped += 1
            continue
        stats.decisions[decision] = stats.decisions.get(decision, 0) + 1
        if decided is not None:
            stats.decided.setdefault(decision, []).append(decided)
        first = cast(list[object], partials)[:1]
        if first and isinstance(first[0], dict) and _is_int(ms := cast(dict[str, Any], first[0]).get("ms")):
            stats.first_partial.append(ms)
        stats.extended += row.get("extended") is True
        if decision == "resume" and repeated is not None:
            stats.judged_resumes += 1
            stats.wrong_resumes += repeated
    return stats


def render_bargein(stats: BargeStats, since: datetime | None, until: datetime | None) -> str:
    def stamp(when: datetime | None, none: str) -> str:
        return none if when is None else when.astimezone().strftime("%Y-%m-%d %H:%M")

    out = [f"Jarvis Barge-ins from {stamp(since, 'the start')} to {stamp(until, 'now')}", ""]
    footer = [f"Skipped {stats.skipped} malformed rows."] if stats.skipped else []
    total = sum(stats.decisions.values())
    if not total:
        return "\n".join([*out, "No Barge-ins logged in this range.", *([""] + footer if footer else [])])
    table: list[tuple[str, ...]] = [("decision", "n", "share")]
    for decision in DECISIONS:
        if n := stats.decisions.get(decision):
            table.append((decision, str(n), f"{n / total:.0%}"))
    table.append(("total", str(total), ""))
    out += _align(table, left=1)
    latency: list[tuple[str, ...]] = [("metric", "n", "p50", "p90")]
    latency.append(_barge_latency("first partial", stats.first_partial))
    for decision in DECISIONS:
        if decision in stats.decided:
            latency.append(_barge_latency(f"onset to {decision}", stats.decided[decision]))
    out += ["", "Timing (ms)", *_align(latency, left=1), ""]
    out.append(f"Decisions that waited past the deadline for one more partial: {stats.extended}")
    if stats.judged_resumes:
        share = stats.wrong_resumes / stats.judged_resumes
        out.append(f"Likely wrong resumes: {stats.wrong_resumes} of {stats.judged_resumes} resumes ({share:.0%})")
    else:
        out.append("Likely wrong resumes: none judged yet")
    return "\n".join([*out, *([""] + footer if footer else [])])


def _barge_latency(metric: str, values: list[int]) -> tuple[str, ...]:
    p50, p90 = percentile(values, 50), percentile(values, 90)
    return (metric, str(len(values)), "-" if p50 is None else f"{p50:,}", "-" if p90 is None else f"{p90:,}")


def render_voice_trace(rows: Iterable[object], since: datetime | None, until: datetime | None) -> str:
    requests: dict[tuple[str, float], dict[str, Any]] = {}
    evidence: list[str] = []
    for raw in rows:
        at = _at(raw)
        if not isinstance(raw, dict) or at is None or (since and at < since) or (until and at >= until):
            continue
        row = cast(dict[str, Any], raw)
        run = str(row.get("run_id", "legacy"))
        kind, request = row.get("kind"), row.get("request_at")
        if isinstance(request, (int, float)) and kind in ("turn", "user", "speech", "speech_stop"):
            item = requests.setdefault((run, request), {})
            item.update(row)
            if kind == "speech_stop":
                item["superseded"] = True
            if item.get("superseded"):
                item["outcome"] = "superseded"
        if kind == "capture":
            evidence.append(
                f"{run} capture={row.get('captured_through_at')} queue_max_ms={row.get('max_queue_delay_ms')} "
                f"dropped={row.get('dropped_frames')}"
            )
        elif kind == "recognition":
            evidence.append(f"{run} candidate={row.get('candidate_at')} stt_ms={row.get('stt_ms')}")
        elif kind == "bargein":
            evidence.append(f"{run} candidate={row.get('onset')} decision={row.get('decision')} at={row.get('decided_at')}")
        elif kind == "playback_reference":
            evidence.append(
                f"{run} reference_block={row.get('block')} submitted={row.get('submitted_at')} "
                f"samples={row.get('samples')} rate={row.get('sample_rate')}"
            )
    table: list[tuple[str, ...]] = [
        ("run", "request_at", "turn", "endpoint ms", "STT ms", "Hub wait ms", "meaningful ms (estimated)", "outcome")
    ]
    for (run, request), item in requests.items():
        def cell(name: str) -> str:
            value = item.get(name)
            return "unavailable" if value is None else str(value)

        table.append((run, str(request), cell("turn"), cell("end_wait_ms"), cell("stt_ms"), cell("wait_ms"),
                      cell("first_meaningful_audio_ms"), cell("outcome")))
    return "\n".join([
        "Jarvis voice trace (perf_counter seconds within each run)", "", *_align(table, left=3), "",
        "Physical ADC/DAC timestamps: unavailable; meaningful audio uses the playback queue estimate.",
        "Cached speech and chimes are excluded. Missing evidence is unavailable, never zero.", *evidence,
    ])
