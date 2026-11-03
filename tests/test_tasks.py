"""Tasks end to end through the Hub: the Talker delegates, the Orchestrator reports, the Hub keeps the record."""

import asyncio
import json

import pytest

from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    Gate,
    Raise,
    RecordingMetrics,
    RecordingOutput,
    Set,
    Text,
    Tool,
    ToolResult,
    Typist,
    Wait,
    lines,
    make_hub,
    until,
)

ASK_PRS = "What PRs are open in pumpkit?"


def delegate(request: str = ASK_PRS, task_id: str | None = None) -> Tool:
    args = {"request": request, "context": "Asked in text mode."}
    return Tool("delegate", args if task_id is None else {**args, "task_id": task_id})


def report(task_id: str, spoken: str, details: str | None = None, needs_user: bool = False) -> Tool:
    args: dict[str, object] = {"task_id": task_id, "spoken": spoken, "needs_user": needs_user}
    if details is not None:
        args["details_markdown"] = details
    return Tool("report", args)


def results(backend: FakeBackend, role: str, name: str) -> list[ToolResult]:
    return [r for r in backend.session(role).tool_results if r.name == name]


def talker_messages(backend: FakeBackend) -> list[str]:
    return backend.session("talker").received[1:]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


async def test_delegate_returns_a_task_id_at_once_while_the_orchestrator_works(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("Checking now, sir."), delegate()],
            [Set(release), Text("You're welcome, sir.")],
            [Text("Two are open, sir.")],
        ],
        orchestrator=[[Gate(release), report("T1", "Two PRs are open in pumpkit.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "Thanks"))

    [result] = results(backend, "talker", "delegate")
    assert json.loads(result.result or "") == {"task_id": "T1", "queued": True}
    assert output.replies == ["Checking now, sir.", "You're welcome, sir.", "Two are open, sir."]
    [task] = backend.session("orchestrator").received
    assert task.startswith('<task id="T1">') and ASK_PRS in task and "Asked in text mode." in task


async def test_a_report_reaches_the_talker_as_an_event_with_its_details_on_screen(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    details = "| PR | Title |\n|---|---|\n| #41 | Fix the scheduler |"
    backend = FakeBackend(
        clock,
        [[Text("Right away, sir."), delegate()], [Text("Two are open, sir; the list is on screen.")]],
        orchestrator=[[report("T1", "Two PRs are open in pumpkit.", details)]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS))

    assert talker_messages(backend)[-1] == (
        '<event source="orchestrator" task="T1" needs_user="false" details="on screen">'
        "Two PRs are open in pumpkit.</event>"
    )
    assert [kind for kind, _ in output.log if kind != "reply"] == ["heard", "end", "show", "end"]
    assert output.shown == [details]
    assert results(backend, "orchestrator", "report")[0].result == "Reported to the User."
    assert [r["kind"] for r in metrics.records if r["role"] == "talker"] == ["warm_up", "user", "event"]


async def test_status_is_answered_from_the_hub_without_an_orchestrator_turn(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("On it, sir."), delegate()],
            [Wait(30), Text("Let me see."), Tool("status"), Set(release), Text("One lookup running, sir.")],
        ],
        orchestrator=[[Gate(release), report("T1", "Two PRs are open.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "What are you working on?"))

    [status] = results(backend, "talker", "status")
    assert status.result == f"T1 working, started 30 s ago: {ASK_PRS}"
    assert len(backend.session("orchestrator").received) == 1


async def test_status_lists_every_task_with_its_last_report(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("Noted.")], [Text("Let me see."), Tool("status")]],
        orchestrator=[[report("T1", "Two PRs are open.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "What's the state?"))

    assert results(backend, "talker", "status")[0].result == (
        f"T1 done, started 0 s ago: {ASK_PRS} | Last report: Two PRs are open."
    )


async def test_status_with_no_tasks(clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics) -> None:
    backend = FakeBackend(clock, [[Text("Let me see."), Tool("status"), Text("Nothing, sir.")]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "What are you working on?"))

    assert results(backend, "talker", "status")[0].result == "No Tasks this session."
    assert [s.spec.role for s in backend.sessions] == ["talker"]


async def test_cancel_stops_the_orchestrator_on_that_task_and_nothing_more_is_heard_of_it(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("On it, sir."), delegate()],
            [Text("Cancelling, sir."), Tool("cancel", {"task_id": "T1"})],
            [Text("Let me see."), Tool("status")],
        ],
        orchestrator=[[Text("Looking."), AwaitInterrupt(), report("T1", "Too late.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    await until(lambda: any(s.spec.role == "orchestrator" and s.received for s in backend.sessions))
    typist.type("Cancel that")
    typist.type("And now?")
    typist.end()
    await asyncio.wait_for(running, 1)

    assert results(backend, "talker", "cancel")[0].result == "Cancelled T1."
    assert results(backend, "orchestrator", "report")[0].result == "T1 was cancelled; the User will not hear this."
    assert not any(m.startswith("<event") for m in talker_messages(backend))
    assert results(backend, "talker", "status")[0].result.startswith("T1 cancelled")  # type: ignore[union-attr]
    orchestrator_turn = next(r for r in metrics.records if r["role"] == "orchestrator")
    assert (orchestrator_turn["task"], orchestrator_turn["outcome"]) == ("T1", "interrupted")


async def test_a_queued_task_cancelled_before_its_turn_never_reaches_the_orchestrator(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate()],
            [Text("Also on it."), delegate("Is CI green on jarvis?")],
            [Text("Dropping it."), Tool("cancel", {"task_id": "T2"}), Set(release)],
            [Text("Done, sir.")],
        ],
        orchestrator=[[Gate(release), report("T1", "Two PRs are open.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "Is CI green on jarvis?", "Forget the CI one"))

    assert [m.splitlines()[0] for m in backend.session("orchestrator").received] == ['<task id="T1">']


async def test_cancel_of_an_unknown_or_finished_task_says_so(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate()],
            [Text("Noted.")],
            [Text("Right."), Tool("cancel", {"task_id": "T1"}), Tool("cancel", {"task_id": "T9"})],
        ],
        orchestrator=[[report("T1", "Two PRs are open.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "Cancel that"))

    assert [r.result for r in results(backend, "talker", "cancel")] == ["T1 is already done.", "There is no Task T9."]


async def test_a_failed_task_with_a_follow_up_waiting_can_be_cancelled_and_the_follow_up_never_runs(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    fail_now, release = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate()],
            [Text("That too."), delegate("Is CI green on jarvis?")],
            [Text("Adding that."), delegate("And the drafts?", task_id="T1"), Set(fail_now)],
            [Text("The PR look-up failed, sir.")],
            [Text("Dropping it."), Tool("cancel", {"task_id": "T1"}), Set(release)],
            [Text("CI is green, sir.")],
        ],
        orchestrator=[
            [Gate(fail_now), Raise(ConnectionError("pipe closed"))],
            [Gate(release), report("T2", "CI is green.")],
            [report("T1", "One draft.")],
        ],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    typist.type("Is CI green on jarvis?")
    typist.type("And the drafts?")
    await until(lambda: len(output.replies) == 4)
    typist.type("Forget the PR one")
    typist.end()
    await asyncio.wait_for(running, 1)

    assert results(backend, "talker", "cancel")[0].result == "Cancelled T1."
    assert [m.splitlines()[0] for m in backend.session("orchestrator").received] == [
        '<task id="T1">',
        '<task id="T2">',
    ]


async def test_an_answer_to_an_orchestrator_question_continues_the_same_task(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("On it, sir."), delegate()],
            [Text("Should I count drafts, sir?")],
            [Text("Very good."), delegate("Yes, count drafts too.", task_id="T1")],
            [Text("Three, counting drafts.")],
        ],
        orchestrator=[
            [report("T1", "Two are open, plus one draft. Count the draft?", needs_user=True)],
            [report("T1", "Three PRs are open, one of them a draft.")],
        ],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    await until(lambda: len(output.replies) == 2)
    typist.type("Yes, include drafts")
    typist.end()
    await asyncio.wait_for(running, 1)

    assert '<event source="orchestrator" task="T1" needs_user="true">' in talker_messages(backend)[1]
    delegations = results(backend, "talker", "delegate")
    assert [json.loads(r.result or "")["task_id"] for r in delegations] == ["T1", "T1"]
    second = backend.session("orchestrator").received[1]
    assert second.startswith('<task id="T1" continues="true">') and "Yes, count drafts too." in second


async def test_a_task_id_the_hub_does_not_know_is_refused(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("Right."), delegate("Go on.", task_id="T4")]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "Go on with it"))

    assert results(backend, "talker", "delegate")[0].result == (
        "There is no Task T4. Delegate without task_id to start a new one."
    )
    assert [s.spec.role for s in backend.sessions] == ["talker"]


async def test_a_cancelled_task_cannot_be_continued(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate(), Tool("cancel", {"task_id": "T1"})],
            [Text("Right."), delegate("Go on after all.", task_id="T1")],
        ],
        orchestrator=[[AwaitInterrupt()]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "Go on with it after all"))

    assert results(backend, "talker", "delegate")[1].result == (
        "T1 was cancelled. Delegate without task_id to start a new one."
    )


async def test_an_orchestrator_turn_that_ends_without_a_report_fails_the_task_and_says_so(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("That lookup failed, sir.")]],
        orchestrator=[[Text("I looked around.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS))

    assert talker_messages(backend)[-1] == (
        '<event source="hub" task="T1" needs_user="false">'
        "T1 failed: the Orchestrator ended its turn without a report.</event>"
    )


async def test_an_orchestrator_that_cannot_start_fails_the_task(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("I couldn't start that, sir.")]],
        orchestrator_open_error=RuntimeError("claude CLI not found"),
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS))

    assert talker_messages(backend)[-1] == (
        '<event source="hub" task="T1" needs_user="false">'
        "T1 failed: the Orchestrator could not start: claude CLI not found.</event>"
    )


async def test_the_orchestrator_takes_one_message_at_a_time(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("That too."), delegate("Is CI green on jarvis?")]],
        orchestrator=[[Gate(release), report("T1", "Two PRs are open.")], [report("T2", "CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    typist.type("Is CI green on jarvis?")
    await until(lambda: len(output.replies) == 2)
    await asyncio.sleep(0.01)
    assert len(backend.session("orchestrator").received) == 1
    release.set()
    typist.end()
    await asyncio.wait_for(running, 1)

    assert [m.splitlines()[0] for m in backend.session("orchestrator").received] == ['<task id="T1">', '<task id="T2">']
    events = [m for m in talker_messages(backend) if m.startswith("<event")]
    assert [e[:50] for e in events] == [
        '<event source="orchestrator" task="T1" needs_user=',
        '<event source="orchestrator" task="T2" needs_user=',
    ]


async def test_show_renders_on_screen(clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics) -> None:
    backend = FakeBackend(clock, [[Text("On screen, sir."), Tool("show", {"markdown": "# Plan\n\n1. Fix it"})]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "Put the plan on screen"))

    assert output.shown == ["# Plan\n\n1. Fix it"]
    assert results(backend, "talker", "show")[0].result == "Shown on screen."


async def test_end_conversation_is_answered_by_the_hub(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("Very good, sir."), Tool("end_conversation")]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "That's all, thanks"))

    assert results(backend, "talker", "end_conversation")[0].result == "The conversation ends after this reply."
    assert [s.spec.role for s in backend.sessions] == ["talker"]


async def test_each_role_gets_only_its_own_hub_tools(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("On it."), delegate()]], orchestrator=[[report("T1", "Done.")]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS))

    assert [t.name for t in backend.session("talker").spec.tools] == [
        "delegate",
        "status",
        "cancel",
        "show",
        "end_conversation",
    ]
    assert [t.name for t in backend.session("orchestrator").spec.tools] == [
        "report",
        "spawn_subagent",
        "list_subagents",
        "cancel_subagent",
        "watch",
        "schedule_check",
        "cancel_wait",
    ]


async def test_each_turn_logs_its_tool_calls_and_whether_text_came_first(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it, sir."), delegate()], [Tool("status"), Text("Nothing else, sir.")], [Text("Indeed.")]],
        orchestrator=[[Tool("Bash", {"command": "gh pr list"}), report("T1", "Two PRs are open.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "Anything else?"))

    talker = [(r["kind"], r["tools"], r["tool_before_text"]) for r in metrics.records if r["role"] == "talker"]
    assert talker[:3] == [("warm_up", [], False), ("user", ["delegate"], False), ("user", ["status"], True)]
    orchestrator = [r for r in metrics.records if r["role"] == "orchestrator"]
    assert [(r["kind"], r["task"], r["tools"], r["outcome"]) for r in orchestrator] == [
        ("task", "T1", ["Bash", "report"], "completed")
    ]


async def test_an_orchestrator_session_that_raises_fails_the_task_and_the_next_one_still_runs(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("That failed, sir.")], [Text("On it."), delegate("Is CI green?")]],
        orchestrator=[[Raise(ConnectionError("pipe closed"))], [report("T2", "CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    await until(lambda: len(output.replies) == 2)
    typist.type("Is CI green?")
    typist.end()
    await asyncio.wait_for(running, 1)

    events = [m for m in talker_messages(backend) if m.startswith("<event")]
    assert events == [
        '<event source="hub" task="T1" needs_user="false">T1 failed: ConnectionError: pipe closed.</event>',
        '<event source="orchestrator" task="T2" needs_user="false">CI is green.</event>',
    ]


async def test_a_cancel_that_arrives_after_its_turn_ended_leaves_the_next_task_alone(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    finish, arrive, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate()],
            [Text("That too."), delegate("Is CI green on jarvis?")],
            [Text("Cancelling."), Tool("cancel", {"task_id": "T1"})],
            [Text("Noted.")],
        ],
        orchestrator=[
            [Gate(finish), report("T1", "Too late.")],
            [Text("Checking."), Gate(checked), report("T2", "CI is green.")],
        ],
        interrupt_gate=arrive,
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    typist.type("Is CI green on jarvis?")
    await until(lambda: len(output.replies) == 2)
    typist.type("Cancel the first one")
    await until(lambda: ("reply", "Cancelling.") in output.log)
    await asyncio.sleep(0.01)
    finish.set()
    await asyncio.sleep(0.01)
    arrive.set()
    await asyncio.sleep(0.01)
    checked.set()
    typist.end()
    await asyncio.wait_for(running, 1)

    t2 = next(r for r in metrics.records if r.get("task") == "T2")
    assert t2["outcome"] == "completed"
    assert '<event source="orchestrator" task="T2" needs_user="false">CI is green.</event>' in talker_messages(backend)


async def test_a_report_on_a_task_other_than_the_one_in_hand_is_refused(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(), delegate("Is CI green on jarvis?")]],
        orchestrator=[
            [report("T2", "CI is green."), report("T1", "Two PRs are open.")],
            [report("T2", "CI is green.")],
        ],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS))

    [wrong, first, second] = [r.result for r in results(backend, "orchestrator", "report")]
    assert wrong == "You are working on T1: report on T1 only."
    assert (first, second) == ("Reported to the User.", "Reported to the User.")
    events = [m for m in talker_messages(backend) if m.startswith("<event")]
    assert [e.split('task="')[1][:2] for e in events] == ["T1", "T2"]


async def test_following_up_on_a_running_task_keeps_it_working(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate()],
            [Text("Noted."), delegate("Count drafts too.", task_id="T1"), Tool("status"), Set(release)],
        ],
        orchestrator=[[Gate(release), report("T1", "Two are open.")], [report("T1", "Three, with the draft.")]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, ASK_PRS, "Count drafts too"))

    assert results(backend, "talker", "status")[0].result == f"T1 working, started 0 s ago: {ASK_PRS}"
    assert len(backend.session("orchestrator").received) == 2


async def test_an_orchestrator_turn_past_its_time_is_interrupted_and_fails_the_task(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("That one stalled, sir.")], [Text("On it."), delegate("Is CI green?")]],
        orchestrator=[[Text("Looking."), AwaitInterrupt()], [report("T2", "CI is green.")]],
    )
    hub = make_hub(backend, output, metrics, orchestrator_timeout=0.05)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    await until(lambda: len(output.replies) == 2)
    typist.type("Is CI green?")
    typist.end()
    await asyncio.wait_for(running, 1)

    events = [m for m in talker_messages(backend) if m.startswith("<event")]
    assert events == [
        '<event source="hub" task="T1" needs_user="false">T1 failed: the Orchestrator took longer than 0.05 s.</event>',
        '<event source="orchestrator" task="T2" needs_user="false">CI is green.</event>',
    ]
    assert len(backend.all_sessions("orchestrator")) == 1


async def test_a_turn_that_ignores_the_interrupt_gets_its_session_replaced(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    never = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("That one stalled, sir.")], [Text("On it."), delegate("Is CI green?")]],
        orchestrator=[[Gate(never)], [report("T2", "CI is green.")]],
    )
    hub = make_hub(backend, output, metrics, orchestrator_timeout=0.05)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    await until(lambda: len(output.replies) == 2)
    typist.type("Is CI green?")
    typist.end()
    await asyncio.wait_for(running, 1)

    first, second = backend.all_sessions("orchestrator")
    assert first.closed and not second.closed
    assert second.received[0].startswith('<task id="T2">')
    assert talker_messages(backend)[1].startswith('<event source="hub" task="T1" needs_user="false">T1 failed')


async def test_an_error_in_the_hub_on_one_task_fails_it_and_the_next_task_still_runs(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    never = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate()], [Text("That one broke, sir.")], [Text("On it."), delegate("Is CI green?")]],
        orchestrator=[[Gate(never)], [report("T2", "CI is green.")]],
        interrupt_error=ConnectionError("pipe closed"),
    )
    hub = make_hub(backend, output, metrics, orchestrator_timeout=0.05)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    await until(lambda: len(output.replies) == 2)
    typist.type("Is CI green?")
    typist.end()
    await asyncio.wait_for(running, 1)

    events = [m for m in talker_messages(backend) if m.startswith("<event")]
    assert events == [
        '<event source="hub" task="T1" needs_user="false">T1 failed: ConnectionError: pipe closed.</event>',
        '<event source="orchestrator" task="T2" needs_user="false">CI is green.</event>',
    ]
    assert any("ConnectionError" in n for n in output.notices)


async def test_in_text_mode_news_reaches_the_talker_in_the_order_it_arrives(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    reported, second_done = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("Checking now, sir."), delegate()],
            [Gate(reported), Text("Working on it, sir."), Set(second_done)],
            [Text("Two are open, sir.")],
            [Text("Good night, sir.")],
        ],
        orchestrator=[[report("T1", "Two PRs are open in pumpkit."), Set(reported)]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(ASK_PRS)
    typist.type("Anything else?")
    await until(lambda: reported.is_set())
    typist.type("Good night")
    await until(lambda: second_done.is_set())
    typist.end()
    await running

    assert [m.split(">")[0] for m in talker_messages(backend)] == [
        '<user lang="en" typed="true"',
        '<user lang="en" typed="true"',
        '<event source="orchestrator" task="T1" needs_user="false"',
        '<user lang="en" typed="true"',
    ]
