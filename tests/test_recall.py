import asyncio
import io
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub import event_log
from jarvis_hub.event_log import FOLLOWED_CHARS, MATCH_CHARS, RECALL_CHARS, EventLog, EventOutput, Recall
from tests.fakes import FakeBackend, FakeClock, Gate, RecordingMetrics, RecordingOutput, Text, Tool, lines, make_hub

DAY = 86400.0
# Sunday 22 November 2026, 12:00 UTC.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class Wall:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def exchange(log: EventLog, wall: Wall, at: float, said: str, replied: str) -> None:
    wall.now = at
    log.append("heard", {"request_id": f"r{at}", "text": said, "spoken": True})
    log.append("reply_delta", {"reply_id": f"p{at}", "request_id": f"r{at}", "text": replied})
    log.append("reply_end", {"reply_id": f"p{at}", "request_id": f"r{at}", "text": replied, "reason": "complete"})
    wall.now = NOW


def shown_chars(found: Recall) -> int:
    """The text a search shows, escaped as the Talker reads it: what the budget of about 2,000 tokens counts."""
    shown = [m.text for m in found.matches] + [m.followed.text for m in found.matches if m.followed]
    return sum(len(escape(text, quote=False)) for text in shown)


def test_recall_finds_what_the_user_said_dated_with_what_followed(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - 3 * DAY, "The lighthouse key goes under the blue mat.", "Noted, sir: under the blue mat.")
        exchange(log, wall, NOW - DAY, "Book the dentist.", "I can't book things, sir.")

        found = log.search("lighthouse")

    [match] = found.matches
    assert (match.source, match.text) == ("user", "The lighthouse key goes under the blue mat.")
    assert match.time == NOW - 3 * DAY
    assert match.followed is not None
    assert (match.followed.source, match.followed.text) == ("jarvis", "Noted, sir: under the blue mat.")
    shown = found.render()
    assert '<match at="2026-11-19 12:00 Thursday" from="user">' in shown
    assert '<followed from="jarvis">Noted, sir: under the blue mat.</followed>' in shown
    assert 'today="2026-11-22 Sunday"' in shown


