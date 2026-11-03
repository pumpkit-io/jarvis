"""`jarvis-hub stats`: what the metrics log says about usage and latency, per role, over a time range."""

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROLES = ("talker", "orchestrator", "subagent")
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


def parse_when(text: str, now: datetime) -> datetime:
    """A time flag: how long ago (`30m`, `24h`, `7d`, `2w`), or a date or date and time (local unless it has an offset)."""
    if match := _AGO.fullmatch(text.strip()):
        return now - timedelta(**{_UNITS[match[2]]: int(match[1])})
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"{text!r} is neither a time ago like 24h or 7d nor a date like 2026-11-01") from None
    return when if when.tzinfo else when.astimezone()


def percentile(values: list[int], p: float) -> int | None:
    """Nearest rank: the smallest value that at least p percent of the values do not exceed."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Every row of the log; a line that is not a JSON object (a write cut short) is skipped."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)  # pyright: ignore[reportUnknownArgumentType]
    return rows


def summarise(rows: Iterable[dict[str, Any]], since: datetime | None = None, until: datetime | None = None) -> Stats:
    stats = Stats()
    for row in rows:
        try:
            at = datetime.fromisoformat(row["at"])
        except (KeyError, ValueError, TypeError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if (since and at < since) or (until and at >= until):
            continue
        if row.get("kind") == "usage_limit":
            stats.limit_hits += 1
        if row.get("role") not in ROLES or not isinstance(row.get("usage"), dict):
            continue
        totals = stats.roles.setdefault(row["role"], Totals())
        usage = row["usage"]
        totals.turns += 1
        totals.input_tokens += usage.get("input_tokens", 0)
        totals.output_tokens += usage.get("output_tokens", 0)
        totals.cache_read_tokens += usage.get("cache_read_tokens", 0)
        totals.cache_creation_tokens += usage.get("cache_creation_tokens", 0)
        if isinstance(duration := row.get("duration_ms"), int):
            totals.duration_ms += duration
            totals.durations.append(duration)
        if row["role"] == "talker" and isinstance(first := row.get("first_sentence_ms"), int):
            stats.first_sentence.append(first)
    return stats


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
    if len(table) == 1:
        return "\n".join([*out, "No turns logged in this range."])
    table.append(_cells("total", total))
    out += _align(table, left=1)
    out += ["", f"Usage limit hits: {stats.limit_hits}", "", "Latency (ms)"]
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
