import io
import json
from pathlib import Path

from jarvis_hub.agents.backend import SessionSpec
from jarvis_hub.main import JsonlMetrics, guard_api_key, text_mode
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, Text, Wait

SPEC = SessionSpec(
    role="talker", model="sonnet", effort="low", thinking=False, system_prompt="You are Jarvis.", cwd=Path("/tmp")
)


async def test_typed_lines_get_streamed_replies_on_the_terminal() -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Wait(0.2), Text("Good evening, "), Text("sir.")], [Text("Indeed, sir.")]])
    stdout, stderr = io.StringIO(), io.StringIO()
    metrics = RecordingMetrics()

    await text_mode(
        talker=SPEC,
        backend=backend,
        stdin=io.StringIO("Good evening\n\nAll well?\n"),
        stdout=stdout,
        stderr=stderr,
        metrics=metrics,
        clock=clock,
    )

    assert stdout.getvalue() == "› Good evening\nGood evening, sir.\n› All well?\nIndeed, sir.\n"
    assert stderr.getvalue() == ""
    assert backend.sessions[0].received[1:] == [
        '<user typed="true">Good evening</user>',
        '<user typed="true">All well?</user>',
    ]
    assert backend.sessions[0].closed
    assert [r["kind"] for r in metrics.records] == ["warm_up", "user", "user"]


def test_api_key_is_warned_about_and_kept_from_the_child_processes() -> None:
    environ = {"ANTHROPIC_API_KEY": "sk-ant-x", "PATH": "/bin"}
    warning = guard_api_key(environ)
    assert warning is not None and "ANTHROPIC_API_KEY" in warning
    assert environ == {"PATH": "/bin"}


def test_no_warning_without_an_api_key() -> None:
    environ = {"PATH": "/bin"}
    assert guard_api_key(environ) is None
    assert environ == {"PATH": "/bin"}


def test_metrics_append_one_json_line_per_entry_with_a_timestamp(tmp_path: Path) -> None:
    metrics = JsonlMetrics(tmp_path / "runtime" / "metrics.jsonl")
    metrics.record({"kind": "user", "first_sentence_ms": 640})
    metrics.record({"kind": "user", "first_sentence_ms": 910})
    rows = [json.loads(line) for line in (tmp_path / "runtime" / "metrics.jsonl").read_text().splitlines()]
    assert [r["first_sentence_ms"] for r in rows] == [640, 910]
    assert all("at" in r for r in rows)
