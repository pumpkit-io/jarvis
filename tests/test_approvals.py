"""Approvals through the one seam (ADR 0004, ADR 0013): the Orchestrator asks, the User's yes is recorded, and the
Hub merges or deletes with a stub gh, or hands the action to a Subagent whose brief carries the Approval."""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.agents.backend import McpGuard, Refused
from jarvis_hub.approvals import Approval, Approvals, rules_argv
from jarvis_hub.hub import Hub, Line
from jarvis_hub.subagent_rules import connector_check
from jarvis_hub.waits import run_confined
from tests.fakes import (
    FakeBackend,
    FakeClock,
    RecordingMetrics,
    RecordingOutput,
    Step,
    StubGh,
    Text,
    Tool,
    ToolResult,
    Typist,
    make_hub,
    ruleset_rule,
    until,
)

MERGE_42 = "Merge pumpkit#42 once CI is green"
HEAD = "3f9c2b1e5a7d4c6b8e0f1a2b3c4d5e6f7a8b9c0d"
PUSHED = "7d41aa90c3e2b1f0a9e8d7c6b5a4f3e2d1c0b9a8"
TIP = "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00"
ASK = "CI on pumpkit#42 is green. Shall I merge it?"
ASKED = "CI on 42 is green, sir. Shall I merge it?"
POST_NOW = "mcp__claude_ai_Pumpkit__post_now"
# What gh prints for the rules of a private repo on GitHub Free, where no ruleset is enforced (pumpkit-ops, 2026-11-04).
NO_RULESETS = "Upgrade to GitHub Pro or make this repository public to enable this feature."


def delegate(request: str = MERGE_42) -> Tool:
    return Tool("delegate", {"request": request, "context": ""})


def delegate_to(request: str, task_id: str) -> Tool:
    return Tool("delegate", {"request": request, "context": "", "task_id": task_id})


def ask_merge(task_id: str = "T1", sha: str = HEAD, spoken: str = ASK, pr: int = 42) -> Tool:
    approval = {"action": "merge_pr", "repo": "pumpkit", "pr": pr, "sha": sha}
    return Tool("report", {"task_id": task_id, "spoken": spoken, "needs_user": True, "approval": approval})


def ask_delete(task_id: str = "T1", branch: str = "feat/old-idea", sha: str = TIP) -> Tool:
    approval = {"action": "delete_branch", "repo": "pumpkit", "branch": branch, "sha": sha}
    spoken = f"{branch} is merged. Shall I delete it?"
    return Tool("report", {"task_id": task_id, "spoken": spoken, "needs_user": True, "approval": approval})


def ask_post(task_id: str = "T1") -> Tool:
    approval = {"action": "subagent_action", "grants": ["x_post"], "post": "812", "post_action": "post_now"}
    spoken = "The launch thread is ready. Shall I post it now?"
    return Tool("report", {"task_id": task_id, "spoken": spoken, "needs_user": True, "approval": approval})


def report(task_id: str, spoken: str) -> Tool:
    return Tool("report", {"task_id": task_id, "spoken": spoken, "needs_user": False})


def approve(approval_id: str = "A1") -> Tool:
    return Tool("approve", {"approval_id": approval_id})


def results(backend: FakeBackend, role: str, name: str) -> list[ToolResult]:
    return [r for s in backend.all_sessions(role) for r in s.tool_results if r.name == name]


def received(backend: FakeBackend, role: str) -> list[str]:
    return [m for s in backend.all_sessions(role) for m in s.received]


def merge_argv(sha: str = HEAD) -> tuple[str, ...]:
    return ("gh", "pr", "merge", "42", "-R", "pumpkit-io/pumpkit", "--merge", "--match-head-commit", sha)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def gh() -> StubGh:
    return StubGh(prs={"pumpkit-io/pumpkit#42": ("OPEN", HEAD)}, branches={"pumpkit-io/pumpkit:feat/old-idea": TIP})


class Session:
    """A running Hub the test types into, one line at a time, waiting for each reply."""

    def __init__(self, hub: Hub, backend: FakeBackend, output: RecordingOutput, clock: FakeClock) -> None:
        self.hub, self.backend, self.output = hub, backend, output
        self.typist = Typist(clock)
        self.running: asyncio.Task[None] | None = None

    async def start(self, inputs: AsyncIterator[Line] | None = None) -> None:
        await self.hub.start()
        self.running = asyncio.create_task(self.hub.run(inputs or self.typist.lines()))

    async def say(self, text: str, replies: int = 1) -> None:
        before = len(self.output.replies)
        self.typist.type(text)
        await until(lambda: len(self.output.replies) >= before + replies)

    async def replied(self, text: str) -> None:
        await until(lambda: text in self.output.replies)

    async def end(self) -> None:
        self.typist.end()
        assert self.running is not None
        await self.running
        await self.hub.close()


def a_session(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    gh: StubGh,
    talker: list[list[Step]],
    orchestrator: list[list[Step]],
    **options: Any,
) -> Session:
    backend = FakeBackend(
        clock, talker, orchestrator=orchestrator, subagents=options.pop("subagents", None)
    )
    hub = make_hub(backend, output, metrics, runner=gh, **options)
    return Session(hub, backend, output, clock)


async def asked_to_merge(session: Session) -> None:
    await session.start()
    await session.say(MERGE_42)
    await session.replied(ASKED)


async def test_a_report_asking_for_an_approval_names_the_exact_action_and_the_commit_it_covers(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Let me see, sir."), Tool("status")]],
        [[ask_merge()]],
    )

    await asked_to_merge(session)
    await session.say("What's pending?")
    await session.end()

    [asked] = results(session.backend, "orchestrator", "report")
    assert asked.result == f"Reported to the User, asking for Approval A1: merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]}."
    assert received(session.backend, "talker")[2] == (
        f'<event source="orchestrator" task="T1" needs_user="true" approval="A1" details="on screen">{ASK}</event>'
    )
    assert output.shown == [f"**Approval A1:** merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]}"]
    [status] = results(session.backend, "talker", "status")
    assert f"A1 pending, asked 0 s ago: merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]}" in str(status.result)
    assert gh.actions == []


async def test_a_request_whose_commit_is_not_the_pr_head_is_refused_and_nobody_is_asked(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text("Merged? Not yet, sir.")]],
        [[ask_merge(sha=PUSHED), report("T1", "Someone pushed to 42; I'll look again.")]],
    )

    await session.start()
    await session.say(MERGE_42)
    await session.replied("Merged? Not yet, sir.")
    await session.end()

    [refused, plain] = results(session.backend, "orchestrator", "report")
    assert refused.result == (
        f"Not asked: the head of pumpkit-io/pumpkit#42 is now {HEAD[:12]}, not {PUSHED[:12]}. "
        "Look again and ask about what is there now."
    )
    assert plain.result == "Reported to the User."
    assert not any("approval=" in m for m in received(session.backend, "talker"))


async def test_a_typed_yes_right_after_the_question_merges_the_pr_through_the_hub_with_the_users_words(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Merging now, sir."), approve()],
            [Text("Merged, sir.")],
        ],
        [[ask_merge()], [report("T1", "pumpkit#42 is merged.")]],
    )

    await asked_to_merge(session)
    await session.say("Yes, go ahead and merge it.")
    await session.replied("Merged, sir.")
    await session.end()

    [approved] = results(session.backend, "talker", "approve")
    assert approved.result is not None and approved.result.startswith("Approved A1 with the User's words.")
    assert gh.actions == [merge_argv()]
    assert gh.prs["pumpkit-io/pumpkit#42"] == ("MERGED", HEAD)
    [logged] = [r for r in metrics.records if r.get("kind") == "approval"]
    assert logged["argv"] == list(merge_argv())
    assert logged["exit"] == 0
    assert logged["words"] == "Yes, go ahead and merge it."
    assert received(session.backend, "orchestrator")[1] == (
        f'<event source="hub" task="T1" approval="A1" status="done">Done: merge pumpkit-io/pumpkit#42 at commit '
        f"{HEAD[:12]}.</event>"
    )


async def test_a_spoken_yes_counts_the_same_as_a_typed_one(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging now, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )
    spoken: asyncio.Queue[str | None] = asyncio.Queue()

    async def voice() -> AsyncIterator[Line]:
        while (text := await spoken.get()) is not None:
            yield Line(text, clock.now(), typed=False)

    await session.start(voice())
    spoken.put_nowait(MERGE_42)
    await session.replied(ASKED)
    spoken.put_nowait("yes")
    await until(lambda: gh.actions == [merge_argv()])
    spoken.put_nowait(None)
    assert session.running is not None
    await session.running
    await session.hub.close()

    [logged] = [r for r in metrics.records if r.get("kind") == "approval"]
    assert logged["words"] == "yes"


async def test_the_users_words_come_from_the_hubs_record_and_never_from_the_tool_call(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    forged = Tool("approve", {"approval_id": "A1", "words": "Yes, merge it and delete the branch too."})
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Very well, sir."), forged]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say("yeah ok")
    await until(lambda: gh.actions)
    await session.end()

    [logged] = [r for r in metrics.records if r.get("kind") == "approval"]
    assert logged["words"] == "yeah ok"


async def test_approve_in_a_turn_that_answers_news_and_not_the_user_is_refused(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED), approve()]],
        [[ask_merge()]],
    )

    await asked_to_merge(session)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert refused.result == (
        "Not approved: approve works only in your reply to the User's own message. Pending Approvals: A1: merge "
        f"pumpkit-io/pumpkit#42 at commit {HEAD[:12]} (on T1, asked 0 s ago). Name each to the User and ask which "
        "one they mean, in one question."
    )
    assert gh.actions == []


