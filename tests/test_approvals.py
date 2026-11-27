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


def present(approval_id: str = "A1") -> Tool:
    return Tool("present_approval", {"approval_id": approval_id})


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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Let me see, sir."), Tool("status")]],
        [[ask_merge()]],
    )

    await asked_to_merge(session)
    await session.say("What's pending?")
    await session.end()

    [asked] = results(session.backend, "orchestrator", "report")
    assert asked.result == f"Reported to the User, asking for Approval A1: merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]}."
    assert received(session.backend, "talker")[2] == (
        f'<event source="orchestrator" task="T1" needs_user="true" approval="A1" details="on screen">{ASK}'
        f" Call present_approval for this event's Approval, then ask about this action naturally: "
        f"merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]}</event>"
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
            [present(), Text(ASKED)],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging now, sir."), approve()]],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Very well, sir."), forged]],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED), approve()]],
        [[ask_merge()]],
    )

    await asked_to_merge(session)
    await session.end()

    [refused] = results(session.backend, "talker", "approve")
    assert refused.result == "Not approved: approve works only in your reply to the User's own message."
    assert gh.actions == []


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
            [present(), Text(ASKED)],
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
    assert str(twice.result).startswith("Not approved: A1 is not pending.")
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging, sir."), approve()]],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging, sir."), approve()]],
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
            [present(), Text(ASKED)],
            [Text("Merging, sir."), approve()],
            [present("A2"), Text("A new commit landed on 42, sir. Merge it?")],
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
        f"not {HEAD[:12]}, so the yes no longer covers it. Look at what changed and request a fresh concrete Approval, "
        "using applicable Authorization before asking the User.</event>"
    )
    assert received(session.backend, "talker")[-1] == (
        f'<event source="orchestrator" task="T1" needs_user="true" approval="A2" details="on screen">{fresh}'
        f" Call present_approval for this event's Approval, then ask about this action naturally: "
        f"merge pumpkit-io/pumpkit#42 at commit {PUSHED[:12]}</event>"
    )


async def test_a_pr_closed_after_the_yes_is_not_merged(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, gh: StubGh
) -> None:
    session = a_session(
        clock,
        output,
        metrics,
        gh,
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging, sir."), approve()]],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging, sir."), approve()]],
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
            [present(), Text("Delete old-idea, sir?")],
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
            [present(), Text("Delete old-idea, sir?")],
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


PUMPKIT_42 = "pumpkit-io/pumpkit#42"
REVIEW_HINT = "`gh workflow run sdlc-review-llm.yml --ref feat/pr-42 -R pumpkit-io/pumpkit`"


def blocked_on_review(gh: StubGh) -> None:
    """pumpkit's PR 42 as GitHub showed pumpkit#579: the ruleset requires `review`, which nobody started."""
    gh.merge_states[PUMPKIT_42] = "BLOCKED"
    gh.checks[PUMPKIT_42] = {"ci": "SUCCESS"}
    gh.rulesets["pumpkit-io/pumpkit:main"] = [ruleset_rule("required_status_checks", ("ci", "review"))]


def merge_approvals(clock: FakeClock, gh: StubGh) -> Approvals:
    return Approvals(org="pumpkit-io", runner=gh, clock=clock, on_change=lambda: None, log=lambda entry: None)


async def ask_to_merge(approvals: Approvals) -> Approval | Refused:
    return await approvals.request("T1", {"action": "merge_pr", "repo": "pumpkit", "pr": 42, "sha": HEAD})


async def test_a_merge_the_rules_hold_back_for_a_check_nobody_started_is_not_asked(
    clock: FakeClock, gh: StubGh
) -> None:
    blocked_on_review(gh)
    approvals = merge_approvals(clock, gh)

    asked = await ask_to_merge(approvals)

    assert isinstance(asked, Refused)
    assert asked.reason.startswith("Not asked: pumpkit-io/pumpkit#42 cannot merge yet: the required check `review` has not run")
    assert REVIEW_HINT in asked.reason
    assert "subagent_action Approval" in asked.reason
    assert "`ci`" not in asked.reason
    assert approvals.pending() == []


