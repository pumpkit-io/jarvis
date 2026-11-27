import asyncio
import io
import threading
from pathlib import Path

import pytest

from jarvis_hub.event_log import EventLog, EventOutput
from tests.fakes import FakeBackend, FakeClock, RecordingOutput, Text, Tool
from tests.test_text_mode import converse, settings, user_lines



def test_events_are_durable_and_sequences_increase_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    with EventLog(path, wall=lambda: 1000.0) as log:
        first = log.append("heard", {"text": "Hello", "typed": True})
        second = log.append("reply_delta", {"text": "Evening, sir."})
        assert (first.seq, second.seq) == (1, 2)
    with EventLog(path, wall=lambda: 1001.0) as log:
        third = log.append("reply_end", {})
        assert [(event.seq, event.type, event.client_id, event.payload) for event in log.after(0)] == [
            (1, "heard", "terminal", {"text": "Hello", "typed": True}),
            (2, "reply_delta", "terminal", {"text": "Evening, sir."}),
            (3, "reply_end", "terminal", {}),
        ]
        assert first.time == 1000.0
        assert third.seq == 3


def test_launch_prunes_only_events_older_than_thirty_days_without_reusing_sequences(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    day = 86400.0
    with EventLog(path, wall=lambda: 0.0) as log:
        log.append("notice", {"text": "old"})
    with EventLog(path, wall=lambda: day) as log:
        log.append("notice", {"text": "boundary"})
    with EventLog(path, wall=lambda: 2 * day) as log:
        log.append("notice", {"text": "recent"})
    with EventLog(path, wall=lambda: 31 * day) as log:
        assert [event.payload["text"] for event in log.after(0)] == ["boundary", "recent"]
    with EventLog(path, wall=lambda: 100 * day) as log:
        assert log.after(0) == []
        assert log.append("notice", {"text": "new"}).seq == 4


async def test_terminal_conversation_logs_output_and_does_not_rehydrate_the_talker(tmp_path: Path) -> None:
    config = settings(tmp_path)
    backend = FakeBackend(FakeClock(), [[Text("Here, sir."), Tool("show", {"markdown": "# Plan"})]])
    stdout, stderr = await converse(backend, io.StringIO("Show me\n"), config=config)

    with EventLog(config.runtime_dir / "events.sqlite3") as log:
        events = log.after(0)
        request_id = events[2].payload["request_id"]
        reply_id = events[3].payload["reply_id"]
        assert [(event.type, event.payload) for event in events] == [
            ("notice", {"text": "Model loading."}),
            ("notice", {"text": "Model ready."}),
            ("heard", {"request_id": request_id, "text": "Show me", "spoken": False}),
            ("reply_delta", {"reply_id": reply_id, "request_id": request_id, "text": "Here, sir."}),
            ("show", {"reply_id": reply_id, "title": "Details", "text": "# Plan"}),
            ("reply_end", {"reply_id": reply_id, "request_id": request_id, "text": "Here, sir.", "reason": "complete"}),
        ]
    assert stdout.startswith("› Show me\nHere, sir.\n")
    assert "Plan" in stdout and "# Plan" not in stdout
    assert stderr == "[hub] Model loading.\n[hub] Model ready.\n"
    restarted = FakeBackend(FakeClock(), [[Text("Morning, sir.")]])
    await converse(restarted, io.StringIO("Hello\n"), config=config)
    assert user_lines(restarted) == ['<user lang="en" typed="true">Hello</user>']


@pytest.mark.parametrize("kind", ["heard", "reply", "reply_end", "notice", "show"])
def test_output_is_durable_before_it_reaches_the_screen(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "events.sqlite3"

    class CheckingScreen(RecordingOutput):
        def record(self, expected: str) -> None:
            with EventLog(path) as reader:
                [event] = reader.after(0)
                assert event.type == expected

        def heard(self, text: str, *, typed: bool = True, client_id: str = "terminal") -> None:
            self.record("heard")

        def reply(self, text: str) -> None:
            self.record("reply_delta")

        def reply_end(self) -> None:
            self.record("reply_end")

        def notice(self, text: str) -> None:
            self.record("notice")

        def show(self, markdown: str) -> None:
            self.record("show")

    with EventLog(path) as log:
        output = EventOutput(log, CheckingScreen())
        if kind == "reply_end":
            output.reply_end()
        else:
            getattr(output, kind)("A line.")


async def test_terminal_show_log_failure_is_recorded_once_with_the_visible_notice(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.runtime_dir.mkdir(parents=True)
    (config.runtime_dir / "show.log").mkdir()
    backend = FakeBackend(FakeClock(), [[Tool("show", {"markdown": "# Plan"})]])

    stdout, stderr = await converse(backend, io.StringIO("Show me\n"), config=config)

    with EventLog(config.runtime_dir / "events.sqlite3") as log:
        notices = [event.payload["text"] for event in log.after(0) if event.type == "notice"]
    assert "Plan" in stdout
    assert len(notices) == 3
    assert notices[:2] == ["Model loading.", "Model ready."]
    assert notices[2].startswith("Could not write the show log:")
    assert stderr == "".join(f"[hub] {notice}\n" for notice in notices)


async def test_terminal_screen_failure_is_recorded_before_the_visible_notice(tmp_path: Path) -> None:
    from jarvis_hub.main import text_mode

    config = settings(tmp_path)
    backend = FakeBackend(FakeClock(), [[Tool("show", {"markdown": "# Plan"})]])
    path = config.runtime_dir / "events.sqlite3"

    class FailedDetailsScreen(io.StringIO):
        def write(self, text: str) -> int:
            if "Plan" in text:
                raise OSError("Screen unavailable")
            return super().write(text)

    class CheckingNotices(io.StringIO):
        def write(self, text: str) -> int:
            with EventLog(path) as reader:
                assert text == f"[hub] {reader.after(0)[-1].payload['text']}\n"
            return super().write(text)

    stderr = CheckingNotices()
    await text_mode(
        settings=config, environ={}, backend=backend, clock=backend.clock,
        stdin=io.StringIO("Show me\n"), stdout=FailedDetailsScreen(), stderr=stderr,
    )

    with EventLog(path) as log:
        notices = [event.payload["text"] for event in log.after(0) if event.type == "notice"]
    assert notices == ["Model loading.", "Model ready.", "Could not show the details on screen: Screen unavailable"]
    assert stderr.getvalue() == "".join(f"[hub] {notice}\n" for notice in notices)
    assert "# Plan" in (config.runtime_dir / "show.log").read_text()


async def test_worker_notices_are_durable_and_displayed_in_log_order(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    start = threading.Barrier(8)

    class CheckingScreen(RecordingOutput):
        def notice(self, text: str) -> None:
            with EventLog(path) as reader:
                assert reader.after(0)[-1].payload == {"text": text}
            super().notice(text)

    screen = CheckingScreen()
    with EventLog(path) as log:
        output = EventOutput(log, screen)

        def device_notice(index: int) -> None:
            start.wait(timeout=5)
            output.notice(f"Audio moved: device {index}")

        await asyncio.gather(*(asyncio.to_thread(device_notice, index) for index in range(8)))
        assert [event.payload["text"] for event in log.after(0)] == screen.notices
        assert len(screen.notices) == 8