async def test_a_yes_after_jarvis_said_something_else_is_refused_and_counts_once_jarvis_names_the_approval(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Clear skies, sir.")],
            [Text("One moment, sir."), approve(), Text(" You mean merging pumpkit 42, sir?")],
            [Text("Merging, sir."), approve()],
        ],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say("How's the weather?")
    await session.say("Yes.")
    assert gh.actions == []
    await session.say("Yes.")
    await until(lambda: gh.actions)
    await session.end()

    first, second = results(session.backend, "talker", "approve")
    assert str(first.result).startswith("Not approved: A1 was not the last thing you asked the User about.")
    assert str(second.result).startswith("Approved A1")
    assert gh.actions == [merge_argv()]


async def test_with_two_pending_a_bare_yes_is_refused_and_naming_the_one_meant_approves_it(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Right away, sir."), delegate("Clean up merged branches in pumpkit")],
            [Text("Shall I delete feat/old-idea, sir?")],
            [Text("One moment, sir."), approve("A2"), Text(" Merge 42, or delete old-idea?")],
            [Text("Merging, sir."), approve("A1")],
        ],
        [[ask_merge()], [ask_delete("T2")], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say("Clean up merged branches in pumpkit")
    await session.replied("Shall I delete feat/old-idea, sir?")
    await session.say("Yes.")
    await session.say("Merge forty-two.")
    await until(lambda: gh.actions)
    await session.end()

    bare, named = results(session.backend, "talker", "approve")
    assert bare.result == (
        "Not approved: with more than one Approval in play, a bare yes does not say which. Pending Approvals: "
        f"A1: merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]} (on T1, asked 0 s ago); A2: delete branch "
        f"feat/old-idea of pumpkit-io/pumpkit at commit {TIP[:12]} (on T2, asked 0 s ago). Name each to the User "
        "and ask which one they mean, in one question."
    )
    assert str(named.result).startswith("Approved A1")
    assert gh.actions == [merge_argv()]
    assert gh.branches == {"pumpkit-io/pumpkit:feat/old-idea": TIP}


async def test_an_approval_is_single_use(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Merging, sir."), approve(), approve()],
            [Text("Merged, sir.")],
            [Text("Again, sir."), approve()],
        ],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say("Yes.")
    await session.replied("Merged, sir.")
    await session.say("Yes, do it again.")
    await session.end()

    first, twice, again = results(session.backend, "talker", "approve")
    assert str(first.result).startswith("Approved A1")
    assert twice.result == "Not approved: one yes approves one action. Ask about the next one in its own question."
    assert again.result == "Not approved: A1 is not pending. No Approval is pending."
    assert gh.actions == [merge_argv()]


async def test_an_approval_expires_after_24_hours(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()]],
    )

    await asked_to_merge(session)
    clock.advance(24 * 3600)
    await session.say("Yes.")
    await session.end()

    [expired] = results(session.backend, "talker", "approve")
    assert expired.result == "Not approved: A1 is not pending. No Approval is pending."
    assert gh.actions == []


async def test_a_yes_just_inside_24_hours_still_counts(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    clock.advance(24 * 3600 - 1)
    await session.say("Yes.")
    await until(lambda: gh.actions)
    await session.end()

    assert gh.actions == [merge_argv()]


async def test_a_push_after_the_yes_lapses_the_approval_and_brings_a_fresh_request_saying_what_changed(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    fresh = "Someone pushed a new commit to 42 since you said yes. CI is green on it too; merge it?"
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Merging, sir."), approve()],
            [Text("A new commit landed on 42, sir. Merge it?")],
        ],
        [[ask_merge()], [ask_merge(sha=PUSHED, spoken=fresh)]],
    )
    gh.action_gate = asyncio.Event()

    await asked_to_merge(session)
    gh.prs["pumpkit-io/pumpkit#42"] = ("OPEN", PUSHED)
    await session.say("Yes.")
    await session.replied("A new commit landed on 42, sir. Merge it?")
    await session.end()

    assert gh.actions == []
    lapsed = received(session.backend, "orchestrator")[1]
    assert lapsed == (
        '<event source="hub" task="T1" approval="A1" status="lapsed">Not done: the User approved merge '
        f"pumpkit-io/pumpkit#42 at commit {HEAD[:12]}, but the head of pumpkit-io/pumpkit#42 is now {PUSHED[:12]}, "
        f"not {HEAD[:12]}, so the yes no longer covers it. Look at what changed, then ask the User again in a fresh "
        "request that says what changed.</event>"
    )
    assert received(session.backend, "talker")[-1] == (
        f'<event source="orchestrator" task="T1" needs_user="true" approval="A2" details="on screen">{fresh}</event>'
    )


async def test_a_pr_closed_after_the_yes_is_not_merged(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Ada closed 42.")]],
    )

    await asked_to_merge(session)
    gh.prs["pumpkit-io/pumpkit#42"] = ("CLOSED", HEAD)
    await session.say("Yes.")
    await until(lambda: len(received(session.backend, "orchestrator")) == 2)
    await session.end()

    assert gh.actions == []
    assert "but pumpkit-io/pumpkit#42 is closed now, so the yes" in received(session.backend, "orchestrator")[1]


async def test_a_merge_github_refuses_spends_the_approval_and_says_why(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "GitHub refused: a review is required.")]],
    )
    gh.action_result = (1, "X Pull request pumpkit-io/pumpkit#42 is not mergeable: review required.")

    await asked_to_merge(session)
    await session.say("Yes.")
    await until(lambda: len(received(session.backend, "orchestrator")) == 2)
    await session.end()

    assert received(session.backend, "orchestrator")[1] == (
        '<event source="hub" task="T1" approval="A1" status="failed">Not done: merge pumpkit-io/pumpkit#42 at commit '
        f"{HEAD[:12]} failed, and the Approval is spent. gh failed: X Pull request pumpkit-io/pumpkit#42 is not "
        "mergeable: review required.</event>"
    )


async def test_an_approved_branch_delete_runs_only_while_the_branch_still_points_at_the_approved_commit(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")],
            [Text("Delete old-idea, sir?")],
            [Text("Deleting, sir."), approve()],
        ],
        [[ask_delete()], [report("T1", "Deleted.")]],
    )

    await session.start()
    await session.say("Delete feat/old-idea in pumpkit")
    await session.replied("Delete old-idea, sir?")
    await session.say("Yes, delete it.")
    await until(lambda: len(received(session.backend, "orchestrator")) == 2)
    await session.end()

    read, rules = gh.reads[-2:]
    assert read[:3] == ("gh", "api", "graphql")
    assert ("-f", "ref=refs/heads/feat/old-idea") == read[9:11]
    assert rules[:4] == ("gh", "api", "--paginate", "repos/pumpkit-io/pumpkit/rules/branches/feat/old-idea")
    [deleted] = gh.actions
    assert deleted[:3] == ("gh", "api", "graphql")
    assert deleted[3:5] == ("-f", deleted[4]) and deleted[4].startswith("query=mutation(")
    assert 'beforeOid:$beforeOid,afterOid:"0000000000000000000000000000000000000000"' in deleted[4]
    assert deleted[5:] == (
        "-f",
        "repositoryId=R_kgDOpumpkit",
        "-f",
        "name=refs/heads/feat/old-idea",
        "-f",
        f"beforeOid={TIP}",
    )
    assert gh.branches == {}


async def test_a_branch_that_moved_after_the_yes_is_not_deleted(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")],
            [Text("Delete old-idea, sir?")],
            [Text("Deleting, sir."), approve()],
        ],
        [[ask_delete()], [report("T1", "Someone pushed to it.")]],
    )

    await session.start()
    await session.say("Delete feat/old-idea in pumpkit")
    await session.replied("Delete old-idea, sir?")
    gh.branches["pumpkit-io/pumpkit:feat/old-idea"] = PUSHED
    await session.say("Yes.")
    await until(lambda: len(received(session.backend, "orchestrator")) == 2)
    await session.end()

    assert gh.actions == []
    assert f"but branch feat/old-idea now points at {PUSHED[:12]}, not {TIP[:12]}" in (
        received(session.backend, "orchestrator")[1]
    )


@pytest.mark.parametrize(
    ("approval", "refusal"),
    [
        ({"action": "push_to_main", "repo": "pumpkit"}, "approval.action is one of merge_pr, delete_branch, "),
        ({"action": "merge_pr", "repo": "pumpkit", "pr": 42, "sha": "3f9c2b1"}, "approval.sha is the full"),
        ({"action": "merge_pr", "repo": "other-org/pumpkit", "pr": 42, "sha": HEAD}, "other-org/pumpkit is outside"),
        ({"action": "merge_pr", "repo": "pumpkit", "sha": HEAD}, "approval.pr is the PR number"),
        ({"action": "delete_branch", "repo": "pumpkit", "branch": "--force", "sha": TIP}, "approval.branch is a"),
        ({"action": "delete_branch", "repo": "pumpkit", "branch": "a/../b", "sha": TIP}, "approval.branch is a"),
        ({"action": "subagent_action", "description": " "}, "approval.description is required"),
        ({"action": "subagent_action", "description": "Deploy", "grants": ["do_ssh"]}, "approval.grants lists"),
        ({"action": "merge_pr", "repo": "pumpkit", "pr": 7, "sha": HEAD}, "Not asked: the Hub could not read"),
    ],
)
async def test_a_request_outside_the_closed_set_of_actions_is_refused(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    gh: StubGh,
    approval: dict[str, Any],
    refusal: str,
) -> None:
    asking = Tool("report", {"task_id": "T1", "spoken": "Shall I?", "needs_user": True, "approval": approval})
    session = a_session(
        clock, output, metrics, gh, [[Text("Right away, sir."), delegate()]], [[asking, report("T1", "No.")]]
    )

    await session.start()
    await session.say(MERGE_42)
    await session.replied("Very good, sir.")
    await session.end()

    assert str(results(session.backend, "orchestrator", "report")[0].result).startswith(refusal)