@pytest.mark.parametrize(
    ("result", "said"),
    [("FAILURE", "is failure"), ("IN_PROGRESS", "is in progress"), ("QUEUED", "is queued")],
)
async def test_a_merge_held_back_by_a_required_check_that_ran_names_how_it_stands(
    clock: FakeClock, gh: StubGh, result: str, said: str
) -> None:
    blocked_on_review(gh)
    gh.checks[PUMPKIT_42]["review"] = result

    asked = await ask_to_merge(merge_approvals(clock, gh))

    assert isinstance(asked, Refused)
    assert f"the required check `review` {said}" in asked.reason
    assert "has not run" not in asked.reason


async def test_a_required_check_other_than_review_is_not_given_the_review_workflow(
    clock: FakeClock, gh: StubGh
) -> None:
    blocked_on_review(gh)
    gh.rulesets["pumpkit-io/pumpkit:main"] = [ruleset_rule("required_status_checks", ("ci", "deploy preview"))]

    asked = await ask_to_merge(merge_approvals(clock, gh))

    assert isinstance(asked, Refused)
    assert "the required check `deploy preview` has not run; wait for CI, or start its workflow" in asked.reason
    assert "sdlc-review-llm.yml" not in asked.reason


async def test_a_merge_blocked_for_a_reason_the_hub_cannot_name_says_so(clock: FakeClock, gh: StubGh) -> None:
    gh.merge_states[PUMPKIT_42] = "BLOCKED"
    gh.checks[PUMPKIT_42] = {"ci": "SUCCESS"}
    gh.rulesets["pumpkit-io/pumpkit:main"] = [ruleset_rule("pull_request")]

    asked = await ask_to_merge(merge_approvals(clock, gh))

    assert isinstance(asked, Refused)
    assert asked.reason.startswith("Not asked: pumpkit-io/pumpkit#42 cannot merge yet: GitHub holds it back")


async def test_a_merge_blocked_on_rules_the_hub_cannot_read_still_is_not_asked(clock: FakeClock, gh: StubGh) -> None:
    gh.merge_states[PUMPKIT_42] = "BLOCKED"
    gh.rules_result = (1, NO_RULESETS)

    asked = await ask_to_merge(merge_approvals(clock, gh))

    assert isinstance(asked, Refused)
    assert "cannot merge yet: GitHub holds it back" in asked.reason


@pytest.mark.parametrize(
    ("merge_state", "said"),
    [
        ("BEHIND", "its branch is behind main; update it first"),
        ("DIRTY", "it conflicts with main; resolve the conflicts first"),
        ("DRAFT", "it is a draft; mark it ready first"),
        ("UNKNOWN", "GitHub has not finished working out whether it can merge"),
    ],
)
async def test_a_merge_state_github_would_refuse_is_not_asked(
    clock: FakeClock, gh: StubGh, merge_state: str, said: str
) -> None:
    gh.merge_states[PUMPKIT_42] = merge_state

    asked = await ask_to_merge(merge_approvals(clock, gh))

    assert isinstance(asked, Refused)
    assert asked.reason.startswith(f"Not asked: pumpkit-io/pumpkit#42 cannot merge yet: {said}")


@pytest.mark.parametrize("merge_state", ["CLEAN", "UNSTABLE", "HAS_HOOKS"])
async def test_a_merge_github_allows_is_asked(clock: FakeClock, gh: StubGh, merge_state: str) -> None:
    gh.merge_states[PUMPKIT_42] = merge_state

    asked = await ask_to_merge(merge_approvals(clock, gh))

    assert isinstance(asked, Approval)


