"""Concrete, single-use actions. The Talker interprets permission; the Hub owns provenance and execution."""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Literal, Protocol, cast, get_args

from jarvis_hub.agents.backend import McpGuard, Refused
from jarvis_hub.read_only import Argv
from jarvis_hub.state import Fields, TimeShift, numbered, one_of
from jarvis_hub.subagent_rules import PUMPKIT_SERVERS
from jarvis_hub.tasks import ago
from jarvis_hub.waits import Runner

ApprovalAction = Literal["merge_pr", "delete_branch", "subagent_action"]
# pending: asked. approved: the User said yes, and the Hub runs it or a Subagent is to take it. granted: a Subagent
# holds it. used, lapsed, expired, withdrawn: spent.
ApprovalState = Literal["pending", "approved", "granted", "used", "lapsed", "expired", "withdrawn"]
LIVE: frozenset[ApprovalState] = frozenset({"pending", "approved", "granted"})
HUB_ACTIONS = ("merge_pr", "delete_branch")

EXPIRES_AFTER = 24 * 3600.0

PostAction = Literal["post_now", "schedule_post"]
# The connector tools an Approval for a Subagent action may open, by grant: the only widening of ADR 0008's list.
# x_post opens only the one of these its Approval's post_action names.
GRANTS: dict[str, tuple[str, ...]] = {
    "x_post": tuple(f"mcp__{server}__{tool}" for server in PUMPKIT_SERVERS for tool in get_args(PostAction)),
    "posthog_config": ("mcp__posthog__exec",),
}

_POST = re.compile(r"[A-Za-z0-9_-]{1,64}")
_COMMUNITY = re.compile(r"[A-Za-z0-9_:/.?=&%-]{1,200}")
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"(?:(?P<owner>[A-Za-z0-9-]+)/)?(?P<name>[A-Za-z0-9._-]+)")
# What git accepts as a branch name, narrowed: no leading dash or slash, no `..`, `//`, `@{`, `.lock` or trailing `/`.
_BRANCH = re.compile(r"(?!-|/)(?!.*(?:\.\.|//|@\{|\.lock$|/$|\.$))[A-Za-z0-9._/+-]{1,200}")

# One read for a branch: the repository's node id, which the delete needs, the branch tip or "none", the default
# branch, and whether a classic protection rule covers it. Both rule fields are null under a ruleset.
REF_QUERY = (
    "query($owner:String!,$name:String!,$ref:String!)"
    "{repository(owner:$owner,name:$name){id defaultBranchRef{name} ref(qualifiedName:$ref){target{oid} "
    "branchProtectionRule{id} refUpdateRule{allowsDeletions}}}}"
)
REF_JQ = (
    '.data.repository as $r | $r.id + " " + ($r.ref.target.oid // "none") + " " + ($r.defaultBranchRef.name // "none")'
    ' + " " + (if $r.ref.branchProtectionRule or $r.ref.refUpdateRule then "protected" else "unprotected" end)'
)
# The ruleset rules that keep a branch: deletion forbids it outright, and a delete is the last non-fast-forward
# update. Other rules (reviews, checks, signatures) govern changes to a branch, not whether it exists.
KEEPING_RULES = frozenset({"deletion", "non_fast_forward", "update"})
# GitHub's 403 for the rules of a private repo on GitHub Free, where no ruleset is enforced.
_NO_RULESETS = "Upgrade to GitHub Pro or make this repository public"
# What a merge read prints: the state, the head, GitHub's merge state, the base branch and the head branch.
MERGE_JQ = (
    '.state + " " + .headRefOid + " " + (.mergeStateStatus // "UNKNOWN") + " " + .baseRefName + " " + .headRefName'
)
# The merge states GitHub merges from: CLEAN, UNSTABLE (only checks the rules do not require are red) and HAS_HOOKS.
MERGEABLE = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})
# A check that ended in one of these does not hold a merge back.
PASSED = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
# The reviews of pumpkit's `review` check start with this workflow, which only a repository admin can run.
REVIEW_WORKFLOW = "sdlc-review-llm.yml"