async def test_an_approved_x_post_goes_to_a_subagent_whose_brief_carries_it_and_whose_tools_widen_once(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {
            "repo": "pumpkit",
            "title": "Post the launch thread",
            "brief": "Post Pumpkit post 812 on X now.",
            "model": "haiku",
            "effort": "low",
            "writes": False,
            "approval_id": "A1",
        },
    )
    release = asyncio.Event()
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Post the launch thread")],
            [Text("The thread is ready, sir. Post it now?")],
            [Text("Posting, sir."), approve()],
        ],
        [[ask_post()], [spawn], [report("T1", "Posted.")]],
        subagents={},
    )
    session.backend.subagent_open_gate = release
    session.backend.subagent_close_gate = asyncio.Event()

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("The thread is ready, sir. Post it now?")
    await session.say("Yes, post it.")
    await until(lambda: session.backend.subagent_opens == 1)
    orchestrator = received(session.backend, "orchestrator")
    release.set()
    await until(lambda: session.backend.all_sessions("subagent"))
    subagent = session.backend.session("subagent")
    guard = subagent.spec.mcp_guard
    assert guard is not None
    tools = (POST_NOW, "mcp__claude_ai_Pumpkit__delete_scheduled_post", POST_NOW)
    verdicts = [guard(tool, {"post_id": "812"}) for tool in tools]
    session.backend.subagent_close_gate.set()
    await session.end()

    assert orchestrator[1] == (
        '<event source="hub" task="T1" approval="A1" status="approved">The User approved A1 (post Pumpkit post 812 '
        "on X now) in these words: 'Yes, post it.'. Start the Subagent that carries it out "
        "with approval_id A1; its brief then carries the Approval.</event>"
    )
    assert subagent.received[0] == (
        'Post Pumpkit post 812 on X now.\n\n<approval id="A1">\n'
        "The User approved exactly this, once: post Pumpkit post 812 on X now. It lets you call "
        'mcp__claude_ai_Pumpkit__post_now, mcp__pumpkit__post_now once, with exactly these arguments: {"post_id": '
        '"812"}.\nTheir words: Yes, post it.\n</approval>'
    )
    allowed, other, again = verdicts
    assert allowed is None
    assert other == connector_check("mcp__claude_ai_Pumpkit__delete_scheduled_post", {})
    assert again is not None and again.reason == f"A1 is used, so {POST_NOW} needs a fresh Approval."


async def test_a_subagent_without_the_approval_keeps_the_read_only_connectors(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Read post", "brief": "Read post 812.", "model": "haiku", "effort": "low"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate("Post the launch thread")], [Text("Post it, sir?")]],
        [[ask_post(), spawn]],
    )

    await session.start()
    await session.say("Post the launch thread")
    await until(lambda: session.backend.all_sessions("subagent"))
    await session.end()

    guard = session.backend.session("subagent").spec.mcp_guard
    assert guard is None or guard(POST_NOW, {"post_id": "812"}) is not None


async def test_a_subagent_cannot_take_an_approval_the_user_has_not_given(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {
            "repo": "pumpkit",
            "title": "Post",
            "brief": "Post 812.",
            "model": "haiku",
            "effort": "low",
            "approval_id": "A1",
        },
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate("Post the launch thread")], [Text("Post it, sir?")]],
        [[ask_post(), spawn]],
    )

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Post it, sir?")
    await session.end()

    [refused] = results(session.backend, "orchestrator", "spawn_subagent")
    assert refused.result == "A1 is pending, so no Subagent may act on it."
    assert session.backend.all_sessions("subagent") == []


async def test_the_widened_tools_close_again_when_the_approval_expires(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Post", "brief": "Post 812.", "model": "haiku", "effort": "low", "approval_id": "A1"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Post the launch thread")],
            [Text("Post it, sir?")],
            [Text("Posting, sir."), approve()],
        ],
        [[ask_post()], [spawn]],
    )
    session.backend.subagent_open_gate = asyncio.Event()
    session.backend.subagent_close_gate = asyncio.Event()

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Post it, sir?")
    await session.say("Yes, post it now.")
    await until(lambda: session.backend.subagent_opens == 1)
    clock.advance(24 * 3600)
    session.backend.subagent_open_gate.set()
    await until(lambda: session.backend.all_sessions("subagent"))
    guard = session.backend.session("subagent").spec.mcp_guard
    assert guard is not None
    verdict = guard(POST_NOW, {"post_id": "812"})
    session.backend.subagent_close_gate.set()
    await session.end()

    assert verdict is not None and verdict.reason == f"A1 is expired, so {POST_NOW} needs a fresh Approval."


async def test_the_orchestrator_may_not_hand_a_merge_to_a_subagent(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Merge", "brief": "Merge 42.", "model": "haiku", "effort": "low", "approval_id": "A1"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)]],
        [[ask_merge(), spawn]],
    )

    await asked_to_merge(session)
    await session.end()

    [refused] = results(session.backend, "orchestrator", "spawn_subagent")
    assert refused.result == "A1 is for the Hub to carry out, never a Subagent."


async def test_cancelling_the_task_withdraws_its_approval(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Cancelled, sir."), Tool("cancel", {"task_id": "T1"})],
            [Text("Merging, sir."), approve()],
        ],
        [[ask_merge()]],
    )

    await asked_to_merge(session)
    await session.say("Actually, cancel that.")
    await session.say("Yes.")
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert refused.result == "Not approved: A1 is not pending. No Approval is pending."
    assert gh.actions == []


async def test_a_newer_request_on_the_same_task_replaces_the_one_pending(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Noted, sir."), delegate_to("Check 42 again", "T1")],
            [Text("Shall I merge 42, sir?")],
            [Text("Merging, sir."), approve("A2")],
        ],
        [[ask_merge()], [ask_merge(spoken="Still green. Merge pumpkit#42?")], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say("Check 42 again")
    await session.replied("Shall I merge 42, sir?")
    await session.say("Yes.")
    await until(lambda: gh.actions)
    await session.end()

    assert str(results(session.backend, "talker", "approve")[0].result).startswith("Approved A2")
    assert gh.actions == [merge_argv()]


class AlwaysQuiet:
    """A voice that is idle whenever asked, so news is announced at once and Quiet mode applies."""

    async def quiet(self) -> None:
        return None

    def announce(self) -> bool:
        return True


async def test_an_approval_request_gets_through_quiet_mode_and_the_same_request_again_waits(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Quiet it is, sir."), Tool("quiet_mode", {"on": True})],
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Noted, sir."), delegate_to("Check 42 again", "T1")],
        ],
        [[ask_merge()], [ask_merge()]],
        floor=AlwaysQuiet(),
    )

    await session.start()
    await session.say("Quiet mode, please.")
    await session.say(MERGE_42)
    await session.replied(ASKED)
    await session.say("Check 42 again")
    await until(lambda: len(results(session.backend, "orchestrator", "report")) == 2)
    for _ in range(20):
        await asyncio.sleep(0)
    told = [m for m in received(session.backend, "talker") if 'approval="' in m]
    await session.end()

    assert told == [
        f'<event source="orchestrator" task="T1" needs_user="true" approval="A1" details="on screen">{ASK}</event>'
    ]


async def test_a_pending_approval_survives_a_restart_and_is_named_again_before_a_yes_counts(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    first = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)]],
        [[ask_merge()]],
        state_path=state,
    )
    await asked_to_merge(first)
    await first.end()
    saved = json.loads(state.read_text())["approvals"]
    assert [(a["id"], a["state"], a["sha"]) for a in saved] == [("A1", "pending", HEAD)]

    second = a_session(
        clock,
        RecordingOutput(),
        metrics,
        gh,
        [
            [Text("One moment, sir."), approve(), Text(" You mean merging pumpkit 42, sir?")],
            [Text("Merging, sir."), approve()],
        ],
        [[], [report("T1", "Merged.")]],
        state_path=state,
    )
    await second.start()
    await until(lambda: received(second.backend, "orchestrator"))
    assert "A1 pending, asked 0 s ago: merge pumpkit-io/pumpkit#42" in received(second.backend, "orchestrator")[0]
    await second.say("Yes.")
    await second.say("Yes.")
    await until(lambda: gh.actions)
    await second.end()

    refused, approved = results(second.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: A1 was not the last thing you asked the User about.")
    assert str(approved.result).startswith("Approved A1")
    assert gh.actions == [merge_argv()]


async def test_a_merge_a_restart_cut_off_is_read_again_and_not_run_twice(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    first = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()]],
        state_path=state,
    )
    gh.action_gate = asyncio.Event()
    await asked_to_merge(first)
    await first.say("Yes.")
    await until(lambda: gh.actions)
    first.typist.end()
    await first.hub.close()
    assert first.running is not None
    first.running.cancel()
    gh.prs["pumpkit-io/pumpkit#42"] = ("MERGED", HEAD)
    gh.action_gate = None

    second = a_session(
        clock, RecordingOutput(), metrics, gh, [], [[], [report("T1", "42 was merged already.")]], state_path=state
    )
    await second.start()
    await until(lambda: len(received(second.backend, "orchestrator")) == 2)
    await second.end()

    assert gh.actions == [merge_argv()]
    assert "but pumpkit-io/pumpkit#42 is merged now, so the yes" in received(second.backend, "orchestrator")[1]


