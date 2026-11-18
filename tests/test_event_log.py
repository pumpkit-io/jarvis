import io
from pathlib import Path

import pytest

from jarvis_hub.event_log import EventOutput
from tests.fakes import FakeBackend, FakeClock, RecordingOutput, Text, Tool
from tests.test_text_mode import converse, settings

from jarvis_hub.event_log import EventLog


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
        assert [(event.type, event.payload) for event in log.after(0)] == [
            ("notice", {"text": "Model loading."}),
            ("notice", {"text": "Model ready."}),
            ("heard", {"text": "Show me", "typed": True}),
            ("reply_delta", {"text": "Here, sir."}),
            ("show", {"markdown": "# Plan"}),
            ("reply_end", {}),
        ]
    assert stdout.startswith("› Show me\nHere, sir.\n")
    assert "Plan" in stdout and "# Plan" not in stdout
    assert stderr == "[hub] Model loading.\n[hub] Model ready.\n"
    restarted = FakeBackend(FakeClock(), [[Text("Morning, sir.")]])
    await converse(restarted, io.StringIO("Hello\n"), config=config)
    assert restarted.session("talker").received[1:] == ['<user lang="en" typed="true">Hello</user>']


@pytest.mark.parametrize("kind", ["heard", "reply", "reply_end", "notice", "show"])
def test_output_is_durable_before_it_reaches_the_screen(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "events.sqlite3"

    class CheckingScreen(RecordingOutput):
        def record(self, expected: str) -> None:
            with EventLog(path) as reader:
                [event] = reader.after(0)
                assert event.type == expected

        def heard(self, text: str, *, typed: bool = True) -> None:
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
