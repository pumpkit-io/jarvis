"""`jarvis-hub stats`: aggregating the metrics log per role, percentiles and time ranges."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.stats import parse_when, percentile, read_rows, render, summarise

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def row(role: str, minutes_ago: float, *, usage: tuple[int, int, int, int] = (0, 0, 0, 0), **fields: Any) -> dict[str, Any]:
    names = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")
    return {"at": at, "kind": "user", "role": role, "usage": dict(zip(names, usage)), "duration_ms": 0, **fields}


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([], 50, None),
        ([7], 90, 7),
        ([5, 1, 3], 50, 3),
        ([1, 2, 3, 4], 50, 2),
        (list(range(1, 11)), 90, 9),
        (list(range(1, 11)), 100, 10),
    ],
)
def test_a_percentile_is_the_nearest_rank(values: list[int], p: float, expected: int | None) -> None:
    assert percentile(values, p) == expected


def test_totals_add_up_per_role() -> None:
    rows = [
        row("talker", 5, usage=(10, 20, 30, 40), duration_ms=1000, first_sentence_ms=600),
        row("talker", 4, usage=(1, 2, 3, 4), duration_ms=500, first_sentence_ms=800),
        row("orchestrator", 3, usage=(100, 200, 300, 400), duration_ms=9000),
        row("subagent", 2, usage=(5, 6, 7, 8), duration_ms=60_000),
    ]

    stats = summarise(rows)

    talker = stats.roles["talker"]
    assert (talker.turns, talker.input_tokens, talker.output_tokens) == (2, 11, 22)
    assert (talker.cache_read_tokens, talker.cache_creation_tokens, talker.duration_ms) == (33, 44, 1500)
    assert stats.roles["orchestrator"].input_tokens == 100
    assert stats.roles["subagent"].duration_ms == 60_000
    assert stats.first_sentence == [600, 800]


def test_rows_that_are_not_a_session_result_are_not_counted_as_turns() -> None:
    rows = [
        {"at": NOW.isoformat(), "kind": "voice", "from": "idle", "to": "listening"},
        {"at": NOW.isoformat(), "kind": "speech", "turn": 1},
        {"at": NOW.isoformat(), "kind": "usage_limit", "role": "subagent", "resets_at": 5.0},
        row("talker", 1, usage=(1, 1, 1, 1)),
    ]

    stats = summarise(rows)

    assert {role: t.turns for role, t in stats.roles.items()} == {"talker": 1}
    assert stats.limit_hits == 1


def test_malformed_rows_are_skipped_and_counted() -> None:
    good = row("talker", 1, usage=(3, 0, 0, 0), duration_ms=10)
    rows: list[object] = [
        good,
        None,
        ["not", "an", "object"],
        {**good, "at": "yesterday"},
        {**good, "usage": {"input_tokens": "3"}},
        {**good, "usage": "3 tokens"},
        {**good, "duration_ms": None},
        {key: value for key, value in good.items() if key != "duration_ms"},
        {**good, "role": "stranger"},
    ]

    stats = summarise(rows)

    assert (stats.roles["talker"].turns, stats.roles["talker"].input_tokens, stats.skipped) == (1, 3, 8)
    assert "Skipped 8 malformed rows." in render(stats, None, None)


def test_the_range_keeps_rows_from_since_up_to_but_not_including_until() -> None:
    rows = [row("talker", m, usage=(m, 0, 0, 0)) for m in (90, 60, 30, 10)]

    stats = summarise(rows, since=NOW - timedelta(minutes=60), until=NOW - timedelta(minutes=10))

    assert stats.roles["talker"].input_tokens == 60 + 30


def test_an_empty_range_says_so() -> None:
    assert "No turns logged in this range." in render(summarise([]), None, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", NOW - timedelta(minutes=30)),
        ("24h", NOW - timedelta(hours=24)),
        ("7d", NOW - timedelta(days=7)),
        ("2w", NOW - timedelta(weeks=2)),
        ("2026-11-01T08:00:00+00:00", datetime(2026, 11, 1, 8, tzinfo=timezone.utc)),
    ],
)
def test_a_time_flag_is_a_time_ago_or_a_date(text: str, expected: datetime) -> None:
    assert parse_when(text, NOW) == expected


def test_a_bare_date_is_local_midnight() -> None:
    assert parse_when("2026-11-01", NOW) == datetime(2026, 11, 1).astimezone()


def test_a_bare_date_as_the_end_of_the_range_includes_that_whole_day() -> None:
    assert parse_when("2026-11-01", NOW, end=True) == datetime(2026, 11, 2).astimezone()
    assert parse_when("2026-11-01T08:00", NOW, end=True) == datetime(2026, 11, 1, 8).astimezone()
    assert parse_when("24h", NOW, end=True) == NOW - timedelta(hours=24)


def test_a_time_flag_that_is_neither_is_refused() -> None:
    with pytest.raises(ValueError, match="neither a time ago"):
        parse_when("yesterday", NOW)


def test_the_log_is_read_past_a_line_cut_short(tmp_path: Path) -> None:
    log = tmp_path / "metrics.jsonl"
    log.write_text(json.dumps(row("talker", 1)) + '\n{"at": "2026-11\n\n' + json.dumps(row("subagent", 1)) + "\n")

    stats = summarise(read_rows(log))

    assert ({role: t.turns for role, t in stats.roles.items()}, stats.skipped) == ({"talker": 1, "subagent": 1}, 1)
    assert list(read_rows(tmp_path / "none.jsonl")) == []


def test_the_report_has_a_row_per_role_a_total_and_latency_percentiles() -> None:
    rows = [
        row("talker", 5, usage=(10, 20, 30, 40), duration_ms=1000, first_sentence_ms=600),
        row("talker", 4, usage=(1, 2, 3, 4), duration_ms=3000, first_sentence_ms=1200),
        row("subagent", 2, usage=(5, 6, 7, 8), duration_ms=3_725_000),
        {"at": NOW.isoformat(), "kind": "usage_limit", "role": "subagent"},
    ]

    text = render(summarise(rows), NOW - timedelta(days=1), None)

    lines = text.splitlines()
    assert lines[0].startswith("Jarvis usage from ") and lines[0].endswith("to now")
    assert [l.split()[:3] for l in lines if l.startswith(("talker", "subagent", "total"))][:3] == [
        ["talker", "2", "11"],
        ["subagent", "1", "5"],
        ["total", "3", "16"],
    ]
    assert "1h 02m 05s" in text
    assert "Usage limit hits: 1" in text
    assert any(l.split() == ["talker", "first", "sentence", "2", "600", "1,200"] for l in lines)
    assert any(l.split() == ["talker", "turn", "duration", "2", "1,000", "3,000"] for l in lines)
    assert any(l.split() == ["subagent", "turn", "duration", "1", "3,725,000", "3,725,000"] for l in lines)
    assert "$" not in text and "cost" not in text.lower()