def rules_argv(repo: str, branch: str) -> Argv:
    """The read-only gh command that lists the types of the active ruleset rules on `branch` of `owner/name`, one
    line per page of rules."""
    return (
        "gh", "api", "--paginate", f"repos/{repo}/rules/branches/{branch}", "--jq", '[.[].type] | unique | join(" ")'
    )


def checks_argv(repo: str, number: int) -> Argv:
    """The read-only gh command that lists the PR's checks as `name<TAB>result`, one per line; a check still going
    shows its status."""
    jq = (
        '.statusCheckRollup[] | [(.name // .context), (if (.conclusion // "") != "" then .conclusion '
        'else (.state // .status // "UNKNOWN") end)] | @tsv'
    )
    return ("gh", "pr", "view", str(number), "-R", repo, "--json", "statusCheckRollup", "--jq", jq)


def required_checks_argv(repo: str, branch: str) -> Argv:
    """The read-only gh command that lists the status checks the rules on `branch` require, one per line."""
    jq = '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context'
    return ("gh", "api", "--paginate", f"repos/{repo}/rules/branches/{branch}", "--jq", jq)


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
    # The Pumpkit post an `x_post` grant may publish: the post tools refuse any other post_id.
    post: str | None = None
    # Whether an `x_post` grant posts now or schedules.
    post_action: PostAction | None = None
    # When a scheduled post goes out, ISO 8601 with its UTC offset.
    publish_at: str | None = None
    # The X community an `x_post` goes to instead of the profile.
    community: str | None = None
    share_with_followers: bool = False
    state: ApprovalState = "pending"
    # The User's message that approved it, verbatim from the Hub's own record of the turn.
    words: str | None = None
    approved: float | None = None
    subagent: str | None = None
    outcome: str = ""
    task_revision: int = 0
    authorization_id: str | None = None
    question: str | None = None

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(
            tool
            for grant in self.grants
            for tool in GRANTS.get(grant, ())
            if grant != "x_post" or tool.endswith(f"__{self.post_action}")
        )

    @property
    def post_arguments(self) -> dict[str, object]:
        """The exact arguments an `x_post` grant's tool must be called with."""
        arguments: dict[str, object] = {"post_id": self.post}
        if self.publish_at:
            arguments["publish_at"] = self.publish_at
        if self.community:
            arguments["community"] = self.community
        if self.share_with_followers:
            arguments["share_with_followers"] = True
        return arguments

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
        "post",
        "post_action",
        "publish_at",
        "community",
        "share_with_followers",
        "state",
        "words",
        "approved",
        "subagent",
        "outcome",
        "task_revision",
        "authorization_id",
        "question",
    ),
    times=frozenset({"created", "approved"}),
)


@dataclass(frozen=True)
class _Target:
    """What the target is now: None when it still matches, else what changed; and the repository's node id."""

    change: str | None
    node: str = ""
    # The change is a branch the Hub never deletes, whatever its tip.
    forbidden: bool = False
    # The change is that GitHub would refuse the merge now, whatever the User says: it holds what is missing.
    blocked: bool = False


def _how_to_start(check: str, repo: str, head: str) -> str:
    """How to start a required check nobody triggered, when the Hub knows it: pumpkit's `review`, which starts only
    by hand and spends the maintainer's usage, so starting it needs the User's yes."""
    if check != "review":
        return "; wait for CI, or start its workflow if it runs by hand"
    return (
        f"; it starts only by hand, with `gh workflow run {REVIEW_WORKFLOW} --ref {head} -R {repo}`, which spends the "
        "User's usage and needs an admin, so request a subagent_action Approval to start it, then wait for "
        "its verdict before asking to merge"
    )


