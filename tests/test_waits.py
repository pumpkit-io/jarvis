"""Watchers and Checks through the Hub: the Orchestrator sets them, the Hub waits without a model and posts the end."""

import asyncio
import json
import subprocess
from typing import Any

import pytest

from jarvis_hub.agents.backend import Refused
from jarvis_hub.waits import run_confined, watch_command
from tests.fakes import (
    FakeBackend,
    FakeClock,
    RecordingMetrics,
    RecordingOutput,
    StubRunner,
    Text,
    Tool,
    ToolResult,
    Typist,
    lines as lines_of,
    make_hub,
    until,
)

REVIEW = "Tell me when Ada reviews pumpkit#42"


def delegate(request: str = REVIEW) -> Tool:
    return Tool("delegate", {"request": request, "context": ""})


def check(after: Any = 600, note: str = "Has Ada reviewed pumpkit#42?", repeats: Any = 1) -> Tool:
    return Tool("schedule_check", {"after_seconds": after, "note": note, "max_repeats": repeats})


def report(task_id: str, spoken: str) -> Tool:
    return Tool("report", {"task_id": task_id, "spoken": spoken, "needs_user": False})


def results(backend: FakeBackend, role: str, name: str) -> list[ToolResult]:
    return [r for s in backend.all_sessions(role) for r in s.tool_results if r.name == name]


def events(backend: FakeBackend, source: str) -> list[str]:
    received = [m for s in backend.all_sessions("orchestrator") for m in s.received]
    return [m for m in received if m.startswith(f'<event source="{source}"')]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


