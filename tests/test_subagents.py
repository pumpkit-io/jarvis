"""Subagents end to end through the Hub: the Orchestrator starts them, the Hub runs them and posts their ends back."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    FakeWorkspace,
    Fail,
    Gate,
    RecordingMetrics,
    RecordingOutput,
    Set,
    Text,
    Tool,
    ToolResult,
    Typist,
    lines,
    make_hub,
    until,
)

TYPO = "Open a PR fixing typo X in pumpkit"
BRIEF = "Fix the typo 'recieve' in pumpkit's README, then open a PR."
PR_REPORT = "Opened pumpkit#43, which fixes the typo.\n\nBranch fix/fix-typo-x-in-pumpkit, one commit."


def delegate(request: str, task_id: str | None = None) -> Tool:
    args = {"request": request, "context": ""}
    return Tool("delegate", args if task_id is None else {**args, "task_id": task_id})


def spawn(repo: str = "pumpkit", brief: str = BRIEF, **overrides: Any) -> Tool:
    args: dict[str, Any] = {
        "repo": repo,
        "title": "Fix typo X in pumpkit",
        "brief": brief,
        "model": "sonnet",
        "effort": "medium",
        "writes": True,
        "branch_kind": "fix",
    }
    return Tool("spawn_subagent", {**args, **overrides})


def report(task_id: str, spoken: str, details: str | None = None, needs_user: bool = False) -> Tool:
    args: dict[str, Any] = {"task_id": task_id, "spoken": spoken, "needs_user": needs_user}
    if details is not None:
        args["details_markdown"] = details
    return Tool("report", args)


def results(backend: FakeBackend, name: str) -> list[ToolResult]:
    return [r for s in backend.all_sessions("orchestrator") for r in s.tool_results if r.name == name]


def orchestrator_messages(backend: FakeBackend) -> list[str]:
    return [m for s in backend.all_sessions("orchestrator") for m in s.received]


def subagent_events(backend: FakeBackend) -> list[str]:
    return [m for m in orchestrator_messages(backend) if m.startswith('<event source="subagent"')]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


async def test_a_typo_fix_ends_with_a_pr_from_a_subagent_in_its_own_worktree_and_one_line_with_details(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    workspace = FakeWorkspace()
    backend = FakeBackend(
        clock,
        [[Text("Right away, sir."), delegate(TYPO)], [Text("The PR is open, sir; details on screen.")]],
        orchestrator=[
            [spawn()],
            [report("T1", "The typo fix is up as pumpkit 43.", "PR: https://github.com/pumpkit-io/pumpkit/pull/43")],
        ],
        subagents={BRIEF: [Text("Fixing it."), Tool("Bash", {"command": "gh pr create"}), Text(PR_REPORT)]},
    )
    hub = make_hub(backend, output, metrics, workspace=workspace, log_dir=tmp_path)

    await hub.start()
    await hub.run(lines(clock, TYPO))

    [spawned] = results(backend, "spawn_subagent")
    started = json.loads(spawned.result or "")
    assert (started["subagent_id"], started["branch"], started["state"]) == ("S1", "fix/fix-typo-x-in-pumpkit", "running")
    assert workspace.worktrees == [("pumpkit", "fix/fix-typo-x-in-pumpkit")]
    worktree = Path("/wt/pumpkit/fix-typo-x-in-pumpkit")
    [session] = backend.all_sessions("subagent")
    assert (session.spec.cwd, session.spec.model, session.spec.effort) == (worktree, "sonnet", "medium")
    assert session.received == [BRIEF]
    assert workspace.released == [worktree] and not workspace.locks
    [event] = subagent_events(backend)
    assert event.startswith('<event source="subagent" id="S1" task="T1" status="done">')
    assert "Opened pumpkit#43" in event and "No commits beyond origin/main." in event
    assert output.replies == ["Right away, sir.", "The PR is open, sir; details on screen."]
    assert output.shown == ["PR: https://github.com/pumpkit-io/pumpkit/pull/43"]


async def test_a_writing_subagent_is_locked_under_its_id_while_it_runs(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    workspace = FakeWorkspace()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(TYPO)]],
        orchestrator=[[spawn()], [report("T1", "Done.")]],
        subagents={BRIEF: [Gate(release), Text("Done.")]},
    )
    hub = make_hub(backend, output, metrics, workspace=workspace)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, TYPO)))
    await until(lambda: backend.all_sessions("subagent"))

    assert workspace.locks == {Path("/wt/pumpkit/fix-typo-x-in-pumpkit"): "jarvis S1 (T1): Fix typo X in pumpkit"}
    release.set()
    await running
    assert not workspace.locks


async def test_a_repo_outside_the_org_is_refused_and_a_missing_one_is_cloned_first(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    workspace = FakeWorkspace(org=("pumpkit", "motion-studio"), cloned=("pumpkit",))
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate("Look at motion-studio and someone's fork")]],
        orchestrator=[
            [spawn(repo="someone/fork", brief="a"), spawn(repo="motion-studio", brief="b", writes=False)],
            [report("T1", "Looked.")],
        ],
    )
    hub = make_hub(backend, output, metrics, workspace=workspace)

    await hub.start()
    await hub.run(lines(clock, "Look at motion-studio and someone's fork"))

    refused, started = results(backend, "spawn_subagent")
    assert refused.result == "someone/fork is outside the Org pumpkit-io."
    assert json.loads(started.result or "")["branch"] is None
    assert workspace.clones == ["motion-studio"] and workspace.worktrees == []
    [session] = backend.all_sessions("subagent")
    assert session.received == ["b"] and session.spec.cwd == Path("/org/motion-studio")


@pytest.mark.parametrize(
    ("request_words", "overrides", "max_effort", "refusal"),
    [
        (TYPO, {"effort": "max", "model": "opus"}, "max", "max is used only when the User asks for it"),
        (TYPO + ", at max effort", {"effort": "max", "model": "opus"}, "max", None),
        (TYPO, {"effort": "high"}, "medium", "high is above JARVIS_MAX_EFFORT=medium"),
        (TYPO, {"model": "gpt-5"}, "max", "'gpt-5' is not a model"),
    ],
    ids=["max unasked", "max asked", "above the cap", "unknown model"],
)
async def test_the_hub_checks_model_and_effort_before_starting_anything(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    request_words: str,
    overrides: dict[str, Any],
    max_effort: Any,
    refusal: str | None,
) -> None:
    workspace = FakeWorkspace()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(request_words)]],
        orchestrator=[[spawn(**overrides), report("T1", "Tried.")], [report("T1", "Done.")]],
    )
    hub = make_hub(backend, output, metrics, workspace=workspace, max_effort=max_effort)

    await hub.start()
    await hub.run(lines(clock, request_words))

    [result] = results(backend, "spawn_subagent")
    if refusal is None:
        assert json.loads(result.result or "")["subagent_id"] == "S1"
        assert len(backend.all_sessions("subagent")) == 1
    else:
        assert refusal in (result.result or "")
        assert backend.all_sessions("subagent") == [] and workspace.worktrees == []


async def test_subagents_past_the_cap_wait_in_a_queue_and_each_starts_as_a_slot_frees(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    first_done = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate("Two fixes"), Tool("status")]],
        orchestrator=[
            [spawn(brief="one", title="One"), spawn(brief="two", title="Two")],
            [report("T1", "One done.")],
            [report("T1", "Both done.")],
        ],
        subagents={"one": [Gate(first_done), Text("One done.")], "two": [Text("Two done.")]},
    )
    hub = make_hub(backend, output, metrics, cap=1)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "Two fixes")))
    await until(lambda: len(results(backend, "spawn_subagent")) == 2)
    await asyncio.sleep(0.01)

    states = [json.loads(r.result or "")["state"] for r in results(backend, "spawn_subagent")]
    assert states == ["running", "queued"]
    assert [s.received for s in backend.all_sessions("subagent")] == [["one"]]
    first_done.set()
    await running
    assert [s.received for s in backend.all_sessions("subagent")] == [["one"], ["two"]]
    assert [e.split('status="')[1].split('"')[0] for e in subagent_events(backend)] == ["done", "done"]


async def test_status_lists_each_subagent_under_its_task(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(TYPO)], [Text("Let me see."), Tool("status"), Set(release), Text("Working.")]],
        orchestrator=[[spawn()], [report("T1", "Done.")]],
        subagents={BRIEF: [Gate(release), Text("Done.")]},
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    typist = Typist(clock)
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(TYPO)
    await until(lambda: backend.all_sessions("subagent"))
    typist.type("What are you working on?")
    typist.end()
    await running

    [status] = [r for r in backend.session("talker").tool_results if r.name == "status"]
    assert status.result == (
        f"T1 working, started 0 s ago: {TYPO}\n"
        "  S1 running 0 s, sonnet at medium, pumpkit on fix/fix-typo-x-in-pumpkit: Fix typo X in pumpkit"
    )


async def test_past_the_time_limit_the_orchestrator_is_asked_to_check_with_the_user_and_the_subagent_goes_on(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(TYPO)], [Text("It's been an hour, sir. Shall it go on?")], [Text("Done, sir.")]],
        orchestrator=[
            [spawn()],
            [report("T1", "The typo fix has run an hour; I'd let it go on. Continue?", needs_user=True), Set(release)],
            [report("T1", "The typo fix is up.")],
        ],
        subagents={BRIEF: [Gate(release), Text("Done.")]},
    )
    hub = make_hub(backend, output, metrics, time_limit=3600)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, TYPO)))
    await until(lambda: backend.all_sessions("subagent"))
    clock.advance(3600)
    await running

    over, done = subagent_events(backend)
    assert over.startswith('<event source="subagent" id="S1" task="T1" status="over_time">')
    assert "has run 60 minutes" in over
    assert done.startswith('<event source="subagent" id="S1" task="T1" status="done">')
    assert output.replies[-2:] == ["It's been an hour, sir. Shall it go on?", "Done, sir."]


async def test_a_failed_subagent_posts_a_failed_event_and_progress_goes_to_its_log(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(TYPO)]],
        orchestrator=[[spawn()], [report("T1", "The typo fix failed.")]],
        subagents={BRIEF: [Text("Looking."), Tool("Read", {"file_path": "README.md"}), Fail("rate limited")]},
    )
    hub = make_hub(backend, output, metrics, log_dir=tmp_path)

    await hub.start()
    await hub.run(lines(clock, TYPO))

    [event] = subagent_events(backend)
    assert event.startswith('<event source="subagent" id="S1" task="T1" status="failed">Failed: rate limited.')
    rows = [json.loads(line) for line in (tmp_path / "S1.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["start", "text", "tool", "end"]
    assert rows[0]["brief"] == BRIEF and rows[0]["branch"] == "fix/fix-typo-x-in-pumpkit"
    assert rows[1]["text"] == "Looking." and rows[2] == {**rows[2], "name": "Read", "input": {"file_path": "README.md"}}
    assert (rows[3]["outcome"], rows[3]["error"]) == ("failed", "rate limited")


async def test_cancel_subagent_stops_it_keeps_its_branch_and_the_report_says_what_is_on_it(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    workspace = FakeWorkspace()
    worktree = Path("/wt/pumpkit/fix-typo-x-in-pumpkit")
    workspace.on_branch[worktree] = "Commits not on origin/main:\n- abc1234 fix: correct the typo\nNot pushed."
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate(TYPO)], [Text("Stopping it."), delegate("Stop the typo fix", "T1")]],
        orchestrator=[
            [spawn()],
            [Tool("cancel_subagent", {"subagent_id": "S1"})],
            [report("T1", "Stopped the typo fix; its branch has one commit.")],
        ],
        subagents={BRIEF: [Text("Working."), AwaitInterrupt()]},
    )
    hub = make_hub(backend, output, metrics, workspace=workspace)

    await hub.start()
    typist = Typist(clock)
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(TYPO)
    await until(lambda: backend.all_sessions("subagent"))
    typist.type("Stop the typo fix")
    typist.end()
    await running

    [cancelled] = results(backend, "cancel_subagent")
    assert cancelled.result == "Stopping S1; its report follows as an event. Its worktree and branch are kept."
    [event] = subagent_events(backend)
    assert event.startswith('<event source="subagent" id="S1" task="T1" status="cancelled">Cancelled before it')
    assert f"Branch fix/fix-typo-x-in-pumpkit, kept in {worktree}" in event and "abc1234 fix: correct the typo" in event
    assert workspace.released == [worktree]
    assert backend.all_sessions("subagent")[0].closed


async def test_cancelling_the_task_stops_its_subagents_and_the_hub_shows_what_their_branches_keep(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    workspace = FakeWorkspace()
    worktree = Path("/wt/pumpkit/fix-typo-x-in-pumpkit")
    workspace.on_branch[worktree] = "No commits beyond origin/main.\nUncommitted changes:\n M README.md"
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate(TYPO)],
            [Text("Cancelling."), Tool("cancel", {"task_id": "T1"})],
            [Text("It stopped, sir; its branch is kept.")],
        ],
        orchestrator=[[spawn()]],
        subagents={BRIEF: [AwaitInterrupt()]},
    )
    hub = make_hub(backend, output, metrics, workspace=workspace)

    await hub.start()
    typist = Typist(clock)
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(TYPO)
    await until(lambda: backend.all_sessions("subagent"))
    typist.type("Cancel that")
    typist.end()
    await running

    assert subagent_events(backend) == []
    last = backend.session("talker").received[-1]
    assert last == (
        '<event source="hub" task="T1" needs_user="false" details="on screen">'
        "S1 on T1 stopped; its branch fix/fix-typo-x-in-pumpkit is kept, details on screen.</event>"
    )
    assert "M README.md" in output.shown[-1]
    assert output.replies[-1] == "It stopped, sir; its branch is kept."


async def test_a_queued_subagent_cancelled_ends_without_ever_starting(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it."), delegate("Two fixes")]],
        orchestrator=[
            [spawn(brief="one", title="One"), spawn(brief="two", title="Two"), Tool("cancel_subagent", {"subagent_id": "S2"})],
            [report("T1", "Two was cancelled."), Set(release)],
            [report("T1", "One done.")],
        ],
        subagents={"one": [Gate(release), Text("One done.")]},
    )
    hub = make_hub(backend, output, metrics, cap=1)

    await hub.start()
    await hub.run(lines(clock, "Two fixes"))

    cancelled, done = subagent_events(backend)
    assert cancelled.startswith('<event source="subagent" id="S2" task="T1" status="cancelled">Cancelled before it started.')
    assert done.startswith('<event source="subagent" id="S1" task="T1" status="done">')
    assert [s.received for s in backend.all_sessions("subagent")] == [["one"]]


async def test_two_tasks_on_different_repos_run_side_by_side_and_both_report(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    both_running = asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("On it."), delegate(TYPO)],
            [Text("On it."), delegate("Bump the deploy script in pumpkit-ops")],
            [Text("The typo fix is up, sir.")],
            [Text("The deploy script is bumped, sir.")],
        ],
        orchestrator=[
            [spawn()],
            [spawn(repo="pumpkit-ops", brief="ops", title="Bump the deploy script", branch_kind="chore")],
            [report("T1", "The typo fix is up.")],
            [report("T2", "The deploy script is bumped.")],
        ],
        subagents={BRIEF: [Gate(both_running), Text("Typo fixed.")], "ops": [Set(both_running), Text("Bumped.")]},
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, TYPO, "Bump the deploy script in pumpkit-ops"))

    assert sorted(s.spec.cwd.parent.name for s in backend.all_sessions("subagent")) == ["pumpkit", "pumpkit-ops"]
    tasks = sorted(e.split('task="')[1].split('"')[0] for e in subagent_events(backend))
    assert tasks == ["T1", "T2"]
    assert sorted(output.replies[-2:]) == ["The deploy script is bumped, sir.", "The typo fix is up, sir."]


async def test_closing_the_hub_stops_running_subagents_and_keeps_their_branches(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    workspace = FakeWorkspace()
    backend = FakeBackend(
        clock, [[Text("On it."), delegate(TYPO)]], orchestrator=[[spawn()]], subagents={BRIEF: [AwaitInterrupt()]}
    )
    hub = make_hub(backend, output, metrics, workspace=workspace)

    await hub.start()
    typist = Typist(clock)
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type(TYPO)
    await until(lambda: backend.all_sessions("subagent"))
    await hub.close()
    running.cancel()

    [session] = backend.all_sessions("subagent")
    assert session.closed and workspace.released == [Path("/wt/pumpkit/fix-typo-x-in-pumpkit")]
