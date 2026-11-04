"""`jarvis-hub stats`: aggregating the metrics log per role, percentiles and time ranges."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.stats import parse_when, percentile, read_rows, render, render_bargein, summarise, summarise_bargein

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


def test_limit_hits_are_reported_even_when_no_turn_is_in_range() -> None:
    hit = {"at": NOW.isoformat(), "kind": "usage_limit", "role": "subagent", "resets_at": None}

    out = render(summarise([hit, hit]), None, None)

    assert "No turns logged in this range." in out and "Usage limit hits: 2" in out


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


def barge(
    decision: str,
    minutes_ago: float = 5,
    *,
    decided_ms: int | None = 700,
    first_ms: int | None = 400,
    repeated: bool | None = None,
    extended: bool = False,
) -> dict[str, Any]:
    partials = [] if first_ms is None else [{"text": "okay", "ms": first_ms, "speech_s": 0.4, "silent_s": 0.0}]
    return {
        "at": (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds"),
        "kind": "bargein",
        "decision": decision,
        "decided_ms": decided_ms,
        "partials": partials,
        "extended": extended,
        "repeated": repeated,
    }


def test_barge_ins_are_counted_by_decision_with_timing_and_likely_wrong_resumes() -> None:
    rows = [
        barge("resume", decided_ms=600, first_ms=400, repeated=False),
        barge("resume", decided_ms=1000, first_ms=400, repeated=True),
        barge("resume", decided_ms=800, first_ms=700, repeated=False, extended=True),
        barge("stop", decided_ms=450, first_ms=400),
        barge("wake", decided_ms=300, first_ms=None),
        barge("overheard-stop", decided_ms=None, first_ms=None),
        row("talker", 1, usage=(1, 1, 1, 1)),
    ]

    stats = summarise_bargein(rows)

    assert stats.decisions == {"resume": 3, "stop": 1, "wake": 1, "overheard-stop": 1}
    assert stats.decided["resume"] == [600, 1000, 800] and "overheard-stop" not in stats.decided
    assert stats.first_partial == [400, 400, 700, 400]
    assert (stats.judged_resumes, stats.wrong_resumes, stats.extended, stats.skipped) == (3, 1, 1, 0)


def test_resumes_the_log_could_not_judge_are_left_out_of_the_wrong_resume_share() -> None:
    stats = summarise_bargein([barge("resume", repeated=None), barge("resume", repeated=True)])

    assert (stats.judged_resumes, stats.wrong_resumes) == (1, 1)


def test_malformed_barge_in_rows_are_skipped_and_counted_and_other_rows_ignored() -> None:
    good = barge("stop")
    rows: list[object] = [
        good,
        None,
        "x",
        {"kind": "bargein"},
        {**good, "decision": "shrug"},
        {**good, "decided_ms": "fast"},
        {**good, "partials": "okay"},
        {**good, "repeated": "yes"},
        {**good, "at": "yesterday"},
        {**good, "decided_ms": True},
        {"at": good["at"], "kind": "voice"},
    ]

    stats = summarise_bargein(rows)

    assert stats.decisions == {"stop": 1}
    assert stats.skipped == 9


def test_barge_in_rows_with_odd_partials_still_count() -> None:
    odd = {**barge("stop"), "partials": ["okay", {"ms": "soon"}]}
    stats = summarise_bargein([odd, {**odd, "partials": [{"text": "x", "ms": "soon"}]}, {**odd, "partials": []}])

    assert stats.decisions == {"stop": 3} and stats.first_partial == []


def test_the_barge_in_range_keeps_rows_from_since_up_to_but_not_including_until() -> None:
    rows = [barge("stop", 90), barge("stop", 60), barge("resume", 30), barge("stop", 0)]

    stats = summarise_bargein(rows, NOW - timedelta(minutes=60), NOW)

    assert stats.decisions == {"stop": 1, "resume": 1}


def test_the_barge_in_report_has_counts_percentiles_and_the_wrong_resume_share() -> None:
    rows = [
        barge("resume", decided_ms=600, first_ms=400, repeated=False),
        barge("resume", decided_ms=1000, first_ms=400, repeated=True),
        barge("stop", decided_ms=450, first_ms=400),
        barge("overheard-stop", decided_ms=None, first_ms=None),
    ]

    text = render_bargein(summarise_bargein(rows), None, NOW)

    assert text.splitlines()[0].startswith("Jarvis Barge-ins from the start to ")
    lines = [line.split() for line in text.splitlines()]
    assert ["resume", "2", "50%"] in lines and ["overheard-stop", "1", "25%"] in lines and ["total", "4"] in lines
    assert "onset to resume" in text and "1,000" in text
    assert "first partial" in text
    assert "Likely wrong resumes: 1 of 2 resumes (50%)" in text


def test_an_empty_barge_in_range_says_so_and_reports_skipped_rows() -> None:
    assert "No Barge-ins logged in this range." in render_bargein(summarise_bargein([]), None, None)
    assert "Skipped 1 malformed rows." in render_bargein(summarise_bargein([None]), None, None)


def test_the_stats_command_reads_the_barge_in_log_only_with_the_flag(tmp_path: Path) -> None:
    import io

    from jarvis_hub.config import load_settings
    from jarvis_hub.main import print_stats

    settings = load_settings(tmp_path, {})
    log = settings.runtime_dir / "metrics.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps(barge("stop", 1)) + "\n")
    plain, flagged = io.StringIO(), io.StringIO()

    print_stats(settings, None, None, plain)
    print_stats(settings, None, None, flagged, bargein=True)

    assert "Barge-ins" not in plain.getvalue() and "No turns logged" in plain.getvalue()
    assert "stop" in flagged.getvalue() and "Barge-ins" in flagged.getvalue()
