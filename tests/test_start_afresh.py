"""Start afresh and showing Memory through the Hub: the User folds everything before one sentence into Memory, and
sees and corrects what Jarvis remembers."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import (
    AwaitInterrupt,
    Fail,
    FakeBackend,
    FakeClock,
    Gate,
    Limited,
    StubGh,
    Text,
    Tool,
    Typist,
    a_summary,
    until,
)
from tests.test_approvals import HEAD, ask_merge
from tests.test_fold import SHORT, a_hub, exchanges, leave_a_scope_unresolved, said, tokens
from tests.test_fold_failure import say
from tests.test_voice_mode import LocalRelation, MakeRig, rig_for as rig_for

AFRESH = [Text("Very well, sir."), Tool("start_afresh")]
SHOW_MEMORY = [Text("Here it is, sir."), Tool("show", {"memory": True})]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / ".jarvis" / "conversation.json"


def tool_result(backend: FakeBackend, name: str) -> list[str | None]:
    return [r.result for s in backend.all_sessions("talker") for r in s.tool_results if r.name == name]


async def test_start_afresh_folds_only_the_messages_before_it_while_jarvis_keeps_talking(
    clock: FakeClock, path: Path
) -> None:
    release = asyncio.Event()
    hub, backend, output, _ = a_hub(
        clock, path, [[Text("Noted.")], AFRESH, [Text("Rome it is.")], [Text("After the swap.")]],
        summaries=[[Gate(release), Text(a_summary("The User's sister is Giulia."))]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await say(typist, backend, "Let's start afresh.")
    await until(lambda: len(backend.all_sessions("summariser")) == 1)
    await say(typist, backend, "Let's plan a trip to Rome.")
    assert output.replies == ["Noted.", "Very well, sir.", "Rome it is."]
    old = backend.all_sessions("talker")[0]
    release.set()
    await until(lambda: len(backend.all_sessions("talker")) == 2 and old.closed)
    typist.type("Next.")
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    [summarised] = backend.all_sessions("summariser")[0].received
    assert "My sister is Giulia." in summarised and "start afresh" not in summarised and "Rome" not in summarised
    seed = backend.all_sessions("talker")[1].received[0]
    assert "The User's sister is Giulia." in seed
    assert exchanges(seed) == ["Let's start afresh.", "Let's plan a trip to Rome."]
    saved = json.loads(path.read_text())
    assert saved["active_summary"] == 1
    assert [said(e["message"]) for e in saved["entries"]] == ["Let's start afresh.", "Let's plan a trip to Rome.",
                                                              "Next."]
    assert output.notices == [] and output.shown == []


async def test_start_afresh_cancels_a_running_automatic_fold_and_its_late_result_is_never_saved(
    clock: FakeClock, path: Path
) -> None:
    late = asyncio.Event()
    hub, backend, _, metrics = a_hub(
        clock, path, [[Text("Noted."), tokens(200_000)], [Text("Noted.")], AFRESH], memory=SHORT,
        summaries=[[Gate(late), Text(a_summary("Automatic."))], [Text(a_summary("Afresh."))]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await until(lambda: len(backend.all_sessions("summariser")) == 1)
    await say(typist, backend, "My dog is Fido.")
    await say(typist, backend, "Let's start afresh.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    late.set()
    await asyncio.sleep(0.01)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    automatic, afresh = backend.all_sessions("summariser")
    assert automatic.closed
    assert "My dog is Fido." in afresh.received[0] and "start afresh" not in afresh.received[0]
    assert [r["outcome"] for r in metrics.records if r.get("role") == "summariser"] == ["cancelled", "completed"]
    kept = [json.loads(p.read_text())["summary"] for p in (path.parent / "memory").glob("summary-*.json")]
    assert kept == [a_summary("Afresh.").strip()]
    seed = backend.all_sessions("talker")[1].received[0]
    assert "Afresh." in seed and "Automatic." not in seed


async def test_start_afresh_leaves_tasks_authorizations_and_approvals_as_they_were(clock: FakeClock, path: Path) -> None:
    grant = {"scope": "task", "description": "Merge PR 42 once CI is green"}
    status = [Text("Let me look."), Tool("status")]
    hub, backend, output, _ = a_hub(
        clock, path,
        [[Text("I will see to it, sir."), Tool("delegate", {"request": "Merge pumpkit#42", "authorization": grant})],
         [Text("Shall I merge it?")], status, AFRESH, status],
        orchestrator=[[ask_merge()]], runner=StubGh(prs={"pumpkit-io/pumpkit#42": ("OPEN", HEAD)}),
        state_path=path.with_name("state.json"),
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("Merge pumpkit#42 once CI is green")
    await until(lambda: len(output.replies) == 2)
    await say(typist, backend, "What is running?")
    await say(typist, backend, "Let's start afresh.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    await say(typist, backend, "What is running now?")
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    before, after = tool_result(backend, "status")
    assert before == after
    assert before is not None and "A1" in before and "U1" in before and "T1" in before
    records = backend.all_sessions("talker")[1].received[0].split("</conversation>")[1]
    assert "T1 needs user" in records and '<authorization id="U1"' in records and "A1 pending" in records


async def test_start_afresh_with_nothing_before_it_folds_nothing(clock: FakeClock, path: Path) -> None:
    hub, backend, _, _ = a_hub(clock, path, [AFRESH])
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "Let's start afresh.")
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert backend.all_sessions("summariser") == []
    assert tool_result(backend, "start_afresh") == ["There is nothing before this message to fold: the Conversation "
                                                    "already starts here."]


async def test_start_afresh_waits_for_the_summariser_usage_limit_to_reset(clock: FakeClock, path: Path) -> None:
    hub, backend, _, _ = a_hub(clock, path, [[Text("Noted."), tokens(200_000)], AFRESH], memory=SHORT,
                               summaries=[[Limited(3600)]])
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await until(lambda: all(s.closed for s in backend.all_sessions("summariser")))
    await say(typist, backend, "Let's start afresh.")
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert len(backend.all_sessions("summariser")) == 1
    [refused] = tool_result(backend, "start_afresh")
    assert refused is not None and refused.startswith("Memory cannot be written until the usage limit resets")


async def test_start_afresh_waits_for_an_unresolved_spoken_scope(rig_for: MakeRig, tmp_path: Path) -> None:
    rig = await rig_for(
        [Tool("delegate", {"request": "Check build"}), AwaitInterrupt()], [Text("A separate answer, sir.")], AFRESH,
        orchestrator=[[AwaitInterrupt()]], recognition=LocalRelation("new_request"),
        hub_options={"conversation_path": tmp_path / "conversation.json"},
    )

    await leave_a_scope_unresolved(rig)
    rig.typist.type("Let's start afresh.")
    await until(lambda: bool(tool_result(rig.backend, "start_afresh")))

    assert tool_result(rig.backend, "start_afresh") == ["Resolve the pending spoken scope before starting afresh."]
    assert rig.backend.all_sessions("summariser") == []


async def test_a_start_afresh_that_fails_tells_the_user_the_conversation_goes_on(clock: FakeClock, path: Path) -> None:
    hub, backend, output, _ = a_hub(clock, path, [[Text("Noted.")], AFRESH], summaries=[[Fail("overloaded")]] * 2)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await say(typist, backend, "Let's start afresh.")
    await until(lambda: len(output.notices) == 1)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert output.notices == ["Could not start afresh: Memory could not be written, so the conversation goes on as "
                              "it was."]
    assert len(backend.all_sessions("talker")) == 1


async def test_before_the_first_fold_jarvis_says_nothing_has_been_folded_and_shows_nothing(
    clock: FakeClock, path: Path
) -> None:
    hub, backend, output, _ = a_hub(clock, path, [SHOW_MEMORY])
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "What do you remember about me?")
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert output.shown == []
    assert tool_result(backend, "show") == ["Nothing has been folded yet, so there is no Memory to show: say so."]


async def test_what_jarvis_remembers_is_the_summary_on_screen(clock: FakeClock, path: Path) -> None:
    summary = a_summary("The User's sister is Giulia.")
    hub, backend, output, _ = a_hub(clock, path, [[Text("Noted."), tokens(200_000)], SHOW_MEMORY], memory=SHORT,
                                    summaries=[[Text(summary)]])
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    await say(typist, backend, "What do you remember about me?")
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert output.shown == [summary.strip()]
    assert tool_result(backend, "show") == ["Your Memory is on screen."]


async def test_a_spoken_correction_overrides_the_earlier_statement_in_the_next_summary(
    clock: FakeClock, path: Path
) -> None:
    hub, backend, _, _ = a_hub(
        clock, path, [[Text("Noted."), tokens(200_000)], [Text("Noted.")], AFRESH], memory=SHORT,
        summaries=[[Text(a_summary("The User lives in Milan."))], [Text(a_summary("The User lives in Rome."))]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "I live in Milan.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    await say(typist, backend, "Actually, I live in Rome now.")
    await say(typist, backend, "Let's start afresh.")
    await until(lambda: len(backend.all_sessions("talker")) == 3)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    second = backend.all_sessions("summariser")[1].received[0]
    assert second.index("The User lives in Milan.") < second.index("Actually, I live in Rome now.")
    assert "The User lives in Rome." in backend.all_sessions("talker")[2].received[0]