class ReadsOnly:
    """The real confined gh for reads; any other command fails the test before it runs."""

    async def __call__(self, argv: tuple[str, ...]) -> tuple[int, str]:
        rules = argv[:3] == ("gh", "api", "--paginate") and "/rules/branches/" in argv[3] and "-X" not in argv
        assert argv[:3] == ("gh", "pr", "view") or "query=query(" in " ".join(argv) or rules, argv
        return await run_confined(argv)


@pytest.mark.real_github
async def test_the_hubs_real_reads_see_a_merged_pr_and_a_branch_tip(clock: FakeClock) -> None:
    def gh_read(*args: str) -> str:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout.strip()

    merged_head = gh_read("pr", "view", "1", "-R", "pumpkit-io/jarvis", "--json", "headRefOid", "--jq", ".headRefOid")
    main_tip = gh_read("api", "repos/pumpkit-io/jarvis/git/ref/heads/main", "--jq", ".object.sha")
    approvals = Approvals(
        org="pumpkit-io", runner=ReadsOnly(), clock=clock, on_change=lambda: None, log=lambda entry: None
    )

    async with asyncio.timeout(30):
        merged = await approvals.request("T1", {"action": "merge_pr", "repo": "jarvis", "pr": 1, "sha": merged_head})
        tip = await approvals.request(
            "T1", {"action": "delete_branch", "repo": "jarvis", "branch": "main", "sha": main_tip}
        )

    assert isinstance(merged, Refused) and merged.reason.startswith("Not asked: pumpkit-io/jarvis#1 is merged now.")
    assert isinstance(tip, Refused) and tip.reason == (
        "Not asked: main is the default branch of pumpkit-io/jarvis, and the Hub never deletes the default or a "
        "protected branch."
    )


@pytest.mark.real_github
async def test_the_hubs_real_rules_read_sees_the_ruleset_on_pumpkits_main() -> None:
    async with asyncio.timeout(30):
        code, printed = await ReadsOnly()(rules_argv("pumpkit-io/pumpkit", "main"))

    assert code == 0
    assert {"deletion", "non_fast_forward"} <= set(printed.split())


@pytest.mark.real_github
async def test_the_hubs_real_rules_read_of_a_private_repo_without_rulesets_says_so() -> None:
    async with asyncio.timeout(30):
        code, printed = await ReadsOnly()(rules_argv("pumpkit-io/pumpkit-ops", "main"))

    assert code == 1
    assert NO_RULESETS in printed


async def test_an_approval_held_by_a_subagent_the_hub_stops_is_spent_and_not_saved(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Post", "brief": "Post 812.", "model": "haiku", "effort": "low", "approval_id": "A1"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Post the launch thread")],
            [Text("Post it, sir?")],
            [Text("Posting, sir."), approve()],
        ],
        [[ask_post()], [spawn]],
        state_path=state,
    )
    session.backend.subagent_open_gate = asyncio.Event()

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Post it, sir?")
    await session.say("Yes, post it.")
    await until(lambda: session.backend.subagent_opens == 1)
    pending = json.loads(state.read_text())["approvals"]
    session.typist.end()
    await session.hub.close()
    assert session.running is not None
    session.running.cancel()

    assert [(a["id"], a["state"], a["subagent"]) for a in pending] == [("A1", "granted", "S1")]
    assert json.loads(state.read_text())["approvals"] == []


def two_pending(talker_after: list[list[Step]], orchestrator_after: list[list[Step]] | None = None) -> tuple[
    list[list[Step]], list[list[Step]]
]:
    """A Talker and an Orchestrator that leave A1 (merge 42) and A2 (delete feat/old-idea) pending, A2 asked last."""
    talker: list[list[Step]] = [
        [Text("Right away, sir."), delegate()],
        [Text(ASKED)],
        [Text("Right away, sir."), delegate("Clean up merged branches in pumpkit")],
        [Text("Shall I delete feat/old-idea, sir?")],
    ]
    return talker + talker_after, [[ask_merge()], [ask_delete("T2")], *(orchestrator_after or [])]


async def both_asked(session: Session) -> None:
    await asked_to_merge(session)
    await session.say("Clean up merged branches in pumpkit")
    await session.replied("Shall I delete feat/old-idea, sir?")