def _x_post(task_id: str, spec: dict[str, Any], grants: list[str], repo: str, now: float) -> Approval | Refused:
    """An `x_post` Approval, said in the Hub's words from what it publishes, where and when, so the User hears
    exactly what the grant allows."""
    if grants != ["x_post"]:
        return Refused("approval.grants: x_post is granted alone, in an Approval of its own.")
    if spec.get("description"):
        return Refused(
            "approval.description is left out with x_post: the Hub says the action from post, post_action, "
            "publish_at and community."
        )
    post = str(spec.get("post") or "").strip()
    if not _POST.fullmatch(post):
        return Refused(
            "approval.post is the Pumpkit post id the X post publishes, such as 812: x_post opens the post tools "
            "for that post only."
        )
    post_action = str(spec.get("post_action") or "")
    if post_action not in get_args(PostAction):
        return Refused("approval.post_action is post_now or schedule_post: x_post opens only the post tool it names.")
    when = _instant(spec.get("publish_at"))
    if post_action == "post_now" and spec.get("publish_at"):
        return Refused("approval.publish_at is left out of post_now, which publishes at once.")
    if post_action == "schedule_post" and (when is None or when.second or when.microsecond):
        return Refused(
            "approval.publish_at is when schedule_post publishes: ISO 8601 on a whole minute with its UTC offset, "
            "such as 2026-11-06T09:00:00+01:00."
        )
    community = " ".join(str(spec.get("community") or "").split()) or None
    if community is not None and not _COMMUNITY.fullmatch(community):
        return Refused("approval.community is the X community's URL or id, or left out for the profile.")
    share = spec.get("share_with_followers", False)
    if not isinstance(share, bool) or (share and community is None):
        return Refused("approval.share_with_followers is true or false, and true only with a community.")
    text = f"post Pumpkit post {post} on X now"
    if when is not None:
        offset = when.strftime("%z")
        zone = "UTC" if offset == "+0000" else f"UTC{offset[:3]}:{offset[3:]}"
        text = f"schedule Pumpkit post {post} on X for {when:%A} {when.day} {when:%B %Y at %H:%M} {zone}"
    text += f", in community {community}" if community else ""
    text += ", shared with followers" if share else ""
    publish_at = when.isoformat() if when is not None else None
    kind = cast(PostAction, post_action)
    return Approval(
        "", task_id, "subagent_action", repo, now, text, grants=grants, post=post, post_action=kind,
        publish_at=publish_at, community=community, share_with_followers=share,
    )


def _instant(value: object) -> datetime | None:
    """`value` as a time with its UTC offset, or None when it is not one."""
    try:
        when = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return when if when is not None and when.utcoffset() is not None else None


def _same_post_call(approval: Approval, tool_input: Mapping[str, object]) -> bool:
    """Whether a post tool's arguments are exactly those `approval` pins; a time may be written in another offset."""
    given = {k: v for k, v in tool_input.items() if not (k in ("community", "share_with_followers") and not v)}
    pinned = approval.post_arguments
    if given.keys() != pinned.keys() or any(given[k] != pinned[k] for k in pinned if k != "publish_at"):
        return False
    return "publish_at" not in pinned or _instant(given["publish_at"]) == _instant(pinned["publish_at"])