async def test_a_merge_is_asked_once_the_review_has_run(clock: FakeClock, gh: StubGh) -> None:
    blocked_on_review(gh)
    approvals = merge_approvals(clock, gh)
    assert isinstance(await ask_to_merge(approvals), Refused)

    gh.merge_states[PUMPKIT_42] = "CLEAN"
    gh.checks[PUMPKIT_42]["review"] = "SUCCESS"

    assert isinstance(await ask_to_merge(approvals), Approval)


async def test_a_yes_to_a_merge_that_became_blocked_runs_nothing_and_says_what_is_missing(
    clock: FakeClock, gh: StubGh
) -> None:
    approvals = merge_approvals(clock, gh)
    asked = await ask_to_merge(approvals)
    assert isinstance(asked, Approval)
    approvals.approve(asked, "Yes, merge 42.")
    blocked_on_review(gh)

    event = await approvals.carry_out(asked)

    assert gh.actions == []
    assert asked.state == "lapsed"
    assert 'status="lapsed"' in event
    assert "the required check `review` has not run" in event
    assert "Nothing ran." in event


async def test_the_merge_reads_ask_gh_only_to_read(clock: FakeClock, gh: StubGh) -> None:
    blocked_on_review(gh)

    await ask_to_merge(merge_approvals(clock, gh))

    assert [argv[:3] for argv in gh.reads] == [("gh", "pr", "view"), ("gh", "api", "--paginate"), ("gh", "pr", "view")]
    assert gh.actions == []


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
            [present(), Text("The thread is ready, sir. Post it now?")],
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
        [[Text("Right away, sir."), delegate("Post the launch thread")], [present(), Text("Post it, sir?")]],
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
        [[Text("Right away, sir."), delegate("Post the launch thread")], [present(), Text("Post it, sir?")]],
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
            [present(), Text("Post it, sir?")],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)]],
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
            [present(), Text(ASKED)],
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
            [present(), Text(ASKED)],
            [Text("Noted, sir."), delegate_to("Check 42 again", "T1")],
            [present("A2"), Text("Shall I merge 42, sir?")],
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

    def listens(self, client: str) -> bool:
        return True

    def owns(self, owner: tuple[str, int]) -> bool:
        return True

    def typed_input(self, *, client: str | None = None) -> None:
        pass

    def semantic_context(self, owner: tuple[str, int], start: int) -> None:
        pass

    def announce(self, client: str = "terminal") -> bool:
        return True

    def text_reply(self, owner: tuple[str, int] | None, *, client: str = "terminal") -> None:
        pass


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
            [present(), Text(ASKED)],
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
        f'<event source="orchestrator" task="T1" needs_user="true" approval="A1" details="on screen">{ASK}'
        f" Call present_approval for this event's Approval, then ask about this action naturally: "
        f"merge pumpkit-io/pumpkit#42 at commit {HEAD[:12]}</event>"
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)]],
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
            [Text("One moment, sir."), approve(), present(), Text(" You mean merging pumpkit 42, sir?")],
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
    assert str(refused.result).startswith("Not approved: A1 has not been presented in a completed reply.")
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging, sir."), approve()]],
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
            [present(), Text("Post it, sir?")],
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
            [present(), Text("Delete old-idea, sir?")],
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
        [[Text("Right away, sir."), delegate("Delete feat/old-idea in pumpkit")], [present(), Text("Delete old-idea, sir?")]],
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
            [present(), Text("Post it, sir?")],
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
        [[Text("Right away, sir."), delegate()], [present(), Text(ASKED)], [Text("Merging, sir."), approve()]],
        [[ask_merge()], [report("T1", "Merged.")]],
    )


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
            [present(), Text("Delete old-idea, sir?")],
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
            [present(), Text("Delete old-idea, sir?")],
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
            [present(), Text("Delete old-idea, sir?")],
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
        [[Text("Right away, sir."), delegate("Post the launch thread")], [present(), Text("Shall I " + ("schedule" if asking.input["approval"]["post_action"] == "schedule_post" else "post") + " it, sir?")], [approve()]],
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
    approvals.present("A1")
    approvals.turn_ended(completed=True)
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