@pytest.mark.parametrize(
    "words",
    [
        "No, don't merge it.",
        "No.",
        "Yes, but wait.",
        "Not yet.",
        "Hold on.",
        "Maybe later.",
        "Yes? Is CI green?",
        "Cancel that, go ahead later.",
        "Yeah, I don't think so.",
        "Stop.",
        "No, non farlo.",
        "Sì, ma aspetta.",
        "Forse dopo.",
        "Annulla.",
        "Ferma.",
        "Fine, whatever, I'll look later.",
        "Yes, after the release.",
        "Ok, but first run CI.",
        "Go ahead and cancel it.",
        "Yes. Actually, never mind.",
    ],
)
async def test_words_that_hold_the_yes_back_never_approve_and_the_refusal_says_why(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, words: str
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say(words)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: the User's words hold ")
    assert "Nothing was done" in str(refused.result)
    assert gh.actions == []


@pytest.mark.parametrize("words", ["The weather is nice.", "Hmm.", "Thanks, Jarvis.", "Tell me about 42."])
async def test_words_with_no_yes_in_them_do_not_approve(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, words: str
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say(words)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: the User's words hold no yes")
    assert gh.actions == []


@pytest.mark.parametrize(
    "words",
    [
        "Yes.",
        "yeah go ahead",
        "Yep.",
        "Sure, do it.",
        "Okay, go.",
        "Approved.",
        "Merge it.",
        "Yes, merge 42.",
        "Sì, procedi.",
        "Certo, vai.",
        "Fallo.",
        "Va bene.",
    ],
)
async def test_a_yes_from_the_lexicon_approves(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, words: str
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say(words)
    await until(lambda: gh.actions)
    await session.end()

    assert gh.actions == [merge_argv()]


@pytest.mark.parametrize(
    "words", ["Yes, merge 43.", "Yes, delete it.", "Yes, merge forty-three.", "Yes, and post it on X."]
)
async def test_a_yes_whose_words_name_something_else_is_refused(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, words: str
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say(words)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: the User's words name ")
    assert gh.actions == []


async def test_with_two_in_play_the_words_must_name_the_approval_the_talker_chose(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    talker, orchestrator = two_pending(
        [
            [Text("One moment, sir."), approve("A2"), Text(" Merge 42, or delete old-idea?")],
            [Text("Deleting, sir."), approve("A2")],
        ]
    )
    session = a_session(clock, output, metrics, gh, talker, orchestrator)

    await both_asked(session)
    await session.say("Yes.")
    await session.say("Merge forty-two.")
    await session.end()

    _, wrong = results(session.backend, "talker", "approve")
    assert str(wrong.result).startswith("Not approved: the User's words name A1 (merge pumpkit-io/pumpkit#42")
    assert gh.actions == []
    assert gh.branches == {"pumpkit-io/pumpkit:feat/old-idea": TIP}


@pytest.mark.parametrize(
    ("words", "chosen"),
    [
        ("Merge forty-two.", "A1"),
        ("Yes, number 42.", "A1"),
        ("Sì, unisci la quarantadue.", "A1"),
        ("Yes, the one hundred and forty-two... no, forty-two.", None),
        ("Yes, delete it.", "A2"),
        ("Yes, old idea.", "A2"),
        ("Delete feat/old-idea, yes.", "A2"),
        ("Yes, both.", None),
        ("Yes, merge forty-three.", None),
    ],
)
async def test_with_two_in_play_the_hub_matches_the_target_the_words_name(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    gh: StubGh,
    words: str,
    chosen: str | None,
) -> None:
    talker, orchestrator = two_pending(
        [
            [Text("One moment, sir."), approve("A2"), Text(" Merge 42, or delete old-idea?")],
            [Text("Very well, sir."), approve(chosen or "A1")],
        ]
    )
    session = a_session(clock, output, metrics, gh, talker, orchestrator)

    await both_asked(session)
    await session.say("Yes.")
    await session.say(words)
    if chosen:
        await until(lambda: gh.actions)
    await session.end()

    _, decided = results(session.backend, "talker", "approve")
    if chosen:
        assert str(decided.result).startswith(f"Approved {chosen}")
    else:
        assert str(decided.result).startswith("Not approved:")
        assert gh.actions == []


async def test_after_a_refusal_a_plain_yes_counts_for_the_one_approval_jarvis_read_back(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    talker, orchestrator = two_pending(
        [
            [Text("One moment, sir."), approve("A2"), Text(" You mean deleting old-idea, sir?")],
            [Text("Deleting, sir."), approve("A2")],
        ],
        [[report("T2", "Deleted.")]],
    )
    session = a_session(clock, output, metrics, gh, talker, orchestrator)

    await both_asked(session)
    await session.say("Yes.")
    await session.say("Yes.")
    await until(lambda: gh.actions)
    await session.end()

    _, approved = results(session.backend, "talker", "approve")
    assert str(approved.result).startswith("Approved A2")
    assert gh.branches == {}


async def test_after_a_refusal_that_named_nothing_no_approval_counts_as_last_said(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    talker, orchestrator = two_pending(
        [
            [Text("One moment, sir."), approve("A2"), Text(" Which one, sir?")],
            [Text("Merging, sir."), approve("A1")],
        ]
    )
    session = a_session(clock, output, metrics, gh, talker, orchestrator)

    await both_asked(session)
    await session.say("Yes.")
    await session.say("Merge forty-two.")
    await session.end()

    _, refused = results(session.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: A1 was not the last thing you asked the User about.")
    assert gh.actions == []


async def test_after_a_no_a_plain_yes_does_not_approve(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate()],
            [Text(ASKED)],
            [Text("Merging, sir."), approve(), Text(" Very well, sir, I won't merge 42.")],
            [Text("Merging, sir."), approve()],
        ],
        [[ask_merge()], [report("T1", "Merged.")]],
    )

    await asked_to_merge(session)
    await session.say("No.")
    await session.say("Yes.")
    await session.end()

    _, refused = results(session.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: A1 was not the last thing you asked the User about.")
    assert gh.actions == []


@pytest.mark.parametrize("branch", ["main", "refs/heads/main", "release"])
async def test_the_default_or_a_protected_branch_is_never_asked_about(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, branch: str
) -> None:
    gh = StubGh(
        branches={"pumpkit-io/pumpkit:main": TIP, "pumpkit-io/pumpkit:release": TIP},
        protected={"pumpkit-io/pumpkit:release"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text("Not that one, sir.")]],
        [[ask_delete(branch=branch), report("T1", "I may not delete it.")]],
    )

    await session.start()
    await session.say("Delete the branch")
    await session.replied("Not that one, sir.")
    await session.end()

    refused, _ = results(session.backend, "orchestrator", "report")
    name = branch.removeprefix("refs/heads/")
    kind = "the default branch" if name == "main" else "a protected branch"
    assert refused.result == (
        f"Not asked: {name} is {kind} of pumpkit-io/pumpkit, and the Hub never deletes the default or a protected "
        "branch."
    )
    assert gh.actions == []


async def test_a_branch_protected_after_the_yes_is_not_deleted(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")],
            [Text("Delete old-idea, sir?")],
            [Text("Deleting, sir."), approve()],
        ],
        [[ask_delete()], [report("T1", "It is protected now.")]],
    )

    await session.start()
    await session.say("Delete feat/old-idea in pumpkit")
    await session.replied("Delete old-idea, sir?")
    gh.protected.add("pumpkit-io/pumpkit:feat/old-idea")
    await session.say("Yes.")
    await until(lambda: len(received(session.backend, "orchestrator")) == 2)
    await session.end()

    assert gh.actions == []
    assert "but feat/old-idea is a protected branch of pumpkit-io/pumpkit" in received(session.backend, "orchestrator")[1]


@pytest.mark.parametrize(
    ("field", "value"),
    [("repo", "other-org/pumpkit"), ("branch", "--force"), ("action", "push_to_main"), ("sha", "3f9c2b1")],
)
async def test_an_approval_saved_in_a_shape_no_request_could_have_is_dropped_on_restart(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    gh: StubGh,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    state = tmp_path / "state.json"
    first = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")], [Text("Delete old-idea, sir?")]],
        [[ask_delete()]],
        state_path=state,
    )
    await first.start()
    await first.say("Delete feat/old-idea in pumpkit")
    await first.replied("Delete old-idea, sir?")
    await first.end()
    data = json.loads(state.read_text())
    data["approvals"][0] |= {"state": "approved", field: value}
    state.write_text(json.dumps(data))

    restarted = RecordingOutput()
    second = a_session(clock, restarted, metrics, gh, [], [[]], state_path=state)
    await second.start()
    await until(lambda: received(second.backend, "orchestrator"))
    await second.end()

    assert gh.actions == []
    assert any(n.startswith("Dropped Approval A1 from the state file:") for n in restarted.notices)
    assert json.loads(state.read_text())["approvals"] == []


async def test_an_x_post_approval_opens_the_post_tools_for_its_own_post_only(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Post", "brief": "Post 812.", "model": "haiku", "effort": "low", "approval_id": "A1"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Post the launch thread")],
            [Text("Post it, sir?")],
            [Text("Posting, sir."), approve()],
        ],
        [[ask_post()], [spawn]],
    )
    session.backend.subagent_close_gate = asyncio.Event()

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Post it, sir?")
    await session.say("Yes, post it.")
    await until(lambda: session.backend.all_sessions("subagent"))
    guard = session.backend.session("subagent").spec.mcp_guard
    assert guard is not None
    other = guard(POST_NOW, {"post_id": "813"})
    own = guard(POST_NOW, {"post_id": "812"})
    session.backend.subagent_close_gate.set()
    await session.end()

    assert other is not None and other.reason == "A1 covers Pumpkit post 812 only, not 813."
    assert own is None


async def test_an_x_post_request_must_name_the_post(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    approval = {"action": "subagent_action", "grants": ["x_post"], "post_action": "post_now"}
    asking = Tool("report", {"task_id": "T1", "spoken": "Post it?", "needs_user": True, "approval": approval})
    session = a_session(
        clock, output, metrics, gh, [[Text("Right away, sir."), delegate()]], [[asking, report("T1", "No.")]]
    )

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Very good, sir.")
    await session.end()

    assert str(results(session.backend, "orchestrator", "report")[0].result).startswith(
        "approval.post is the Pumpkit post id"
    )


def merge_asked() -> tuple[list[list[Step]], list[list[Step]]]:
    return (
        [[Text("Right away, sir."), delegate()], [Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )


@pytest.mark.parametrize(
    "words",
    [
        "Sure, abort.",
        "Yes, abort it.",
        "Ok, scratch that.",
        "Ok, let me think about it.",
        "Yes, in an hour.",
        "Merge it next week.",
        "Go away.",
        "Sounds good, nevermind",
        "Yes, the jarvis repo.",
        "Yes, merge number one.",
        "Sure... actually, scrap that.",
        "Fine, whatever.",
        "Yes, eventually.",
        "Go home.",
        "Merge the other one.",
        "Yes, merge both.",
    ],
)
async def test_a_yes_with_any_word_outside_a_plain_yes_is_refused(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, words: str
) -> None:
    session = a_session(clock, output, metrics, gh, *merge_asked())

    await asked_to_merge(session)
    await session.say(words)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert str(refused.result).startswith("Not approved: ")
    assert "plain yes" in str(refused.result)
    assert gh.actions == []


@pytest.mark.parametrize(
    "words",
    [
        "yes",
        "yes please",
        "yes, merge it",
        "go ahead",
        "do it",
        "merge forty-two",
        "yes Jarvis, merge 42",
        "Merge PR 42 now, please.",
        "Yes, merge the pull request.",
        "Yes, number 42.",
        "Yes, merge pumpkit 42.",
        "sì",
        "sì, procedi",
        "vai pure",
        "fallo",
        "ok, unisci la 42",
    ],
)
async def test_a_plain_yes_made_only_of_yes_words_the_verb_the_target_and_filler_approves(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, words: str
) -> None:
    session = a_session(clock, output, metrics, gh, *merge_asked())

    await asked_to_merge(session)
    await session.say(words)
    await until(lambda: gh.actions)
    await session.end()

    assert gh.actions == [merge_argv()]


def jarvis_and_pumpkit_pending(talker_after: list[list[Step]]) -> tuple[list[list[Step]], list[list[Step]]]:
    """A1 merges jarvis#7 and A2 deletes pumpkit's feat/old-idea, both pending, A2 asked last."""
    talker: list[list[Step]] = [
        [Text("Right away, sir."), delegate("Merge jarvis#7")],
        [Text("Shall I merge 7, sir?")],
        [Text("Right away, sir."), delegate("Clean up merged branches in pumpkit")],
        [Text("Shall I delete feat/old-idea, sir?")],
    ]
    merge_7 = Tool(
        "report",
        {
            "task_id": "T1",
            "spoken": "Shall I merge jarvis#7?",
            "needs_user": True,
            "approval": {"action": "merge_pr", "repo": "jarvis", "pr": 7, "sha": HEAD},
        },
    )
    return talker + talker_after, [[merge_7], [ask_delete("T2")]]


@pytest.mark.parametrize(("words", "chosen"), [("Yes, Jarvis.", None), ("Yes, the jarvis repo.", "A1")])
async def test_jarvis_as_address_never_names_the_jarvis_repo_but_the_jarvis_repo_does(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, words: str, chosen: str | None
) -> None:
    gh = StubGh(prs={"pumpkit-io/jarvis#7": ("OPEN", HEAD)}, branches={"pumpkit-io/pumpkit:feat/old-idea": TIP})
    talker, orchestrator = jarvis_and_pumpkit_pending(
        [
            [Text("One moment, sir."), approve("A2"), Text(" Merge 7, or delete old-idea?")],
            [Text("Very well, sir."), approve("A1")],
        ]
    )
    session = a_session(clock, output, metrics, gh, talker, orchestrator)

    await session.start()
    await session.say("Merge jarvis#7")
    await session.replied("Shall I merge 7, sir?")
    await session.say("Clean up merged branches in pumpkit")
    await session.replied("Shall I delete feat/old-idea, sir?")
    await session.say("Yes.")
    await session.say(words)
    if chosen:
        await until(lambda: gh.actions)
    await session.end()

    _, decided = results(session.backend, "talker", "approve")
    if chosen:
        assert str(decided.result).startswith("Approved A1")
        assert gh.prs["pumpkit-io/jarvis#7"] == ("MERGED", HEAD)
    else:
        assert str(decided.result).startswith("Not approved: with more than one Approval in play, a bare yes")
        assert gh.actions == []


@pytest.mark.parametrize("rule", ["deletion", "non_fast_forward", "update"])
async def test_a_branch_a_ruleset_keeps_is_never_asked_about(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, rule: str
) -> None:
    gh.rulesets["pumpkit-io/pumpkit:feat/old-idea"] = [ruleset_rule(rule)]
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text("Not that one, sir.")]],
        [[ask_delete(), report("T1", "I may not delete it.")]],
    )

    await session.start()
    await session.say("Delete the branch")
    await session.replied("Not that one, sir.")
    await session.end()

    refused, _ = results(session.backend, "orchestrator", "report")
    assert refused.result == (
        "Not asked: feat/old-idea is a protected branch of pumpkit-io/pumpkit, and the Hub never deletes the default "
        "or a protected branch."
    )
    assert gh.actions == []


async def test_a_ruleset_that_only_asks_for_reviews_does_not_keep_a_branch(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    gh.rulesets["pumpkit-io/pumpkit:feat/old-idea"] = [ruleset_rule("pull_request")]
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")],
            [Text("Delete old-idea, sir?")],
            [Text("Deleting, sir."), approve()],
        ],
        [[ask_delete()], [report("T1", "Deleted.")]],
    )

    await session.start()
    await session.say("Delete feat/old-idea in pumpkit")
    await session.replied("Delete old-idea, sir?")
    await session.say("Yes.")
    await until(lambda: gh.actions)
    await session.end()

    assert gh.branches == {}


async def test_a_branch_a_ruleset_keeps_after_the_yes_is_not_deleted(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")],
            [Text("Delete old-idea, sir?")],
            [Text("Deleting, sir."), approve()],
        ],
        [[ask_delete()], [report("T1", "It is protected now.")]],
    )

    await session.start()
    await session.say("Delete feat/old-idea in pumpkit")
    await session.replied("Delete old-idea, sir?")
    gh.rulesets["pumpkit-io/pumpkit:feat/old-idea"] = [ruleset_rule("deletion")]
    await session.say("Yes.")
    await until(lambda: len(received(session.backend, "orchestrator")) == 2)
    await session.end()

    assert gh.actions == []
    assert "but feat/old-idea is a protected branch of pumpkit-io/pumpkit" in received(session.backend, "orchestrator")[1]


