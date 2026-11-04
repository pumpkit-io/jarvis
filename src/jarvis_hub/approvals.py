"""Approvals (ADR 0004, ADR 0013): the User's recorded yes to one exact Boundary action. The Hub merges a PR or
deletes a branch itself with a fixed gh command; any other action goes to a Subagent whose brief carries the Approval
and whose connector allowlist widens by the tools that action names, for one call."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from typing import Any, Literal, Protocol, cast, get_args

from jarvis_hub.agents.backend import McpGuard, Refused
from jarvis_hub.read_only import Argv
from jarvis_hub.state import Fields, TimeShift, numbered, one_of
from jarvis_hub.subagent_rules import PUMPKIT_SERVERS
from jarvis_hub.tasks import ago
from jarvis_hub.waits import Runner

ApprovalAction = Literal["merge_pr", "delete_branch", "subagent_action"]
# pending: asked, no answer yet. approved: the User said yes; the Hub is running it, or a Subagent is still to take it.
# granted: a Subagent holds it. used, lapsed, expired, withdrawn: spent, never live again.
ApprovalState = Literal["pending", "approved", "granted", "used", "lapsed", "expired", "withdrawn"]
LIVE: frozenset[ApprovalState] = frozenset({"pending", "approved", "granted"})
HUB_ACTIONS = ("merge_pr", "delete_branch")

EXPIRES_AFTER = 24 * 3600.0

# The connector tools an Approval for a Subagent action may open, by grant: the only widening of ADR 0008's list.
GRANTS: dict[str, tuple[str, ...]] = {
    "x_post": tuple(f"mcp__{server}__{tool}" for server in PUMPKIT_SERVERS for tool in ("post_now", "schedule_post")),
    "posthog_config": ("mcp__posthog__exec",),
}

# Answers that say yes without saying to what. With more than one Approval in play they choose nothing.
_BARE = frozenset(
    "yes yeah yep yup ya sure ok okay fine right alright all go ahead do it please sir jarvis approve approved "
    "confirm confirmed absolutely of course correct that that's thats one this sounds good great perfect proceed "
    "sì si certo va bene vai pure fallo procedi d'accordo daccordo signore grazie esatto".split()
)
_WORD = re.compile(r"[\w'’]+")

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"(?:(?P<owner>[A-Za-z0-9-]+)/)?(?P<name>[A-Za-z0-9._-]+)")
# What git accepts as a branch name, narrowed: no leading dash or slash, no `..`, `//`, `@{`, `.lock` or trailing `/`.
_BRANCH = re.compile(r"(?!-|/)(?!.*(?:\.\.|//|@\{|\.lock$|/$|\.$))[A-Za-z0-9._/+-]{1,200}")

# One read for a branch: the repository's node id, which the delete needs, and the branch tip, or "none".
REF_QUERY = (
    "query($owner:String!,$name:String!,$ref:String!)"
    "{repository(owner:$owner,name:$name){id ref(qualifiedName:$ref){target{oid}}}}"
)
REF_JQ = '.data.repository.id + " " + (.data.repository.ref.target.oid // "none")'
# GitHub deletes the ref only while it still points at beforeOid, so the tip the User approved is enforced server side.
DELETE_REF = (
    "mutation($repositoryId:ID!,$name:GitRefname!,$beforeOid:GitObjectID!)"
    "{updateRefs(input:{repositoryId:$repositoryId,refUpdates:[{name:$name,beforeOid:$beforeOid,"
    'afterOid:"0000000000000000000000000000000000000000",force:true}]}){clientMutationId}}'
)


@dataclass
class Approval:
    id: str
    task_id: str
    action: ApprovalAction
    # `owner/name`; empty for a Subagent action that names no repo.
    repo: str
    created: float
    # The exact action, written by the Hub for its own actions and by the Orchestrator for a Subagent's.
    text: str
    number: int | None = None
    branch: str | None = None
    # The state the Approval covers: the PR head for a merge, the branch tip for a delete.
    sha: str | None = None
    grants: list[str] = field(default_factory=lambda: [])
    state: ApprovalState = "pending"
    # The User's message that approved it, verbatim from the Hub's own record of the turn.
    words: str | None = None
    approved: float | None = None
    subagent: str | None = None
    outcome: str = ""

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(tool for grant in self.grants for tool in GRANTS.get(grant, ()))

    def line(self, now: float) -> str:
        return f"{self.id} {self.state}, asked {ago(now - self.created)} ago: {self.text}"


_SAVED = Fields(
    (
        "id",
        "task_id",
        "action",
        "repo",
        "created",
        "text",
        "number",
        "branch",
        "sha",
        "grants",
        "state",
        "words",
        "approved",
        "subagent",
        "outcome",
    ),
    times=frozenset({"created", "approved"}),
)


@dataclass(frozen=True)
class _Target:
    """What the target is now: None when it still matches, else what changed; and the repository's node id."""

    change: str | None
    node: str = ""