def approval_event(approval: Approval, status: str, text: str) -> str:
    return (
        f'<event source="hub" task="{approval.task_id}" approval="{approval.id}" status="{status}">'
        f"{escape(text, quote=False)}</event>"
    )


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
        self._heard: str | None = None
        self._presenting: set[str] = set()
        self._presented: set[str] = set()

    async def request(self, task_id: str, args: object, *, task_revision: int = 0) -> Approval | Refused:
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
            if target.forbidden or target.blocked:
                return Refused(f"Not asked: {target.change}.")
            if target.change is not None:
                return Refused(f"Not asked: {target.change}. Look again and ask about what is there now.")
        approval.task_revision = task_revision
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
            return self._subagent_action(task_id, spec, now)
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

    def _subagent_action(self, task_id: str, spec: dict[str, Any], now: float) -> Approval | Refused:
        raw: object = spec.get("grants") or []
        grants = [str(g) for g in cast(list[object], raw)] if isinstance(raw, list) else None
        if grants is None or any(g not in GRANTS for g in grants):
            return Refused(f"approval.grants lists any of {', '.join(GRANTS)}, or nothing.")
        repo = self._repo(spec.get("repo")) if spec.get("repo") else ""
        if isinstance(repo, Refused):
            return repo
        if "x_post" in grants:
            return _x_post(task_id, spec, grants, repo, now)
        description = " ".join(str(spec.get("description") or "").split())
        if not description:
            return Refused("approval.description is required: the one exact action, as the User will hear it.")
        return Approval("", task_id, "subagent_action", repo, now, description, grants=grants)

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
        return f"Pending Approvals: {named}. Interpret the User's intent in context; clarify only ambiguity."

    def turn_began(self, heard: str | None) -> None:
        self._heard = heard
        self._presenting.clear()

    def present(self, approval_id: object) -> str:
        approval = self.get(approval_id)
        if approval is None or approval.state != "pending":
            return f"Cannot present {approval_id}: it is not pending. {self.listing()}"
        self._presenting.add(approval.id)
        return f"Ask whether to proceed with {approval.id}: {approval.text}"

    def record_question(self, text: str) -> None:
        """The caller supplies the completed visible reply, never the action description."""
        if not text.strip():
            return
        for approval_id in self._presenting:
            self._all[approval_id].question = text
        if self._presenting:
            self._on_change()

    def turn_ended(self, *, completed: bool) -> None:
        if completed:
            self._presented.update(self._presenting)
        self._presenting.clear()
        self._heard = None

    def decide(self, approval_id: object) -> Approval | str:
        if not self._heard or not self._heard.strip():
            return "Not approved: approve works only in your reply to the User's own message."
        approval = self.get(approval_id)
        if approval is None or approval.state != "pending":
            return f"Not approved: {approval_id} is not pending. {self.listing()}"
        if approval.id not in self._presented:
            return f"Not approved: {approval.id} has not been presented in a completed reply. {self.listing()}"
        self.approve(approval, self._heard)
        return approval

    def approve(self, approval: Approval, heard: str) -> None:
        approval.state, approval.words, approval.approved = "approved", heard, self._clock.now()
        self._on_change()

    async def carry_out(self, approval: Approval, *, authorize: Callable[[], Awaitable[Refused | None]] | None = None,
                        authority_version: Callable[[], object] | None = None) -> str:
        """Runs an approved merge or branch delete after reading its target again; returns the Orchestrator's
        event. A changed target lapses the Approval, and the event says what changed."""
        self._running.add(approval.id)
        try:
            if self._expired(approval):
                approval.state, approval.outcome = "expired", "It expired before it ran."
                return approval_event(approval, "expired", f"{approval.text}: {approval.outcome} Request a fresh Approval, using applicable Authorization before asking the User.")
            while True:
                if authorize is not None and (refused := await authorize()) is not None:
                    return approval_event(approval, "held", refused.reason)
                version = authority_version() if authority_version is not None else None
                target = await self._read(approval)
                if authorize is not None and (refused := await authorize()) is not None:
                    return approval_event(approval, "held", refused.reason)
                if authority_version is None or version == authority_version():
                    break
            if target.blocked:
                approval.state, approval.outcome = "lapsed", str(target.change)
                text = (
                    f"Not done: the User approved {approval.text}, but {target.change}. Nothing ran. Once it can "
                    "merge, request a fresh concrete Approval, using applicable Authorization before asking the User."
                )
                return approval_event(approval, "lapsed", text)
            if target.change is not None:
                approval.state, approval.outcome = "lapsed", target.change
                text = (
                    f"Not done: the User approved {approval.text}, but {target.change}, so the yes no longer covers "
                    "it. Look at what changed and request a fresh concrete Approval, using applicable Authorization before asking the User."
                )
                return approval_event(approval, "lapsed", text)
            if self._expired(approval):
                approval.state, approval.outcome = "expired", "It expired before it ran."
                return approval_event(approval, "expired", f"{approval.text}: {approval.outcome} Request a fresh Approval, using applicable Authorization before asking the User.")
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
                "--json", "headRefOid,state,mergeStateStatus,baseRefName,headRefName", "--jq", MERGE_JQ,
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
        if code != 0 or len(words) != (5 if approval.action == "merge_pr" else 4):
            return _Target(f"the Hub could not read {what}: {printed.strip() or f'gh exited {code}'}")
        first, second = words[:2]
        sha = str(approval.sha)
        if approval.action == "merge_pr":
            if first != "OPEN":
                return _Target(f"{what} is {first.lower()} now")
            if second.lower() != sha:
                return _Target(f"the head of {what} is now {second[:12]}, not {sha[:12]}")
            merge_state, base, head = words[2:]
            if merge_state in MERGEABLE:
                return _Target(None)
            return _Target(await self._held_back(approval, what, merge_state, base, head), blocked=True)
        if second == "none":
            return _Target(f"{what} no longer exists in {approval.repo}")
        default, rule = words[2:]
        never = "and the Hub never deletes the default or a protected branch"
        if approval.branch == default:
            return _Target(f"{approval.branch} is the default branch of {approval.repo}, {never}", forbidden=True)
        code, printed = await self._runner(rules_argv(approval.repo, str(approval.branch)))
        if code != 0 and _NO_RULESETS not in printed:
            return _Target(f"the Hub could not read the rules of {what}: {printed.strip() or f'gh exited {code}'}")
        if rule != "unprotected" or KEEPING_RULES & set(printed.split()):
            return _Target(f"{approval.branch} is a protected branch of {approval.repo}, {never}", forbidden=True)
        if second.lower() != sha:
            return _Target(f"{what} now points at {second[:12]}, not {sha[:12]}")
        return _Target(None, node=first)

    async def _held_back(self, approval: Approval, what: str, merge_state: str, base: str, head: str) -> str:
        """Why GitHub would refuse to merge `what` now, and what to do about it."""
        if merge_state == "BLOCKED":
            held = await self._unmet_checks(approval, base, head)
            if held:
                return f"{what} cannot merge yet: {'; '.join(held)}"
            return (
                f"{what} cannot merge yet: GitHub holds it back (merge state BLOCKED), such as a required review or "
                "rule that is unmet. Read its checks and rules to find which, and have that done first"
            )
        why = {
            "BEHIND": f"its branch is behind {base}; update it first",
            "DIRTY": f"it conflicts with {base}; resolve the conflicts first",
            "DRAFT": "it is a draft; mark it ready first",
        }.get(merge_state, "GitHub has not finished working out whether it can merge; look again in a minute")
        return f"{what} cannot merge yet: {why}"

    async def _unmet_checks(self, approval: Approval, base: str, head: str) -> list[str]:
        """The checks the base branch requires that have not run or have not passed, each with what to do."""
        number = int(approval.number or 0)
        code, printed = await self._runner(required_checks_argv(approval.repo, base))
        required = sorted({line for line in printed.splitlines() if line.strip()}) if code == 0 else []
        code, printed = await self._runner(checks_argv(approval.repo, number))
        ran = dict(line.split("\t", 1) for line in printed.splitlines() if "\t" in line) if code == 0 else {}
        held: list[str] = []
        for check in required:
            result = ran.get(check)
            if result is None:
                held.append(f"the required check `{check}` has not run{_how_to_start(check, approval.repo, head)}")
            elif result not in PASSED:
                held.append(f"the required check `{check}` is {result.lower().replace('_', ' ')}")
        return held

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

        def check(tool_name: str, tool_input: Mapping[str, object]) -> Refused | None:
            verdict = base(tool_name, tool_input)
            if verdict is None:
                return None
            approval = self.get(approval_id)
            if approval is None or approval.subagent != subagent_id or tool_name not in approval.tools:
                return verdict
            if approval.state != "granted":
                return Refused(f"{approval.id} is {approval.state}, so {tool_name} needs a fresh Approval.")
            post_id = str(tool_input.get("post_id") or "")
            if approval.post is not None and tool_name in GRANTS["x_post"] and post_id != approval.post:
                return Refused(f"{approval.id} covers Pumpkit post {approval.post} only, not {post_id}.")
            if tool_name in GRANTS["x_post"] and not _same_post_call(approval, tool_input):
                pinned, given = json.dumps(approval.post_arguments), json.dumps(dict(tool_input), default=str)
                return Refused(f"{approval.id} covers exactly {tool_name} with {pinned}, not {given}.")
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

    def reassess(self, task_id: str) -> None:
        for approval in self._all.values():
            if approval.task_id == task_id and approval.state == "pending":
                approval.state, approval.outcome = "withdrawn", "New Authorization requires reassessing this action."
        self._on_change()

    def withdraw(self, task_id: str, *, reason: str = "Its Task was cancelled.") -> None:
        """Cancel or supersede the Task's Approvals, except an action the Hub is already running."""
        for approval in self._all.values():
            if approval.task_id == task_id and approval.state in LIVE and approval.id not in self._running:
                approval.state, approval.outcome = "withdrawn", reason
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
            approval = Approval(**_SAVED.load({"task_revision": 0, "authorization_id": None, "question": None, **r}, from_wall))
            numbered(approval.id, "A")
            one_of(approval.state, get_args(ApprovalState))
            approvals.append(approval)
        return approvals

    def restore(self, approvals: list[Approval]) -> tuple[list[Approval], list[str]]:
        """Takes back the loaded Approvals a fresh request could have made, and a notice for each one dropped. Returns
        the approved Hub actions the old Hub may not have finished, to run again after reading the target anew."""
        dropped: list[str] = []
        for approval in approvals:
            self._ids = max(self._ids, numbered(approval.id, "A"))
            invalid = self._invalid(approval)
            if invalid is not None:
                dropped.append(f"Dropped Approval {approval.id} from the state file: {invalid}")
                continue
            self._all[approval.id] = approval
        unfinished = [a for a in self._all.values() if a.state == "approved" and a.action in HUB_ACTIONS]
        return unfinished, dropped

    def _invalid(self, approval: Approval) -> str | None:
        """Why no request could have made `approval`, by the same checks as a fresh one; None when one could."""
        if approval.question is not None and not isinstance(cast(object, approval.question), str):
            return "The presented question must be text."
        if approval.action not in get_args(ApprovalAction):
            return f"{approval.action!r} is not an Approval action."
        spec = {
            "repo": approval.repo,
            "pr": approval.number,
            "branch": approval.branch,
            "sha": approval.sha,
            "description": None if "x_post" in approval.grants else approval.text,
            "grants": cast(object, approval.grants),
            "post": approval.post,
            "post_action": approval.post_action,
            "publish_at": approval.publish_at,
            "community": approval.community,
            "share_with_followers": approval.share_with_followers,
        }
        shaped = self._shaped(approval.task_id, approval.action, spec)
        if isinstance(shaped, Refused):
            return shaped.reason
        fields = (
            "repo", "text", "number", "branch", "sha", "grants", "post", "post_action", "publish_at", "community",
            "share_with_followers",
        )
        if any(getattr(shaped, f) != getattr(approval, f) for f in fields):
            return "it does not match the action it names."
        return None