async def test_a_branch_whose_rules_cannot_be_read_is_not_asked_about(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    gh.rules_result = (1, "HTTP 502: Bad Gateway")
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text("Not now, sir.")]],
        [[ask_delete(), report("T1", "GitHub did not answer.")]],
    )

    await session.start()
    await session.say("Delete the branch")
    await session.replied("Not now, sir.")
    await session.end()

    refused, _ = results(session.backend, "orchestrator", "report")
    assert str(refused.result).startswith("Not asked: the Hub could not read the rules of branch feat/old-idea")
    assert gh.actions == []


async def test_a_branch_in_a_repo_whose_plan_has_no_rulesets_can_be_deleted(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    gh.rules_result = (1, f'{{"message":"{NO_RULESETS}","status":"403"}}gh: {NO_RULESETS} (HTTP 403)')
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [
            [Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")],
            [Text("Delete old-idea, sir?")],
            [Text("Deleting, sir."), approve()],
        ],
        [[ask_delete()], [report("T1", "Deleted.")]],
    )

    await session.start()
    await session.say("Delete feat/old-idea in pumpkit")
    await session.replied("Delete old-idea, sir?")
    await session.say("Yes.")
    await until(lambda: gh.actions)
    await session.end()

    assert gh.branches == {}


FRIDAY_9 = "2026-11-06T09:00:00+01:00"


def ask_schedule() -> Tool:
    approval = {
        "action": "subagent_action",
        "grants": ["x_post"],
        "post": "812",
        "post_action": "schedule_post",
        "publish_at": FRIDAY_9,
    }
    spoken = "The launch thread is ready. Shall I schedule it for Friday at nine?"
    return Tool("report", {"task_id": "T1", "spoken": spoken, "needs_user": True, "approval": approval})


@pytest.mark.parametrize(
    ("asking", "words", "opened", "arguments", "closed"),
    [
        (ask_post(), "Yes, post it now.", POST_NOW, {"post_id": "812"}, "mcp__claude_ai_Pumpkit__schedule_post"),
        (
            ask_schedule(),
            "Yes, schedule it.",
            "mcp__pumpkit__schedule_post",
            {"post_id": "812", "publish_at": FRIDAY_9},
            "mcp__pumpkit__post_now",
        ),
    ],
)
async def test_an_x_post_approval_opens_only_the_post_tool_of_its_action(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    gh: StubGh,
    asking: Tool,
    words: str,
    opened: str,
    arguments: dict[str, object],
    closed: str,
) -> None:
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Post", "brief": "Do it.", "model": "haiku", "effort": "low", "approval_id": "A1"},
    )
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate("Post the launch thread")], [Text("Shall I " + ("schedule" if asking.input["approval"]["post_action"] == "schedule_post" else "post") + " it, sir?")], [approve()]],
        [[asking], [spawn]],
    )
    session.backend.subagent_close_gate = asyncio.Event()

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Shall I " + ("schedule" if asking.input["approval"]["post_action"] == "schedule_post" else "post") + " it, sir?")
    await session.say(words)
    await until(lambda: session.backend.all_sessions("subagent"))
    guard = session.backend.session("subagent").spec.mcp_guard
    assert guard is not None
    other = guard(closed, {"post_id": "812"})
    own = guard(opened, arguments)
    session.backend.subagent_close_gate.set()
    await session.end()

    assert other == connector_check(closed, {})
    assert own is None


@pytest.mark.parametrize(
    ("asking", "words"), [(ask_post(), "Post it on Friday."), (ask_schedule(), "Yes, post it now.")]
)
async def test_a_yes_that_changes_when_a_post_goes_out_is_refused(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, asking: Tool, words: str
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate("Post the launch thread")], [Text("Shall I " + ("schedule" if asking.input["approval"]["post_action"] == "schedule_post" else "post") + " it, sir?")], [approve()]],
        [[asking]],
    )

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Shall I " + ("schedule" if asking.input["approval"]["post_action"] == "schedule_post" else "post") + " it, sir?")
    await session.say(words)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert "which is not part of a plain yes" in str(refused.result)


async def test_an_x_post_request_must_say_whether_it_posts_now_or_schedules(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    approval = {"action": "subagent_action", "grants": ["x_post"], "post": "812"}
    asking = Tool("report", {"task_id": "T1", "spoken": "Post it?", "needs_user": True, "approval": approval})
    session = a_session(
        clock, output, metrics, gh, [[Text("Right away, sir."), delegate()]], [[asking, report("T1", "No.")]]
    )

    await session.start()
    await session.say("Post the launch thread")
    await session.replied("Very good, sir.")
    await session.end()

    assert str(results(session.backend, "orchestrator", "report")[0].result).startswith(
        "approval.post_action is post_now or schedule_post"
    )


async def decided(
    specs: list[dict[str, Any]],
    said: str,
    approval_id: str,
    told: list[str] | None = None,
    read_back: str | None = None,
) -> Approval | str:
    """What `decide` returns for `said`, after Jarvis asked about the Approvals `told` (every one of `specs` by
    default) in one turn, and, with `read_back`, after a bare yes was refused and Jarvis replied `read_back`."""
    gh = StubGh(
        prs={f"pumpkit-io/{s['repo']}#{s['pr']}": ("OPEN", HEAD) for s in specs if s["action"] == "merge_pr"},
        branches={f"pumpkit-io/{s['repo']}:{s['branch']}": TIP for s in specs if s["action"] == "delete_branch"},
    )
    approvals = Approvals(org="pumpkit-io", runner=gh, clock=FakeClock(), on_change=lambda: None, log=lambda e: None)
    asked = [await approvals.request(f"T{i}", spec) for i, spec in enumerate(specs, 1)]
    approvals.turn_began(None)
    told = told or [getattr(a, "id", "") for a in asked]
    questions: list[str] = []
    for approval in asked:
        assert isinstance(approval, Approval)
        if approval.id not in told:
            continue
        if approval.action == "merge_pr":
            questions.append(f"merge PR {approval.number}")
        elif approval.action == "delete_branch":
            questions.append(f"delete branch {approval.branch}")
        elif approval.post_action:
            verb = "schedule" if approval.post_action == "schedule_post" else "post"
            questions.append(f"{verb} post {approval.post}")
        else:
            questions.append(approval.text)
    approvals.turn_ended(completed=True, told=told, said="Shall I " + " or ".join(questions) + "?")
    if read_back is not None:
        approvals.turn_began("Yes.")
        assert isinstance(approvals.decide(approval_id), str)
        approvals.turn_ended(completed=True, told=[], said=read_back)
    approvals.turn_began(said)
    return approvals.decide(approval_id)


def merge_spec(pr: int = 42, repo: str = "pumpkit") -> dict[str, Any]:
    return {"action": "merge_pr", "repo": repo, "pr": pr, "sha": HEAD}


def delete_spec(branch: str, repo: str = "pumpkit") -> dict[str, Any]:
    return {"action": "delete_branch", "repo": repo, "branch": branch, "sha": TIP}


@pytest.mark.parametrize(
    ("merge", "branch", "said"),
    [
        (merge_spec(repo="jarvis"), "feat/jarvis", "Yes, Jarvis."),
        (merge_spec(), "chore/it", "Yes, do it."),
        (merge_spec(), "fix/please", "Yes please"),
        (merge_spec(), "spike/this", "Yes, this."),
        (merge_spec(), "go-ahead", "go ahead"),
        (merge_spec(), "x/the-one", "ok do it and the one"),
    ],
)
async def test_a_bare_yes_never_picks_a_branch_spelled_in_yes_or_filler_words(
    merge: dict[str, Any], branch: str, said: str
) -> None:
    result = await decided([merge, delete_spec(branch, merge["repo"])], said, "A2")

    assert str(result).startswith("Not approved: with more than one Approval in play, a bare yes does not say which.")


@pytest.mark.parametrize("said", ["yes, delete feat/old-idea", "yes, the old idea branch"])
async def test_words_that_name_a_branch_choose_its_delete(said: str) -> None:
    result = await decided([merge_spec(), delete_spec("feat/old-idea")], said, "A2")

    assert isinstance(result, Approval) and (result.id, result.state) == ("A2", "approved")


@pytest.mark.parametrize(
    ("branch", "said", "which"),
    [
        ("fix/42", "yes 42", "A1"),
        ("fix/42", "yes 42", "A2"),
        ("fix/42", "yes forty two", "A2"),
        ("42", "yes 42", "A2"),
        ("go-ahead-42", "go ahead, 42", "A2"),
    ],
)
async def test_a_number_in_a_branch_never_names_it_and_makes_the_pr_number_ambiguous(
    branch: str, said: str, which: str
) -> None:
    result = await decided([merge_spec(42), delete_spec(branch)], said, which)

    assert isinstance(result, str) and result.startswith("Not approved")


@pytest.mark.parametrize(("said", "which"), [("yes, delete fix/42", "A2"), ("yes, merge 42", "A1")])
async def test_a_verb_still_names_the_action_when_a_branch_holds_the_pr_number(said: str, which: str) -> None:
    result = await decided([merge_spec(42), delete_spec("fix/42")], said, which)

    assert isinstance(result, Approval) and result.id == which


@pytest.mark.parametrize("said", ["yes ²", "yes, merge ٤٢"])
async def test_digits_other_than_ascii_are_not_a_number_and_refuse_the_yes(said: str) -> None:
    result = await decided([merge_spec()], said, "A1")

    assert isinstance(result, str) and "which is not part of a plain yes" in result


async def test_a_ruleset_rule_that_keeps_a_branch_counts_on_any_page_of_the_rules(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    gh.rulesets["pumpkit-io/pumpkit:feat/old-idea"] = [ruleset_rule("pull_request"), ruleset_rule("deletion")]
    gh.rules_per_page = 1
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text("Not that one, sir.")]],
        [[ask_delete(), report("T1", "I may not delete it.")]],
    )

    await session.start()
    await session.say("Delete the branch")
    await session.replied("Not that one, sir.")
    await session.end()

    refused, _ = results(session.backend, "orchestrator", "report")
    assert str(refused.result).startswith("Not asked: feat/old-idea is a protected branch of pumpkit-io/pumpkit")


