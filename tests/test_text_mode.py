import asyncio
import io
import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import TextIO

import pytest

from jarvis_hub.config import RoleModel, Settings
from jarvis_hub.main import Mouth, TerminalOutput, text_mode
from jarvis_hub.read_only import check
from tests.fakes import FakeBackend, FakeClock, FakeTts, Gate, RecordingPlayer, Text, Tool, Wait


def settings(tmp_path: Path, user_name: str | None = "Ada", talker_model: str = "sonnet") -> Settings:
    return Settings(
        user_name=user_name,
        org="acme",
        org_dir=tmp_path,
        repo_dir=tmp_path / "jarvis",
        runtime_dir=tmp_path / "jarvis" / ".jarvis",
        talker=RoleModel(talker_model, "low", False),
        orchestrator=RoleModel("sonnet", "medium", True),
    )


async def converse(
    backend: FakeBackend,
    stdin: TextIO,
    *,
    config: Settings,
    environ: MutableMapping[str, str] | None = None,
    voice: Mouth | None = None,
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
            voice=voice,
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
        '<user lang="en" typed="true">Good evening</user>',
        '<user lang="en" typed="true">All well?</user>',
    ]
    assert backend.sessions[0].closed


async def test_one_talker_session_with_one_prompt_carries_the_whole_launch(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock())

    await converse(backend, io.StringIO("Hello\nAnd again\n"), config=settings(tmp_path, talker_model="haiku"))

    [session] = backend.sessions
    assert len(session.received) == 3
    spec = session.spec
    assert (spec.role, spec.model, spec.effort, spec.thinking, spec.preset, spec.bash_guard) == (
        "talker",
        "haiku",
        "low",
        False,
        False,
        None,
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
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ],
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
        '<user lang="en" typed="true">Hello</user>',
        '<user lang="en" typed="true">Caf�?</user>',
        '<user lang="en" typed="true">Still there?</user>',
    ]


class BrokenStdin(io.StringIO):
    def __next__(self) -> str:
        if self.tell() == 0:
            return super().__next__()
        raise OSError("input/output error")


async def test_input_that_fails_ends_the_conversation(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock())

    await converse(backend, BrokenStdin("Hello\nNever read\n"), config=settings(tmp_path))

    assert backend.sessions[0].received[1:] == ['<user lang="en" typed="true">Hello</user>']
    assert backend.sessions[0].closed


async def test_input_ending_during_the_warm_up_closes_the_session(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), warm_up=[Gate(asyncio.Event()), Text("Ready.")])

    stdout, _ = await converse(backend, io.StringIO(""), config=settings(tmp_path))

    assert stdout == ""
    assert [s.closed for s in backend.sessions] == [True]


async def test_the_orchestrator_works_read_only_in_the_org_folder_with_the_boundary(tmp_path: Path) -> None:
    backend = FakeBackend(
        FakeClock(),
        [[Text("On it, sir."), Tool("delegate", {"request": "What PRs are open in pumpkit?"})]],
        orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "Two are open.", "needs_user": False})]],
    )
    config = settings(tmp_path)

    await converse(backend, io.StringIO("What PRs are open in pumpkit?\n"), config=config)

    spec = backend.session("orchestrator").spec
    assert (spec.model, spec.effort, spec.thinking, spec.preset, spec.cwd, spec.max_turns) == (
        "sonnet",
        "medium",
        True,
        True,
        tmp_path,
        12,
    )
    assert list(spec.read_tools) == ["Read", "Grep", "Glob"]
    assert spec.bash_guard is check
    prompts = Path(__file__).parents[1] / "prompts"
    boundary = (prompts / "boundary.md").read_text()
    for shared in ("boundary.md", "working-rules.md", "repo-catalog.md"):
        assert (prompts / shared).read_text() in spec.system_prompt
    assert "`gh pr` `checks`" in spec.system_prompt
    assert str(config.repo_dir / "docs" / "working-preferences.md") in spec.system_prompt
    assert "acme" in spec.system_prompt and "{" not in spec.system_prompt.replace(boundary, "")