def approval_event(approval: Approval, status: str, text: str) -> str:
    return (
        f'<event source="hub" task="{approval.task_id}" approval="{approval.id}" status="{status}">'
        f"{escape(text, quote=False)}</event>"
    )


def is_bare(words: str) -> bool:
    """A yes that names nothing: every word is agreement, politeness or a pointer."""
    found = _WORD.findall(words.casefold())
    return all(w.replace("’", "'") in _BARE for w in found)


class ApprovalClock(Protocol):
    def now(self) -> float: ...


class Approvals:
    """Every Approval this launch, and the open Tasks' from before a restart. `on_change` hears of every change of
    state, so the Hub saves it; `log` gets one entry per action the Hub runs."""

    def __init__(
        self,
        *,
        org: str,
        runner: Runner,
        clock: ApprovalClock,
        on_change: Callable[[], None],
        log: Callable[[dict[str, Any]], None],
    ) -> None:
        self._org = org
        self._runner = runner
        self._clock = clock
        self._on_change = on_change
        self._log = log
        self._all: dict[str, Approval] = {}
        self._ids = 0
        self._running: set[str] = set()

    async def request(self, task_id: str, args: object) -> Approval | Refused:
        """A pending Approval for the action `args` names, once the Hub has read that its target is as stated. One
        still pending on the same Task is withdrawn: the newest request is the one the User hears."""
        if not isinstance(args, dict):
            return Refused("approval is an object: {action, repo, pr, branch, sha, description, grants}.")
        spec = cast(dict[str, Any], args)
        action = str(spec.get("action") or "")
        if action not in get_args(ApprovalAction):
            return Refused(f"approval.action is one of {', '.join(get_args(ApprovalAction))}.")
        approval = self._shaped(task_id, cast(ApprovalAction, action), spec)
        if isinstance(approval, Refused):
            return approval
        if approval.action in HUB_ACTIONS:
            target = await self._read(approval)
            if target.change is not None:
                return Refused(f"Not asked: {target.change}. Look again and ask about what is there now.")
        self._ids += 1
        approval.id = f"A{self._ids}"
        for older in self._all.values():
            if older.task_id == task_id and older.state == "pending":
                older.state, older.outcome = "withdrawn", f"Replaced by {approval.id}."
        self._all[approval.id] = approval
        self._on_change()
        return approval

    def _shaped(self, task_id: str, action: ApprovalAction, spec: dict[str, Any]) -> Approval | Refused:
        now = self._clock.now()
        if action == "subagent_action":
            description = " ".join(str(spec.get("description") or "").split())
            if not description:
                return Refused("approval.description is required: the one exact action, as the User will hear it.")
            raw: object = spec.get("grants") or []
            grants = [str(g) for g in cast(list[object], raw)] if isinstance(raw, list) else None
            if grants is None or any(g not in GRANTS for g in grants):
                return Refused(f"approval.grants lists any of {', '.join(GRANTS)}, or nothing.")
            repo = self._repo(spec.get("repo")) if spec.get("repo") else ""
            if isinstance(repo, Refused):
                return repo
            return Approval("", task_id, action, repo, now, description, grants=grants)
        repo = self._repo(spec.get("repo"))
        if isinstance(repo, Refused):
            return repo
        sha = str(spec.get("sha") or "").strip().lower()
        if not _SHA.fullmatch(sha):
            return Refused("approval.sha is the full 40-character commit the User is asked about.")
        if action == "merge_pr":
            try:
                number = int(spec.get("pr") or 0)
            except (TypeError, ValueError):
                number = 0
            if number < 1:
                return Refused("approval.pr is the PR number, such as 42.")
            text = f"merge {repo}#{number} at commit {sha[:12]}"
            return Approval("", task_id, action, repo, now, text, number=number, sha=sha)
        branch = str(spec.get("branch") or "").strip().removeprefix("refs/heads/")
        if not _BRANCH.fullmatch(branch):
            return Refused("approval.branch is a branch name, such as feat/old-idea.")
        text = f"delete branch {branch} of {repo} at commit {sha[:12]}"
        return Approval("", task_id, action, repo, now, text, branch=branch, sha=sha)

    def _repo(self, value: object) -> str | Refused:
        match = _REPO.fullmatch(str(value or "").strip())
        if match is None or match["name"] in (".", ".."):
            return Refused("approval.repo is an Org repo, such as pumpkit.")
        if match["owner"] and match["owner"].lower() != self._org.lower():
            return Refused(f"{value} is outside the Org {self._org}.")
        return f"{self._org}/{match['name']}"

    def get(self, approval_id: object) -> Approval | None:
        self._expire()
        return self._all.get(str(approval_id).strip().upper()) if approval_id else None

    def pending(self) -> list[Approval]:
        self._expire()
        return [a for a in self._all.values() if a.state == "pending"]

    def listing(self) -> str:
        pending = self.pending()
        if not pending:
            return "No Approval is pending."
        now = self._clock.now()
        named = "; ".join(f"{a.id}: {a.text} (on {a.task_id}, asked {ago(now - a.created)} ago)" for a in pending)
        return f"Pending Approvals: {named}. Name each to the User and ask which one they mean, in one question."

    def decide(self, approval_id: object, *, words: str | None, last_said: tuple[str, ...]) -> Approval | str:
        """Approves when the yes is unambiguous, else says why not with the pending ones listed. `words` is the
        User's message this turn answers (None when the turn answers no User message); `last_said` the Approvals
        Jarvis asked about in the last thing it said."""
        listing = self.listing()
        if words is None:
            return f"Not approved: approve works only in your reply to the User's own message. {listing}"
        approval = self.get(approval_id)
        if approval is None or approval.state != "pending":
            return f"Not approved: {approval_id} is not pending. {listing}"
        if approval.id not in last_said:
            return f"Not approved: {approval.id} was not the last thing you asked the User about. {listing}"
        if (len(self.pending()) > 1 or len(last_said) > 1) and is_bare(words):
            return f"Not approved: with more than one Approval in play, a bare yes does not say which. {listing}"
        approval.state, approval.words, approval.approved = "approved", words, self._clock.now()
        self._on_change()
        return approval

    async def carry_out(self, approval: Approval) -> str:
        """Runs an approved merge or branch delete after reading its target again; returns the Orchestrator's
        event. A changed target lapses the Approval, and the event says what changed."""
        self._running.add(approval.id)
        try:
            if self._expired(approval):
                approval.state, approval.outcome = "expired", "It expired before it ran."
                return approval_event(approval, "expired", f"{approval.text}: {approval.outcome} Ask again.")
            target = await self._read(approval)
            if target.change is not None:
                approval.state, approval.outcome = "lapsed", target.change
                text = (
                    f"Not done: the User approved {approval.text}, but {target.change}, so the yes no longer covers "
                    "it. Look at what changed, then ask the User again in a fresh request that says what changed."
                )
                return approval_event(approval, "lapsed", text)
            argv = self._action_argv(approval, target.node)
            code, printed = await self._runner(argv)
            approval.state = "used"
            approval.outcome = "Done." if code == 0 else f"gh failed: {printed or f'exit {code}'}"
            self._log(
                {"kind": "approval", "approval": approval.id, "task": approval.task_id, "argv": list(argv)}
                | {"exit": code, "output": printed, "words": approval.words}
            )
            if code == 0:
                return approval_event(approval, "done", f"Done: {approval.text}. {printed}".strip())
            text = f"Not done: {approval.text} failed, and the Approval is spent. {approval.outcome}"
            return approval_event(approval, "failed", text)
        finally:
            self._running.discard(approval.id)
            self._on_change()

    def check_argv(self, approval: Approval) -> Argv:
        """The read-only gh command that says what the target is now."""
        if approval.action == "merge_pr":
            return (
                "gh", "pr", "view", str(approval.number), "-R", approval.repo,
                "--json", "headRefOid,state", "--jq", '.state + " " + .headRefOid',
            )
        owner, name = approval.repo.split("/", 1)
        return (
            "gh", "api", "graphql", "-f", f"query={REF_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
            "-f", f"ref=refs/heads/{approval.branch}", "--jq", REF_JQ,
        )

    def _action_argv(self, approval: Approval, node: str) -> Argv:
        if approval.action == "merge_pr":
            # The Org's repos merge with a merge commit; GitHub refuses if the head is no longer this commit.
            return (
                "gh", "pr", "merge", str(approval.number), "-R", approval.repo,
                "--merge", "--match-head-commit", str(approval.sha),
            )
        return (
            "gh", "api", "graphql", "-f", f"query={DELETE_REF}", "-f", f"repositoryId={node}",
            "-f", f"name=refs/heads/{approval.branch}", "-f", f"beforeOid={approval.sha}",
        )

    async def _read(self, approval: Approval) -> _Target:
        code, printed = await self._runner(self.check_argv(approval))
        words = printed.strip().splitlines()[-1].split() if printed.strip() else []
        what = f"{approval.repo}#{approval.number}" if approval.action == "merge_pr" else f"branch {approval.branch}"
        if code != 0 or len(words) != 2:
            return _Target(f"the Hub could not read {what}: {printed.strip() or f'gh exited {code}'}")
        first, second = words
        sha = str(approval.sha)
        if approval.action == "merge_pr":
            if first != "OPEN":
                return _Target(f"{what} is {first.lower()} now")
            if second.lower() != sha:
                return _Target(f"the head of {what} is now {second[:12]}, not {sha[:12]}")
            return _Target(None)
        if second == "none":
            return _Target(f"{what} no longer exists in {approval.repo}")
        if second.lower() != sha:
            return _Target(f"{what} now points at {second[:12]}, not {sha[:12]}")
        return _Target(None, node=first)

    def grant(self, approval_id: object, task_id: str) -> Approval | str:
        """The approved Subagent action `approval_id` names, if a Subagent of `task_id` may take it."""
        approval = self.get(approval_id)
        if approval is None or approval.task_id != task_id:
            return f"There is no Approval {approval_id} on {task_id}."
        if approval.action != "subagent_action":
            return f"{approval.id} is for the Hub to carry out, never a Subagent."
        if approval.state != "approved":
            return f"{approval.id} is {approval.state}, so no Subagent may act on it."
        return approval

    def hand_to(self, approval: Approval, subagent_id: str) -> None:
        approval.state, approval.subagent = "granted", subagent_id
        self._on_change()

    def guard(self, approval_id: str, subagent_id: str, base: McpGuard) -> McpGuard:
        """`base`, widened by the tools of the Approval the Subagent holds, for one call while it is unexpired."""

        def check(tool_name: str) -> Refused | None:
            verdict = base(tool_name)
            if verdict is None:
                return None
            approval = self.get(approval_id)
            if approval is None or approval.subagent != subagent_id or tool_name not in approval.tools:
                return verdict
            if approval.state != "granted":
                return Refused(f"{approval.id} is {approval.state}, so {tool_name} needs a fresh Approval.")
            approval.state, approval.outcome = "used", f"{subagent_id} called {tool_name}."
            self._on_change()
            return None

        return check

    def subagent_ended(self, subagent_id: str) -> None:
        """The Approval a Subagent held is spent once it ends, whatever it did with it."""
        for approval in self._all.values():
            if approval.subagent == subagent_id and approval.state == "granted":
                approval.state, approval.outcome = "used", f"{subagent_id} ended."
                self._on_change()

    def withdraw(self, task_id: str) -> None:
        """The Task was cancelled: none of its Approvals may be acted on, except one the Hub is already running."""
        for approval in self._all.values():
            if approval.task_id == task_id and approval.state in LIVE and approval.id not in self._running:
                approval.state, approval.outcome = "withdrawn", "Its Task was cancelled."
        self._on_change()

    def lines(self, task_id: str) -> list[str]:
        now = self._clock.now()
        return [a.line(now) for a in self.live() if a.task_id == task_id]

    def live(self) -> list[Approval]:
        self._expire()
        return [a for a in self._all.values() if a.state in LIVE]

    def _expired(self, approval: Approval) -> bool:
        return self._clock.now() - approval.created >= EXPIRES_AFTER

    def _expire(self) -> None:
        changed = False
        for approval in self._all.values():
            if approval.state in LIVE and approval.id not in self._running and self._expired(approval):
                approval.state, approval.outcome = "expired", "24 hours passed."
                changed = True
        if changed:
            self._on_change()

    def dump(self, task_ids: set[str], to_wall: TimeShift) -> list[dict[str, Any]]:
        """The live Approvals of these Tasks, for the state file."""
        return [_SAVED.dump(a, to_wall) for a in self._all.values() if a.state in LIVE and a.task_id in task_ids]

    @staticmethod
    def load(records: list[dict[str, Any]], from_wall: TimeShift) -> list[Approval]:
        approvals: list[Approval] = []
        for r in records:
            approval = Approval(**_SAVED.load(r, from_wall))
            numbered(approval.id, "A")
            one_of(approval.action, get_args(ApprovalAction))
            one_of(approval.state, get_args(ApprovalState))
            if approval.sha is not None and not _SHA.fullmatch(approval.sha):
                raise ValueError(f"{approval.id} has no commit it covers")
            if not isinstance(cast(object, approval.grants), list) or any(g not in GRANTS for g in approval.grants):
                raise ValueError(f"{approval.id} has an unknown grant")
            approvals.append(approval)
        return approvals

    def restore(self, approvals: list[Approval]) -> list[Approval]:
        """Takes back loaded Approvals; returns the approved Hub actions the old Hub may not have finished, to run
        again: the target is read anew first, so one already done lapses rather than runs twice."""
        for approval in approvals:
            self._all[approval.id] = approval
            self._ids = max(self._ids, numbered(approval.id, "A"))
        return [a for a in approvals if a.state == "approved" and a.action in HUB_ACTIONS]
