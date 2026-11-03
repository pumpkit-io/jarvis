import asyncio
import io
import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import TextIO

import pytest

from jarvis_hub.config import RoleModel, Settings
from jarvis_hub.main import text_mode
from tests.fakes import FakeBackend, FakeClock, Gate, Text, Wait


def settings(tmp_path: Path, user_name: str | None = "Ada", talker_model: str = "sonnet") -> Settings:
    return Settings(
        user_name=user_name,
        org="acme",
        org_dir=tmp_path,
        repo_dir=tmp_path / "jarvis",
        runtime_dir=tmp_path / "jarvis" / ".jarvis",
        talker=RoleModel(talker_model, "low", False),
        orchestrator=RoleModel("sonnet", "medium"),
    )


async def converse(
    backend: FakeBackend,
    stdin: TextIO,
    *,
    config: Settings,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    await asyncio.wait_for(
        text_mode(
            settings=config,
            environ={} if environ is None else environ,
            backend=backend,
            clock=backend.clock,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        ),
        timeout=5,
    )
    return stdout.getvalue(), stderr.getvalue()


async def test_typed_lines_get_streamed_replies_on_the_terminal(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Wait(0.2), Text("Good evening, "), Text("sir.")], [Text("Indeed, sir.")]])

    stdout, stderr = await converse(backend, io.StringIO("Good evening\n\nAll well?\n"), config=settings(tmp_path))

    assert stdout == "› Good evening\nGood evening, sir.\n› All well?\nIndeed, sir.\n"
    assert stderr == ""
    assert backend.sessions[0].received[1:] == [
        '<user typed="true">Good evening</user>',
        '<user typed="true">All well?</user>',
    ]
    assert backend.sessions[0].closed


async def test_one_talker_session_with_one_prompt_carries_the_whole_launch(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock())

    await converse(backend, io.StringIO("Hello\nAnd again\n"), config=settings(tmp_path, talker_model="haiku"))

    [session] = backend.sessions
    assert len(session.received) == 3
    spec = session.spec
    assert (spec.role, spec.model, spec.effort, spec.thinking, list(spec.tools)) == (
        "talker",
        "haiku",
        "low",
        False,
        [],
    )
    assert "J.A.R.V.I.S." in spec.system_prompt
    assert '"sir"' in spec.system_prompt and '"signore"' in spec.system_prompt
    assert "Ada" in spec.system_prompt and "{" not in spec.system_prompt
    # About four characters per token: well under the 1,000-token budget.
    assert len(spec.system_prompt) < 3600


async def test_the_prompt_says_when_the_user_name_is_not_known(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock())

    await converse(backend, io.StringIO("Hello\n"), config=settings(tmp_path, user_name=None))

    assert "not known" in backend.sessions[0].spec.system_prompt


@pytest.mark.parametrize(
    "variable",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"],
)
async def test_billing_outside_the_subscription_is_warned_about_and_switched_off(
    tmp_path: Path, variable: str
) -> None:
    environ = {variable: "1", "PATH": "/bin"}

    stdout, stderr = await converse(FakeBackend(FakeClock()), io.StringIO("Hello\n"), config=settings(tmp_path), environ=environ)

    assert f"WARNING: {variable} is set" in stderr
    assert environ == {"PATH": "/bin"}
    assert stdout.endswith("Very good, sir.\n")


async def test_each_billing_switch_gets_its_own_warning(tmp_path: Path) -> None:
    environ = {"ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_USE_BEDROCK": "1"}

    _, stderr = await converse(FakeBackend(FakeClock()), io.StringIO(""), config=settings(tmp_path), environ=environ)

    assert stderr.count("WARNING:") == 2


async def test_no_warning_on_the_subscription(tmp_path: Path) -> None:
    _, stderr = await converse(
        FakeBackend(FakeClock()), io.StringIO("Hello\n"), config=settings(tmp_path), environ={"PATH": "/bin"}
    )
    assert stderr == ""


async def test_every_turn_appends_a_metrics_row(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Wait(0.64), Text("Evening, sir.")]])
    config = settings(tmp_path)

    await converse(backend, io.StringIO("Hello\n"), config=config)

    rows = [json.loads(line) for line in (config.runtime_dir / "metrics.jsonl").read_text().splitlines()]
    assert [(r["kind"], r["first_sentence_ms"]) for r in rows] == [("warm_up", 0), ("user", 640)]
    assert all(r["at"] for r in rows)


async def test_bytes_that_are_not_utf8_do_not_stop_the_conversation(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock())
    stdin = io.TextIOWrapper(io.BytesIO(b"Hello\nCaf\xe9?\nStill there?\n"), encoding="utf-8")

    await converse(backend, stdin, config=settings(tmp_path))

    assert backend.sessions[0].received[1:] == [
        '<user typed="true">Hello</user>',
        '<user typed="true">Caf�?</user>',
        '<user typed="true">Still there?</user>',
    ]


class BrokenStdin(io.StringIO):
    def __next__(self) -> str:
        if self.tell() == 0:
            return super().__next__()
        raise OSError("input/output error")


async def test_input_that_fails_ends_the_conversation(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock())

    await converse(backend, BrokenStdin("Hello\nNever read\n"), config=settings(tmp_path))

    assert backend.sessions[0].received[1:] == ['<user typed="true">Hello</user>']
    assert backend.sessions[0].closed


async def test_input_ending_during_the_warm_up_closes_the_session(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), warm_up=[Gate(asyncio.Event()), Text("Ready.")])

    stdout, _ = await converse(backend, io.StringIO(""), config=settings(tmp_path))

    assert stdout == ""
    assert [s.closed for s in backend.sessions] == [True]