async def test_details_are_rendered_on_screen_and_kept_in_the_show_log(tmp_path: Path) -> None:
    details = "## Open PRs\n\n- **#41** Fix the scheduler\n- **#42** Add threads"
    backend = FakeBackend(
        FakeClock(),
        [
            [Text("On it, sir."), Tool("delegate", {"request": "What PRs are open in pumpkit?"})],
            [Text("Two are open, sir. Here is my plan."), Tool("show", {"markdown": "# Plan\n\n1. Merge #41"})],
        ],
        orchestrator=[
            [Tool("report", {"task_id": "T1", "spoken": "Two are open.", "details_markdown": details, "needs_user": False})]
        ],
    )
    config = settings(tmp_path)

    stdout, stderr = await converse(backend, io.StringIO("What PRs are open in pumpkit?\n"), config=config)

    assert "Open PRs" in stdout and "#41 Fix the scheduler" in stdout and "**" not in stdout
    assert stdout.index("Open PRs") < stdout.index("Two are open, sir") < stdout.index("Plan")
    assert "Here is my plan.\n" in stdout
    log = (config.runtime_dir / "show.log").read_text()
    assert details in log and "# Plan\n\n1. Merge #41" in log
    assert log.index(details) < log.index("# Plan")
    assert stderr == ""


async def test_a_show_log_that_cannot_be_written_does_not_stop_the_screen(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Text("Here, sir."), Tool("show", {"markdown": "# Plan"})], [Text("Indeed.")]])
    config = settings(tmp_path)
    config.runtime_dir.mkdir(parents=True)
    (config.runtime_dir / "show.log").mkdir()

    stdout, stderr = await converse(backend, io.StringIO("Show me the plan\nThanks\n"), config=config)

    assert "Plan" in stdout and stdout.endswith("Indeed.\n")
    assert stderr.startswith("[hub] Could not write the show log:")


class ClosedPipe(io.StringIO):
    def write(self, s: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_details_for_a_screen_that_is_gone_still_reach_the_show_log(tmp_path: Path) -> None:
    stderr = io.StringIO()
    output = TerminalOutput(ClosedPipe(), stderr, prompt=False, show_log=tmp_path / "show.log")

    output.show("# Plan")

    assert "# Plan" in (tmp_path / "show.log").read_text()
    assert stderr.getvalue().startswith("[hub] Could not show the details on screen:")


def test_a_notice_for_a_terminal_that_is_gone_does_not_stop_the_hub(tmp_path: Path) -> None:
    output = TerminalOutput(ClosedPipe(), ClosedPipe(), prompt=False, show_log=tmp_path / "show.log")

    output.notice("The Talker failed.")
    output.show("# Plan")

    assert "# Plan" in (tmp_path / "show.log").read_text()


async def test_speak_says_the_startup_line_then_every_reply_and_ends_once_they_have_played(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Text("Good evening, sir. All quiet.")], [Text("Bene, signore.")]])
    player = RecordingPlayer()
    config = settings(tmp_path)

    stdout, stderr = await converse(
        backend, io.StringIO("Good evening\nTutto bene?\n"), config=config, voice=Mouth(FakeTts(), lambda rate: player)
    )

    assert stdout == "› Good evening\nGood evening, sir. All quiet.\n› Tutto bene?\nBene, signore.\n"
    assert stderr == ""
    assert list(zip(player.texts, player.voices)) == [
        ("At your service, sir.", "am_liam"),
        ("Good evening, sir.", "am_liam"),
        ("All quiet.", "am_liam"),
        ("Bene, signore.", "im_nicola"),
    ]
    assert player.closed
    rows = [json.loads(line) for line in (config.runtime_dir / "metrics.jsonl").read_text().splitlines()]
    assert [(r["kind"], r.get("lang")) for r in rows if r["kind"] == "speech"] == [("speech", "en"), ("speech", "it")]
    assert [r["load_ms"] for r in rows if r["kind"] == "voice_loaded"] == [0]



async def test_speak_with_no_audio_output_goes_on_in_text(tmp_path: Path) -> None:
    backend = FakeBackend(FakeClock(), [[Text("Good evening, sir.")]])

    def no_device(rate: int) -> RecordingPlayer:
        raise OSError("no output device")

    stdout, stderr = await converse(
        backend, io.StringIO("Good evening\n"), config=settings(tmp_path), voice=Mouth(FakeTts(), no_device)
    )

    assert stdout == "› Good evening\nGood evening, sir.\n"
    assert stderr == "[hub] Speech is off: no audio output: OSError: no output device\n"