async def x_post_guard(spec: dict[str, Any]) -> McpGuard:
    """The connector check of Subagent S1 holding A1, the approved x_post request `spec`."""
    approvals = Approvals(org="pumpkit-io", runner=StubGh(), clock=FakeClock(), on_change=lambda: None, log=lambda e: None)
    asked = await approvals.request("T1", spec)
    assert isinstance(asked, Approval), asked
    approvals.turn_began(None)
    approvals.turn_ended(completed=True, told=["A1"], said="Shall I " + ("schedule" if spec["post_action"] == "schedule_post" else "post") + " post 812?")
    approvals.turn_began("Yes.")
    approved = approvals.decide("A1")
    assert isinstance(approved, Approval), approved
    approvals.hand_to(approved, "S1")
    return approvals.guard("A1", "S1", connector_check)


def x_post(**fields: object) -> dict[str, Any]:
    return {"action": "subagent_action", "grants": ["x_post"], "post": "812", "post_action": "schedule_post"} | fields


SCHEDULE = "mcp__claude_ai_Pumpkit__schedule_post"
COMMUNITY = "1493446837214187523"


@pytest.mark.parametrize(
    ("spec", "text"),
    [
        (x_post(post_action="post_now"), "post Pumpkit post 812 on X now"),
        (x_post(publish_at=FRIDAY_9), "schedule Pumpkit post 812 on X for Friday 6 November 2026 at 09:00 UTC+01:00"),
        (
            x_post(publish_at="2026-11-06T08:00Z", community=COMMUNITY, share_with_followers=True),
            f"schedule Pumpkit post 812 on X for Friday 6 November 2026 at 08:00 UTC, in community {COMMUNITY}, "
            "shared with followers",
        ),
    ],
)
async def test_an_x_post_request_is_said_in_the_hubs_words_from_what_it_will_publish_and_when(
    spec: dict[str, Any], text: str
) -> None:
    approvals = Approvals(org="pumpkit-io", runner=StubGh(), clock=FakeClock(), on_change=lambda: None, log=lambda e: None)

    asked = await approvals.request("T1", spec)

    assert isinstance(asked, Approval) and asked.text == text


@pytest.mark.parametrize(
    ("spec", "refusal"),
    [
        (x_post(post_action="post_now", description="schedule post 812 for Friday"), "approval.description"),
        (x_post(), "approval.publish_at"),
        (x_post(post_action="post_now", publish_at=FRIDAY_9), "approval.publish_at"),
        (x_post(publish_at="2026-11-06T09:00:00"), "approval.publish_at"),
        (x_post(publish_at="Friday at nine"), "approval.publish_at"),
        (x_post(publish_at=FRIDAY_9, share_with_followers=True), "approval.share_with_followers"),
        (x_post(publish_at=FRIDAY_9, share_with_followers="yes", community=COMMUNITY), "approval.share_with_followers"),
        (x_post(publish_at=FRIDAY_9, grants=["x_post", "posthog_config"]), "approval.grants"),
    ],
)
async def test_an_x_post_request_that_does_not_pin_exactly_what_is_published_is_refused(
    spec: dict[str, Any], refusal: str
) -> None:
    approvals = Approvals(org="pumpkit-io", runner=StubGh(), clock=FakeClock(), on_change=lambda: None, log=lambda e: None)

    asked = await approvals.request("T1", spec)

    assert isinstance(asked, Refused) and asked.reason.startswith(refusal)


@pytest.mark.parametrize(
    ("arguments", "allowed"),
    [
        ({"post_id": "812", "publish_at": FRIDAY_9, "community": COMMUNITY}, True),
        ({"post_id": "812", "publish_at": "2026-11-06T08:00:00Z", "community": COMMUNITY}, True),
        ({"post_id": "812", "publish_at": "2026-11-06T08:00:00Z", "community": COMMUNITY, "share_with_followers": False}, True),
        ({"post_id": "812", "publish_at": "2026-11-07T09:00:00+01:00", "community": COMMUNITY}, False),
        ({"post_id": "812", "publish_at": FRIDAY_9}, False),
        ({"post_id": "812", "publish_at": FRIDAY_9, "community": "999"}, False),
        ({"post_id": "812", "publish_at": FRIDAY_9, "community": COMMUNITY, "share_with_followers": True}, False),
        ({"post_id": "812", "publish_at": FRIDAY_9, "community": COMMUNITY, "parts": ["other text"]}, False),
        ({"post_id": "812", "publish_at": "soon", "community": COMMUNITY}, False),
    ],
)
async def test_a_scheduled_x_post_grant_opens_only_for_the_approved_time_and_place(
    arguments: dict[str, object], allowed: bool
) -> None:
    guard = await x_post_guard(x_post(publish_at=FRIDAY_9, community=COMMUNITY))

    verdict = guard(SCHEDULE, arguments)

    if allowed:
        assert verdict is None
    else:
        assert verdict is not None and verdict.reason.startswith("A1 covers exactly")


async def test_a_post_now_grant_refuses_a_community_it_was_not_approved_for() -> None:
    guard = await x_post_guard(x_post(post_action="post_now"))

    verdict = guard(POST_NOW, {"post_id": "812", "community": COMMUNITY})

    assert verdict is not None and verdict.reason.startswith("A1 covers exactly")


@pytest.mark.parametrize(("said", "approved"), [("Yes, schedule 812.", True), ("Yes, 9.", False), ("Yes, 2026.", False)])
async def test_only_the_post_id_names_a_scheduled_x_post_never_the_numbers_of_its_time(said: str, approved: bool) -> None:
    result = await decided([x_post(publish_at=FRIDAY_9)], said, "A1")

    assert isinstance(result, Approval) == approved, result


@pytest.mark.parametrize("pr", [9, 6, 1, 2026])
@pytest.mark.parametrize("which", ["A1", "A2"])
@pytest.mark.parametrize("said", ["yes {n}", "yes, the {n}"])
async def test_a_number_of_a_posts_time_is_ambiguous_with_a_pr_of_that_number(pr: int, which: str, said: str) -> None:
    result = await decided([x_post(publish_at=FRIDAY_9), merge_spec(pr)], said.format(n=pr), which)

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize("said", ["yes nine", "yes, the nine"])
@pytest.mark.parametrize("which", ["A1", "A2"])
async def test_a_number_word_of_a_posts_time_is_as_ambiguous_as_its_digits(said: str, which: str) -> None:
    result = await decided([x_post(publish_at=FRIDAY_9), merge_spec(9)], said, which)

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize(("said", "which"), [("yes, merge 9", "A2"), ("yes, schedule it", "A1")])
async def test_a_verb_still_chooses_when_a_posts_time_shares_a_number_with_a_pr(said: str, which: str) -> None:
    result = await decided([x_post(publish_at=FRIDAY_9), merge_spec(9)], said, which)

    assert isinstance(result, Approval) and result.id == which


async def test_a_posts_own_number_still_names_it_beside_a_pr_of_the_same_number_only_by_its_verb() -> None:
    result = await decided([x_post(publish_at=FRIDAY_9), merge_spec(812)], "yes 812", "A2")

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize(
    ("field", "value"),
    [("publish_at", "2026-11-07T09:00:00+01:00"), ("community", "999"), ("share_with_followers", True)],
)
async def test_a_saved_x_post_whose_time_or_place_was_changed_is_dropped_on_restart(field: str, value: object) -> None:
    def make() -> Approvals:
        return Approvals(org="pumpkit-io", runner=StubGh(), clock=FakeClock(), on_change=lambda: None, log=lambda e: None)

    first = make()
    await first.request("T1", x_post(publish_at=FRIDAY_9, community=COMMUNITY))
    [record] = first.dump({"T1"}, lambda t: t)

    _, dropped = make().restore(Approvals.load([record | {field: value}], lambda t: t))
    _, kept = make().restore(Approvals.load([record], lambda t: t))

    assert kept == []
    assert dropped == ["Dropped Approval A1 from the state file: it does not match the action it names."]