def test_recall_searches_replies_shown_details_and_task_reports_but_not_partial_replies_or_notices(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        log.append("reply_delta", {"reply_id": "p1", "request_id": None, "text": "The harbour"})
        log.append("show", {"reply_id": "p1", "title": "Details", "text": "| harbour | open |"})
        log.append("reply_end", {"reply_id": "p1", "request_id": None, "text": "The harbour plan is ready.", "reason": "complete"})
        log.append("notice", {"text": "Harbour notice."})
        log.append("task_report", {"task_id": "T3", "text": "The harbour PR is open.", "details": "PR 91 renames the harbour module."})

        found = log.search("harbour")

    assert [(match.source, match.text, match.task_id) for match in found.matches] == [
        ("screen", "| harbour | open |", None),
        ("jarvis", "The harbour plan is ready.", None),
        ("task", "The harbour PR is open.\n\nPR 91 renames the harbour module.", "T3"),
    ]
    assert '<match at="2026-11-22 12:00 Sunday" from="task" task="T3">' in found.render()


def test_every_word_must_appear_and_a_quoted_phrase_as_written(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - DAY, "The spare key is under the blue mat.", "Very good, sir.")
        exchange(log, wall, NOW - DAY + 60, "The mat by the door is blue.", "Noted.")

        assert len(log.search("blue mat").matches) == 2
        assert [m.text for m in log.search('"blue mat"').matches] == ["The spare key is under the blue mat."]
        assert log.search("blue mat garage").matches == ()
        # FTS5 syntax in what the User said is searched as words, never parsed.
        assert log.search('blue AND (mat* OR "unclosed').matches == ()
        with pytest.raises(ValueError):
            log.search(' "" ? ')


def test_the_speaker_filter_keeps_the_users_words_or_jarviss(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - DAY, "What about the orchard?", "The orchard needs pruning, sir.")

        assert [m.source for m in log.search("orchard", speaker="user").matches] == ["user"]
        assert [m.source for m in log.search("orchard", speaker="jarvis").matches] == ["jarvis"]
        assert [m.source for m in log.search("orchard").matches] == ["user", "jarvis"]


def test_a_date_range_keeps_only_its_matches_and_a_range_inside_the_log_reaches_no_edge(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        for days in (10, 5, 1):
            exchange(log, wall, NOW - days * DAY, f"Orchard note {days}.", "Noted.")

        found = log.search("orchard", start=NOW - 6 * DAY, end=NOW - 2 * DAY)
        everything = log.search("orchard")

    assert [m.text for m in found.matches] == ["Orchard note 5."]
    assert found.edge is None
    assert "<edge>" not in found.render()
    assert 'from="2026-11-16 12:00 Monday" to="2026-11-20 11:59 Friday"' in found.render()
    assert everything.edge == "retention"
    assert ("<edge>This search reached the edge of the 30 days the log keeps (2026-10-23 12:00 Friday): "
            "anything older is only in Memory.</edge>") in everything.render()


def test_at_most_ten_matches_fit_in_about_two_thousand_tokens(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        for minute in range(15):
            filler = "and the meadow again " * 60
            exchange(log, wall, NOW - DAY + minute * 60, f"Meadow {minute}: {filler}", f"Meadow reply {minute}. {filler}")

        found = log.search("meadow")
        three = log.search("meadow", limit=3)
        many = log.search("meadow", limit=50)

    assert 0 < len(found.matches) <= 10
    assert found.more
    assert shown_chars(found) <= RECALL_CHARS
    # An excerpt cut on both sides gains an ellipsis at each end.
    assert all(len(match.text) <= MATCH_CHARS + len("……") for match in found.matches)
    assert all(match.followed is not None and len(match.followed.text) <= FOLLOWED_CHARS for match in found.matches)
    assert [m.time for m in found.matches] == sorted(m.time for m in found.matches)
    assert len(three.matches) == 3 and three.more
    assert len(many.matches) <= 10
    assert "<more>" in found.render()


def test_an_event_older_than_thirty_days_is_never_returned_pruned_or_not(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    wall = Wall(NOW - 31 * DAY)
    with EventLog(path, wall=wall) as log:
        log.append("heard", {"request_id": "r1", "text": "The quince jam recipe.", "spoken": True})
        wall.now = NOW - 2 * DAY
        log.append("heard", {"request_id": "r2", "text": "More quince jam.", "spoken": True})
        wall.now = NOW
        assert [m.text for m in log.search("quince").matches] == ["More quince jam."]
    with EventLog(path, wall=wall) as log:
        assert [m.text for m in log.search("quince").matches] == ["More quince jam."]


def test_recall_ignores_everything_before_the_stored_reset_time(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    wall = Wall()
    with EventLog(path, wall=wall) as log:
        assert log.reset_time is None
        exchange(log, wall, NOW - 3 * DAY, "The walnut tree is mine.", "Of course, sir.")
        exchange(log, wall, NOW - DAY, "The walnut tree is my sister's.", "Noted, sir.")
        log.set_reset_time(NOW - 2 * DAY)
    with EventLog(path, wall=wall) as log:
        assert log.reset_time == NOW - 2 * DAY
        found = log.search("walnut", start=NOW - 10 * DAY)
        narrower = log.search("walnut", start=NOW - 1.5 * DAY)

    assert [m.text for m in found.matches] == ["The walnut tree is my sister's."]
    assert found.edge == "reset"
    assert ("<edge>This search reached the last Reset context (2026-11-20 12:00 Friday): "
            "recall ignores everything before it.</edge>") in found.render()
    assert narrower.edge is None


def test_secrets_in_results_are_redacted(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - DAY, "The router password is Plum-tree42 for the cellar.", "The password is Plum-tree42, sir.")
        log.append("task_report", {"task_id": "T1", "text": "Cellar router set up.",
                                   "details": "Token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"})

        found = log.search("cellar router")

    shown = found.render()
    assert "Plum-tree42" not in shown and "ghp_" not in shown
    assert "The router password is [secret removed] for the cellar." in shown
    assert '<followed from="jarvis">The password is [secret removed], sir.</followed>' in shown


def old_log(path: Path, events: int, *, at: float = NOW - DAY) -> None:
    """A log written before recall existed: the events table alone, with no index."""
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, "
                   "type TEXT NOT NULL, client_id TEXT, payload TEXT NOT NULL)")
        db.executemany("INSERT INTO events(time, type, client_id, payload) VALUES (?, 'heard', 'terminal', ?)",
                       [(at + n, f'{{"request_id": "r{n}", "text": "Old line {n} about the granary.", "spoken": true}}')
                        for n in range(events)])
    db.close()


def test_a_large_existing_log_is_indexed_in_the_background_while_new_events_are_searchable(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    old_log(path, 3000)
    wall = Wall()
    with EventLog(path, wall=wall) as log:
        log.start_backfill(record=RecordingMetrics().record, batch=20)
        log.append("heard", {"request_id": "new", "text": "A new line about the granary roof.", "spoken": True})

        early = log.search('"granary roof"')
        assert [m.text for m in early.matches] == ["A new line about the granary roof."]
        assert early.indexing
        assert "<indexing>" in early.render()

        deadline = time.monotonic() + 30
        while log.search("granary").indexing:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert [m.text for m in log.search('"old line 0"').matches] == ["Old line 0 about the granary."]
        assert len(log.search("granary").matches) == 10


def test_a_backfill_stopped_by_closing_resumes_at_the_next_launch(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    old_log(path, 3000)
    wall = Wall()
    with EventLog(path, wall=wall) as log:
        log.start_backfill(record=RecordingMetrics().record, batch=20)
    with EventLog(path, wall=wall) as log:
        assert log.search("granary").indexing
        log.start_backfill(record=RecordingMetrics().record)
        deadline = time.monotonic() + 30
        while log.search("granary").indexing:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert [m.text for m in log.search('"old line 0"').matches] == ["Old line 0 about the granary."]
        assert [m.text for m in log.search('"old line 2999"').matches] == ["Old line 2999 about the granary."]


async def test_the_talkers_recall_call_reaches_the_search_with_its_dates_and_speaker(tmp_path: Path) -> None:
    wall = Wall()
    backend = FakeBackend(FakeClock(), [[
        Tool("recall", {"query": "lighthouse", "from": "2026-11-18", "to": "2026-11-19", "speaker": "user", "limit": 5}),
        Tool("recall", {"query": "lighthouse", "from": "last Tuesday"}),
        Tool("recall", {"query": "lighthouse", "to": "9999-12-31"}),
        Tool("recall", {"query": "lighthouse", "to": "0001-01-01"}),
        Tool("recall", {"query": "lighthouse", "from": "0001-01-01"}),
        Text("Under the blue mat, sir."),
    ]])
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - 5 * DAY, "The lighthouse key is in the shed.", "Noted, sir.")
        exchange(log, wall, NOW - 3 * DAY, "The lighthouse key goes under the blue mat.", "Noted, sir.")
        exchange(log, wall, NOW - DAY, "The lighthouse is closed today.", "A pity, sir.")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics(), wall=wall)
        await hub.start()
        try:
            await hub.run(lines(backend.clock, "Where did I say the lighthouse key goes?"))
        finally:
            await hub.close()

    dated, undated, endless, ancient, since_ancient = [
        result for result in backend.session("talker").tool_results if result.name == "recall"]
    assert dated.result is not None
    assert "The lighthouse key goes under the blue mat." in dated.result
    assert "shed" not in dated.result and "closed today" not in dated.result
    assert 'from="2026-11-18 00:00 Wednesday" to="2026-11-19 23:59 Thursday"' in dated.result
    assert {undated.result, endless.result, ancient.result, since_ancient.result} == {
        "from and to are dates such as 2026-11-15."}


async def test_a_task_report_is_logged_and_found_by_its_content(tmp_path: Path) -> None:
    wall = Wall()
    backend = FakeBackend(FakeClock(), [
        [Tool("delegate", {"request": "Review the login flow"}), Text("On it.")],
        [Text("The review is done, sir.")],
    ], orchestrator=[[Tool("report", {
        "task_id": "T1", "spoken": "The login review found a missing rate limit.",
        "details_markdown": "- `auth/session.py` lacks throttling on the token refresh.",
    })]])
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics(), wall=wall)
        await hub.start()
        try:
            await hub.run(lines(backend.clock, "Review the login flow"))
        finally:
            await hub.close()

        found = log.search("throttling", speaker="jarvis")

    report = next(match for match in found.matches if match.source == "task")
    assert report.task_id == "T1"
    assert report.text == ("The login review found a missing rate limit.\n\n"
                           "- `auth/session.py` lacks throttling on the token refresh.")
    assert report.followed is not None and report.followed.text == "The review is done, sir."


async def test_text_mode_indexes_an_existing_log_in_the_background_and_recall_finds_it(tmp_path: Path) -> None:
    from tests.test_text_mode import converse, settings

    config = settings(tmp_path)
    path = config.runtime_dir / "events.sqlite3"
    path.parent.mkdir(parents=True)
    old_log(path, 50, at=time.time() - 3600)
    indexed = asyncio.Event()

    async def wait_for_the_backfill() -> None:
        while True:
            try:
                with closing(sqlite3.connect(path)) as db:
                    row = db.execute("SELECT through FROM recall_backfill").fetchone()
            except sqlite3.OperationalError:
                row = None
            if row == (0,):
                indexed.set()
                return
            await asyncio.sleep(0.01)

    backend = FakeBackend(FakeClock(), [[Gate(indexed), Tool("recall", {"query": '"old line 7"'}), Text("Yes, sir.")]])
    waiting = asyncio.create_task(wait_for_the_backfill())
    try:
        await converse(backend, io.StringIO("Did I mention the granary?\n"), config=config)
    finally:
        waiting.cancel()

    [found] = [result for result in backend.session("talker").tool_results if result.name == "recall"]
    assert found.result is not None and "Old line 7 about the granary." in found.result


def test_a_line_jarvis_never_answered_is_not_paired_with_a_later_reply(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        wall.now = NOW - 3 * DAY
        log.append("heard", {"request_id": "errored", "text": "What about the pergola?", "spoken": True})
        log.append("reply_end", {"reply_id": "p1", "request_id": "errored", "text": "", "reason": "error"})
        log.append("heard", {"request_id": "unanswered", "text": "And the pergola paint?", "spoken": True})
        exchange(log, wall, NOW - DAY, "Book the dentist.", "I can't book things, sir.")

        found = log.search("pergola")

    assert [(m.text, m.followed) for m in found.matches] == [
        ("What about the pergola?", None), ("And the pergola paint?", None)]


def test_a_reply_is_followed_only_by_the_users_next_line_in_the_same_exchange(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - 3 * DAY, "Tell me about the pergola.", "The pergola faces south, sir.")
        exchange(log, wall, NOW - DAY, "Book the dentist.", "I can't book things, sir.")
        exchange(log, wall, NOW - 2 * 3600, "Is the pergola stain dry?", "The pergola stain needs a day, sir.")
        exchange(log, wall, NOW - 2 * 3600 + 60, "Thanks.", "My pleasure, sir.")

        found = log.search("pergola", speaker="jarvis")

    assert [(m.text, m.followed and m.followed.text) for m in found.matches] == [
        ("The pergola faces south, sir.", None), ("The pergola stain needs a day, sir.", "Thanks.")]


def test_shown_details_are_followed_by_the_reply_they_were_shown_with(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        log.append("reply_end", {"reply_id": "other", "request_id": "r0", "text": "Unrelated, sir.", "reason": "complete"})
        log.append("show", {"reply_id": "p1", "title": "Details", "text": "| pergola | 3 m |"})
        log.append("reply_end", {"reply_id": "early", "request_id": "r1", "text": "A reply to something else.", "reason": "complete"})
        log.append("reply_end", {"reply_id": "p1", "request_id": "r2", "text": "The pergola is three metres, sir.", "reason": "complete"})

        [shown] = log.search("pergola", speaker="jarvis").matches[:1]

    assert shown.followed is not None and shown.followed.text == "The pergola is three metres, sir."


def test_a_task_report_is_followed_by_the_announcement_not_a_reply_to_the_user(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        log.append("task_report", {"task_id": "T2", "text": "The pergola PR is open.", "details": None})
        log.append("reply_end", {"reply_id": "p1", "request_id": "r1", "text": "Dinner is at eight, sir.", "reason": "complete"})
        log.append("reply_end", {"reply_id": "p2", "request_id": None, "text": "Sir, the pergola PR is open.", "reason": "complete"})

        [report] = log.search("pergola PR", speaker="jarvis").matches[:1]

    assert report.followed is not None and report.followed.text == "Sir, the pergola PR is open."


def test_when_the_budget_runs_out_the_newest_matches_are_kept(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        for days in range(10, 0, -1):
            # Every `&` is five characters once escaped, so ten of these overrun about 2,000 tokens.
            exchange(log, wall, NOW - days * DAY, f"Meadow note {days}: " + "R&D " * 100, "Noted.")

        found = log.search("meadow")

    assert 0 < len(found.matches) < 10
    assert found.more
    assert shown_chars(found) <= RECALL_CHARS
    assert found.matches[-1].text.startswith("Meadow note 1:")
    assert [m.time for m in found.matches] == sorted(m.time for m in found.matches)


def test_a_long_match_is_cut_around_the_word_even_when_its_accents_differ(tmp_path: Path) -> None:
    wall = Wall()
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - DAY, "We walked along the river. " * 40 + "Then we stopped at the Café Rosé.", "Lovely, sir.")

        [match] = log.search("cafe rose", speaker="user").matches

    assert match.text.startswith("…") and match.text.endswith("Then we stopped at the Café Rosé.")


@pytest.mark.parametrize(("said", "query", "word"), [
    ("the build runs nightly", "running", "runs"),
    ("the crème brûlée was burnt", "creme brulee", "crème brûlée"),
    ("the creme brulee was burnt", "Crème Brûlée", "creme brulee"),
])
def test_a_long_match_is_cut_around_any_word_the_index_matched(tmp_path: Path, said: str, query: str, word: str) -> None:
    wall = Wall()
    filler = "We walked along the river and talked about the weather. " * 14
    with EventLog(tmp_path / "events.sqlite3", wall=wall) as log:
        exchange(log, wall, NOW - DAY, f"{filler}Then {said}. {filler}", "Noted, sir.")

        [match] = log.search(query, speaker="user").matches

    assert match.text.startswith("…") and match.text.endswith("…")
    assert word in match.text


def failing_backfill(path: Path, *, fail: bool = True) -> None:
    """Make every backfill batch fail, or succeed again, as a locked database would."""
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        if fail:
            db.execute("CREATE TRIGGER fail BEFORE UPDATE ON recall_backfill "
                       "BEGIN SELECT RAISE(ABORT, 'database is locked'); END")
        else:
            db.execute("DROP TRIGGER fail")


def wait_for(condition: Callable[[], object]) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_a_failed_backfill_batch_is_logged_and_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_log, "BACKFILL_RETRY", 0.5)
    path = tmp_path / "events.sqlite3"
    old_log(path, 100)
    rows: list[dict[str, Any]] = []
    with EventLog(path, wall=Wall()) as log:
        failing_backfill(path)
        log.start_backfill(record=rows.append, batch=20)
        wait_for(lambda: rows)
        failing_backfill(path, fail=False)

        wait_for(lambda: not log.search("granary").indexing)
        assert [m.text for m in log.search('"old line 0"').matches] == ["Old line 0 about the granary."]

    assert rows[0] == {"kind": "recall_backfill", "error": "IntegrityError: database is locked", "attempt": 1,
                       "next": "retry"}


def test_a_backfill_that_keeps_failing_stops_and_the_next_launch_carries_on(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(event_log, "BACKFILL_RETRY", 0.01)
    path = tmp_path / "events.sqlite3"
    old_log(path, 100)
    rows: list[dict[str, Any]] = []
    with EventLog(path, wall=Wall()) as log:
        failing_backfill(path)
        log.start_backfill(record=rows.append, batch=20)
        wait_for(lambda: rows and rows[-1]["next"] == "next launch")
        assert log.search("granary").indexing
    assert [row["attempt"] for row in rows] == [1, 2, 3]

    failing_backfill(path, fail=False)
    with EventLog(path, wall=Wall()) as log:
        log.start_backfill(record=rows.append, batch=20)
        wait_for(lambda: not log.search("granary").indexing)
        assert len(log.search("granary").matches) == 10
    assert len(rows) == 3


def test_closing_ends_a_backfill_waiting_to_retry(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    old_log(path, 100)
    rows: list[dict[str, Any]] = []
    log = EventLog(path, wall=Wall())
    failing_backfill(path)
    log.start_backfill(record=rows.append, batch=20)
    wait_for(lambda: rows)

    began = time.monotonic()
    log.close()

    assert time.monotonic() - began < 5
    assert len(rows) == 1