async def test_a_check_wakes_the_orchestrator_with_a_timer_event_once_its_time_has_passed(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("I'll keep an eye on it, sir."), delegate()], [Text("Ada has reviewed it, sir.")]],
        orchestrator=[[check()], [report("T1", "Ada approved pumpkit 42.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(REVIEW)
    await until(lambda: results(backend, "orchestrator", "schedule_check"))
    [set_check] = results(backend, "orchestrator", "schedule_check")
    assert json.loads(set_check.result or "")["check_id"] == "C1"

    clock.advance(599)
    await asyncio.sleep(0.01)
    assert events(backend, "timer") == []
    clock.advance(1)
    await until(lambda: output.replies[-1:] == ["Ada has reviewed it, sir."])
    typist.end()
    await running

    assert events(backend, "timer") == [
        '<event source="timer" id="C1" task="T1" repeat="1" of="1">Has Ada reviewed pumpkit#42?</event>'
    ]


async def test_a_repeating_check_waits_twice_as_long_each_time_and_stops_after_its_last_repeat(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("I'll keep an eye on it, sir."), delegate()], [Text("No review yet, sir.")]],
        orchestrator=[[check(after=60, repeats=3)], [], [], [report("T1", "Still no review from Ada.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(REVIEW)
    await until(lambda: results(backend, "orchestrator", "schedule_check"))
    for gap, count in ((60, 1), (120, 2), (240, 3)):
        clock.advance(gap - 1)
        await asyncio.sleep(0.01)
        assert len(events(backend, "timer")) == count - 1
        clock.advance(1)
        await until(lambda: len(events(backend, "timer")) == count)
    clock.advance(4 * 3600)
    await asyncio.sleep(0.01)
    await until(lambda: output.replies[-1:] == ["No review yet, sir."])
    typist.end()
    await running

    assert [e.split('repeat="')[1][:1] for e in events(backend, "timer")] == ["1", "2", "3"]
    assert output.notices == []


async def test_a_check_waits_at_least_a_minute_whatever_it_is_asked(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate()]],
        orchestrator=[[check(after=5)], [report("T1", "Checked.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(REVIEW)
    await until(lambda: results(backend, "orchestrator", "schedule_check"))
    [set_check] = results(backend, "orchestrator", "schedule_check")
    assert json.loads(set_check.result or "")["first_in_seconds"] == 60

    clock.advance(59)
    await asyncio.sleep(0.01)
    assert events(backend, "timer") == []
    clock.advance(1)
    await until(lambda: len(events(backend, "timer")) == 1)
    typist.end()
    await running


async def test_checks_past_the_cap_are_refused_until_one_is_cancelled(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    six = [check(note=f"Look {n}") for n in range(1, 7)]
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate()]],
        orchestrator=[
            [*six, Tool("cancel_wait", {"wait_id": "C2"}), check(note="Look 7"), report("T1", "Watching.")]
        ],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines_of(clock, REVIEW))

    outcomes = [r.result or "" for r in results(backend, "orchestrator", "schedule_check")]
    assert [json.loads(o)["check_id"] for o in outcomes[:5]] == ["C1", "C2", "C3", "C4", "C5"]
    assert outcomes[5] == "5 Checks already wait; cancel one with cancel_wait first."
    assert results(backend, "orchestrator", "cancel_wait")[0].result == "Cancelled C2."
    assert json.loads(outcomes[6])["check_id"] == "C6"
    await hub.close()


async def test_the_user_sees_watchers_and_checks_in_status_and_can_cancel_one(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [
            [Text("I'll watch it, sir."), delegate("Tell me when CI on pumpkit#42 is done, and when Ada reviews it")],
            [Text("Let me see."), Tool("status"), Text("Two waits, sir.")],
            [Text("Stopping it."), Tool("cancel", {"task_id": "C1"}), Tool("status")],
        ],
        orchestrator=[
            [Tool("watch", {"kind": "pr_checks", "target": "pumpkit#42"}), check(after=600)],
            [],
        ],
    )
    hub = make_hub(backend, output, metrics, runner=runner)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("Tell me when CI on pumpkit#42 is done, and when Ada reviews it")
    await until(lambda: results(backend, "orchestrator", "schedule_check"))
    clock.advance(120)
    typist.type("What are you waiting on?")
    await until(lambda: results(backend, "talker", "status"))
    typist.type("Forget the review")
    await until(lambda: len(results(backend, "talker", "status")) == 2)
    clock.advance(600)
    await asyncio.sleep(0.01)
    runner.finish(0, 0, "All checks were successful")
    typist.end()
    await running

    first, after_cancel = [r.result for r in results(backend, "talker", "status")]
    task = "T1 working, started 2 min ago: Tell me when CI on pumpkit#42 is done, and when Ada reviews it"
    assert first == (
        f"{task}\n  W1 watching the checks on PR pumpkit#42, 2 min so far\n"
        "  C1 check in 8 min (1 of 1): Has Ada reviewed pumpkit#42?"
    )
    assert results(backend, "talker", "cancel")[0].result == "Cancelled C1."
    assert after_cancel is not None and "C1 check cancelled after 0 of 1" in after_cancel
    assert events(backend, "timer") == []
    assert events(backend, "hub") == [
        '<event source="hub" id="C1" task="T1" status="cancelled">The User cancelled C1; nothing more comes from it.</event>'
    ]


CI = "Tell me when CI on pumpkit run 41666739492 finishes"


@pytest.mark.parametrize(
    ("code", "printed", "status"),
    [(0, "Run CI (41666739492) completed with 'success'", "succeeded"), (1, "X lint failed", "failed")],
)
async def test_a_watcher_runs_a_fixed_gh_command_and_its_end_wakes_the_orchestrator(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, code: int, printed: str, status: str
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [[Text("I'll watch it, sir."), delegate(CI)], [Text("CI is done, sir.")]],
        orchestrator=[
            [Tool("watch", {"kind": "ci_run", "target": "pumpkit-io/pumpkit#41666739492"})],
            [report("T1", "CI on pumpkit is done.")],
        ],
    )
    hub = make_hub(backend, output, metrics, runner=runner)

    await hub.start()
    running = asyncio.create_task(hub.run(lines_of(clock, CI)))
    await until(lambda: runner.started)
    await asyncio.sleep(0.01)
    assert not running.done() and events(backend, "watcher") == []
    runner.finish(0, code, printed)
    await running

    assert runner.started == [
        (
            "gh", "run", "watch", "41666739492", "-R", "pumpkit-io/pumpkit",
            "--exit-status", "--compact", "--interval", "30",
        )
    ]  # fmt: skip
    assert events(backend, "watcher") == [
        f'<event source="watcher" id="W1" task="T1" kind="ci_run" target="pumpkit#41666739492" status="{status}">'
        f"{printed}</event>"
    ]
    assert output.replies[-1] == "CI is done, sir."


@pytest.mark.parametrize(
    ("kind", "target", "refusal"),
    [
        ("shell", "pumpkit#42", "kind is one of ci_run, pr_checks."),
        ("pr_checks", "someone/fork#42", "someone/fork#42 is outside the Org pumpkit-io."),
        ("pr_checks", "pumpkit#42; rm -rf ~", "target is an Org repo and a number, such as pumpkit#42."),
        ("pr_checks", "pumpkit#--web", "target is an Org repo and a number, such as pumpkit#42."),
        ("ci_run", "../x#1", "target is an Org repo and a number, such as pumpkit#42."),
        ("ci_run", "pumpkit", "target is an Org repo and a number, such as pumpkit#42."),
    ],
)
async def test_a_watch_outside_the_kinds_or_the_org_is_refused_and_nothing_runs(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, kind: str, target: str, refusal: str
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate(CI)]],
        orchestrator=[[Tool("watch", {"kind": kind, "target": target}), report("T1", "I can't watch that.")]],
    )
    hub = make_hub(backend, output, metrics, runner=runner)

    await hub.start()
    await hub.run(lines_of(clock, CI))

    assert results(backend, "orchestrator", "watch")[0].result == refusal
    assert runner.started == []


async def test_pr_checks_are_watched_until_they_finish_or_one_fails(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate(CI)]],
        orchestrator=[[Tool("watch", {"kind": "pr_checks", "target": "jarvis#7"})], [report("T1", "Checks passed.")]],
    )
    hub = make_hub(backend, output, metrics, runner=runner)

    await hub.start()
    running = asyncio.create_task(hub.run(lines_of(clock, CI)))
    await until(lambda: runner.started)
    runner.finish(0, 0, "")
    await running

    assert runner.started == [
        ("gh", "pr", "checks", "7", "-R", "pumpkit-io/jarvis", "--watch", "--fail-fast", "--interval", "30")
    ]


async def test_cancelling_the_task_stops_its_watcher_process_and_its_checks_without_any_event(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate(CI)], [Text("Cancelled, sir."), Tool("cancel", {"task_id": "T1"})]],
        orchestrator=[[Tool("watch", {"kind": "ci_run", "target": "pumpkit#1"}), check()]],
    )
    hub = make_hub(backend, output, metrics, runner=runner)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(CI)
    await until(lambda: results(backend, "orchestrator", "schedule_check"))
    typist.type("Never mind")
    typist.end()
    await running
    clock.advance(3600)
    await asyncio.sleep(0.01)

    assert runner.killed == runner.started
    assert events(backend, "watcher") == events(backend, "timer") == []
    await hub.close()