async def test_rules_printed_before_the_no_rulesets_error_still_keep_the_branch(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    gh.rules_result = (1, f"deletion\ngh: {NO_RULESETS} (HTTP 403)")
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [Text("Not that one, sir.")]],
        [[ask_delete(), report("T1", "I may not delete it.")]],
    )

    await session.start()
    await session.say("Delete the branch")
    await session.replied("Not that one, sir.")
    await session.end()

    refused, _ = results(session.backend, "orchestrator", "report")
    assert str(refused.result).startswith("Not asked: feat/old-idea is a protected branch of pumpkit-io/pumpkit")


def posthog(description: str) -> dict[str, Any]:
    return {"action": "subagent_action", "repo": "pumpkit", "grants": ["posthog_config"], "description": description}


DEPLOY: dict[str, Any] = {
    "action": "subagent_action", "repo": "pumpkit", "grants": [], "description": "deploy pumpkit release 42 now"
}


@pytest.mark.parametrize(
    ("specs", "said"),
    [
        ([posthog("delete the PostHog dashboard Merge Stats")], "Merge it."),
        ([posthog("delete the PostHog dashboard Post signups")], "Post it now."),
        (
            [posthog("delete the PostHog dashboard Post signups"), delete_spec("feat/jarvis", "jarvis")],
            "Yes, post it now.",
        ),
        ([posthog("set PostHog flag merge-queue rollout to 9 percent"), merge_spec(6)], "Yes, merge 9."),
        (
            [posthog("turn off the PostHog survey on release notes"), delete_spec("feat/jarvis", "jarvis")],
            "Release it.",
        ),
        ([DEPLOY], "Yes, deploy."),
        ([DEPLOY], "Yes, release 42."),
        ([DEPLOY], "Yes, the pumpkit repo."),
        ([DEPLOY], "Do it now."),
    ],
)
async def test_an_orchestrators_action_is_never_named_by_the_words_of_its_description(
    specs: list[dict[str, Any]], said: str
) -> None:
    result = await decided(specs, said, "A1", told=["A1"])

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize("said", ["Yes.", "Yes, go ahead.", "Do it, please."])
async def test_a_plain_yes_approves_an_orchestrators_action_when_it_was_the_one_thing_asked(said: str) -> None:
    specs = [posthog("set PostHog flag merge-queue rollout to 9 percent"), merge_spec(6)]

    result = await decided(specs, said, "A1", told=["A1"])

    assert isinstance(result, Approval) and result.id == "A1", result


@pytest.mark.parametrize(("which", "said"), [("A1", "Yes, schedule it."), ("A2", "Yes."), ("A2", "Yes, go ahead.")])
async def test_an_orchestrators_action_asked_beside_another_is_never_approved(which: str, said: str) -> None:
    result = await decided([x_post(post="9", publish_at=FRIDAY_9), DEPLOY], said, which)

    assert (which == "A1") == isinstance(result, Approval), result


async def test_a_yes_naming_another_pending_approval_never_approves_the_orchestrators_action() -> None:
    result = await decided([DEPLOY, merge_spec(6)], "Yes, merge 6.", "A1", ["A1"])

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize(
    "reply",
    [
        "Understood, sir. I won't merge 42 then; it stays open.",
        "Very well, sir, I will not merge 42?",
        "You mean merging 42, sir.",
        "You mean merging 42 or deleting feat/jarvis, sir?",
    ],
)
async def test_a_reply_that_is_not_one_plain_question_about_one_approval_is_no_read_back(reply: str) -> None:
    specs = [merge_spec(42), delete_spec("feat/jarvis", "jarvis")]

    result = await decided(specs, "Yes.", "A1", read_back=reply)

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize(
    ("specs", "reply"),
    [
        ([merge_spec(42), delete_spec("feat/jarvis", "jarvis")], "You mean merging 42, sir?"),
        (
            [posthog("delete the PostHog dashboard Merge Stats"), merge_spec(6)],
            "Shall I delete the PostHog dashboard Merge Stats?",
        ),
    ],
)
async def test_a_question_reading_back_one_approval_lets_a_plain_yes_approve_it(
    specs: list[dict[str, Any]], reply: str
) -> None:
    result = await decided(specs, "Yes.", "A1", read_back=reply)

    assert isinstance(result, Approval) and result.id == "A1", result


async def test_a_number_any_pending_approval_holds_is_ambiguous_even_when_that_one_was_not_asked() -> None:
    specs = [merge_spec(9), merge_spec(10), x_post(publish_at=FRIDAY_9)]

    result = await decided(specs, "yes 9", "A1", told=["A1", "A2"])

    assert isinstance(result, str) and result.startswith("Not approved"), result


MIGRATION_AND_42 = [posthog("run the db migration"), merge_spec(42)]


@pytest.mark.parametrize(
    ("which", "reply"),
    [
        ("A2", "Should I skip PR 42 for now?"),
        ("A2", "Should I drop PR 42?"),
        ("A2", "Should I revert PR 42?"),
        ("A2", "Should I abandon PR 42?"),
        ("A2", "Should I postpone PR 42?"),
        ("A2", "Should I defer PR 42?"),
        ("A2", "Should I park PR 42?"),
        ("A2", "Shall I ignore PR 42?"),
        ("A2", "Shall I leave PR 42 open?"),
        ("A2", "Shall I put PR 42 on ice?"),
        ("A2", "Shall I ping the author of PR 42?"),
        ("A2", "Want me to rebase PR 42 first?"),
        ("A2", "Shall I close PR 42 instead?"),
        ("A1", "Should I postpone: run the db migration?"),
        ("A1", "Shall I skip the db migration?"),
        ("A1", "Shall I re-check run the db migration?"),
        ("A1", "Should I review run the db migration again?"),
        ("A1", "Merge 42, or run the db migration?"),
        ("A2", "Merge 42, or run the db migration?"),
        ("A1", "Shall I merge 42 and run the db migration?"),
        ("A2", "Shall I merge 42 and run the db migration?"),
        ("A2", "Devo saltare la 42?"),
        ("A2", "Should I skip PR 42 for now? Shall I merge PR 42?"),
        ("A2", "Should I skip it... or merge 42?"),
        ("A2", "Should I skip it? Or merge 42?"),
    ],
)
async def test_a_question_with_any_word_outside_a_read_back_reads_nothing_back(which: str, reply: str) -> None:
    result = await decided(MIGRATION_AND_42, "Yes.", which, read_back=reply)

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize(
    ("specs", "reply"),
    [
        ([merge_spec(42), posthog("run the db migration")], "Shall I merge PR 42?"),
        ([delete_spec("feat/old-idea"), merge_spec(42)], "Should I go ahead and delete feat/old-idea?"),
        ([x_post(publish_at=FRIDAY_9), merge_spec(42)], "Do you want me to schedule post 812?"),
        ([posthog("run the db migration on staging"), merge_spec(42)], "Shall I run the db migration on staging?"),
        ([merge_spec(42), delete_spec("feat/old-idea")], "Procedo con il merge della 42?"),
        ([merge_spec(42), posthog("run the db migration")], "One moment, sir. Your words were unclear. Merge 42?"),
    ],
)
async def test_a_read_back_made_only_of_the_approvals_names_and_question_words_lets_a_plain_yes_approve_it(
    specs: list[dict[str, Any]], reply: str
) -> None:
    result = await decided(specs, "Yes.", "A1", read_back=reply)

    assert isinstance(result, Approval) and result.id == "A1", result


async def test_a_word_naming_another_pending_approval_it_does_not_choose_breaks_a_read_back() -> None:
    specs = [merge_spec(42), delete_spec("feat/skip"), delete_spec("fix/skip")]

    result = await decided(specs, "Yes.", "A1", read_back="Should I skip PR 42?")

    assert isinstance(result, str) and result.startswith("Not approved"), result


@pytest.mark.parametrize("reply", [
    "Would you like a status update, sir?",
    "Shall I merge PR 42? Would you like a status update, sir?",
    "The merge is ready, sir.",
])
async def test_approval_news_requires_the_talkers_actual_last_question_to_ask_about_the_action(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh, reply: str
) -> None:
    session = a_session(
        clock, output, metrics, gh,
        [[Text("Right away, sir."), delegate()], [Text(reply)], [approve(), Text("Nothing done, sir.")],
         [Text("Finished, sir.")]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )
    await session.start()
    await session.say(MERGE_42)
    await session.replied(reply)
    await session.say("Yes.")
    await session.end()
    assert gh.actions == []
    assert str(results(session.backend, "talker", "approve")[0].result).startswith("Not approved:")


@pytest.mark.parametrize(("specs", "which", "reply"), [
    ([merge_spec(), delete_spec("fix/review")], "A2", "Should I review the branch?"),
    ([merge_spec(repo="review"), merge_spec(43)], "A1", "Should I review PR 42?"),
    ([merge_spec(repo="review"), merge_spec(43)], "A1", "Should I review and merge PR 42?"),
])
async def test_target_words_cannot_supply_the_action_of_a_read_back(
    specs: list[dict[str, Any]], which: str, reply: str
) -> None:
    result = await decided(specs, "Yes.", which, read_back=reply)
    assert isinstance(result, str) and result.startswith("Not approved:"), result