async def test_a_watcher_that_outlasts_its_limit_ends_as_timed_out(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate(CI)]],
        orchestrator=[[Tool("watch", {"kind": "ci_run", "target": "pumpkit#1"})], [report("T1", "CI is stuck.")]],
    )
    hub = make_hub(backend, output, metrics, runner=runner, watch_limit=3600)

    await hub.start()
    running = asyncio.create_task(hub.run(lines_of(clock, CI)))
    await until(lambda: runner.started)
    clock.advance(3600)
    await running

    assert runner.killed == runner.started
    [event] = events(backend, "watcher")
    assert 'status="timed_out">Still not over after 60 min.</event>' in event



async def test_a_watcher_the_orchestrator_cancels_stops_its_process_and_sends_no_event(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    runner = StubRunner()
    backend = FakeBackend(
        clock,
        [[Text("Very well, sir."), delegate(CI)]],
        orchestrator=[
            [
                Tool("watch", {"kind": "ci_run", "target": "pumpkit#1"}),
                Tool("cancel_wait", {"wait_id": "w1"}),
                report("T1", "Never mind the run."),
            ]
        ],
    )
    hub = make_hub(backend, output, metrics, runner=runner)

    await hub.start()
    await hub.run(lines_of(clock, CI))

    assert results(backend, "orchestrator", "cancel_wait")[0].result == "Cancelled W1."
    assert runner.killed == runner.started
    assert events(backend, "watcher") == []


@pytest.mark.real_github
async def test_a_real_watcher_on_a_finished_ci_run_of_an_org_repo_returns_at_once_with_its_result() -> None:
    listed = subprocess.run(
        ["gh", "run", "list", "-R", "pumpkit-io/pumpkit", "--status", "success", "-L", "1", "--json", "databaseId"],
        capture_output=True,
        text=True,
        check=True,
    )
    run_id = json.loads(listed.stdout)[0]["databaseId"]
    command = watch_command("ci_run", f"pumpkit#{run_id}", "pumpkit-io")
    assert not isinstance(command, Refused)

    async with asyncio.timeout(30):
        code, printed = await run_confined(command[1])

    assert code == 0
    assert f"({run_id}) has already completed with 'success'" in printed
