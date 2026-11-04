"""Approvals (ADR 0004, ADR 0013): the User's recorded yes to one exact Boundary action, decided by the Hub from the
User's own words. The Hub merges and deletes branches itself; any other action goes to one Subagent, for one call."""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from html import escape
from typing import Any, Literal, Protocol, cast, get_args

from jarvis_hub.agents.backend import McpGuard, Refused
from jarvis_hub.approval_words import LEXICONS, Words, found, numbers, words
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

_POST = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"(?:(?P<owner>[A-Za-z0-9-]+)/)?(?P<name>[A-Za-z0-9._-]+)")
# What git accepts as a branch name, narrowed: no leading dash or slash, no `..`, `//`, `@{`, `.lock` or trailing `/`.
_BRANCH = re.compile(r"(?!-|/)(?!.*(?:\.\.|//|@\{|\.lock$|/$|\.$))[A-Za-z0-9._/+-]{1,200}")

# One read for a branch: the repository's node id, which the delete needs, the branch tip or "none", the default
# branch, and whether a protection rule or ruleset covers the branch.
REF_QUERY = (
    "query($owner:String!,$name:String!,$ref:String!)"
    "{repository(owner:$owner,name:$name){id defaultBranchRef{name} ref(qualifiedName:$ref){target{oid} "
    "branchProtectionRule{id} refUpdateRule{allowsDeletions}}}}"
)
REF_JQ = (
    '.data.repository as $r | $r.id + " " + ($r.ref.target.oid // "none") + " " + ($r.defaultBranchRef.name // "none")'
    ' + " " + (if $r.ref.branchProtectionRule or $r.ref.refUpdateRule then "protected" else "unprotected" end)'
)
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
        "post",
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
    # The change is a branch the Hub never deletes, whatever its tip.
    forbidden: bool = False


# A thing words can name an Approval by: ("n", a number), ("p", a phrase) or ("k", a kind of action).
_Feature = tuple[str, object]


@dataclass(frozen=True)
class _Names:
    """What the User's words may name an Approval by: numbers, phrases such as the repo or branch, and action verbs."""

    numbers: frozenset[int]
    phrases: frozenset[Words]
    kinds: frozenset[str]

    @staticmethod
    def of(approval: "Approval") -> "_Names":
        phrases = _repo_phrases(approval.repo.split("/", 1)[-1]) if approval.repo else set[Words]()
        if approval.action == "merge_pr":
            phrases |= {("pr",), ("pull", "request")}
            return _Names(frozenset({int(approval.number or 0)}), frozenset(phrases), frozenset({"merge"}))
        if approval.action == "delete_branch":
            branch = str(approval.branch)
            phrases |= {words(branch), words(branch.rsplit("/", 1)[-1]), ("branch",)}
            return _Names(numbers(words(branch)), frozenset(phrases), frozenset({"delete"}))
        said = words(approval.text)
        post = {int(approval.post)} if approval.post and approval.post.isdigit() else set[int]()
        return _Names(numbers(said) | post, frozenset(phrases), LEXICONS.kinds(said))

    def features(self) -> set[_Feature]:
        out: set[_Feature] = {("n", n) for n in self.numbers}
        return out | {("p", p) for p in self.phrases} | {("k", k) for k in self.kinds}


def _repo_phrases(name: str) -> set[Words]:
    """How words name a repo: its name, alone or with "repo". "Jarvis" alone is the User addressing Jarvis."""
    said = words(name)
    qualified = {said + (repo,) for repo in ("repo", "repository")} | {(repo,) + said for repo in ("repo", "repository")}
    return qualified if said == ("jarvis",) else qualified | {said}


def _said(said: Words, phrases: Iterable[Words]) -> set[_Feature]:
    """The features in `said`: its numbers, verbs, and those of `phrases` not inside a longer one said ("pumpkit" in
    "pumpkit ops")."""
    spans = [(i, len(p), p) for p in set(phrases) for i in found(said, p)]
    hits = {p for i, n, p in spans if not any(j <= i and i + n <= j + m and m > n for j, m, _ in spans)}
    out: set[_Feature] = {("n", n) for n in numbers(said)}
    return out | {("k", k) for k in LEXICONS.kinds(said)} | {("p", p) for p in hits}


def _plain_yes(approval: "Approval") -> str:
    if approval.action == "merge_pr":
        return f"yes, merge {approval.number}"
    if approval.action == "delete_branch":
        return f"yes, delete {str(approval.branch).rsplit('/', 1)[-1]}"
    return "yes, go ahead"


def _spoken(feature: _Feature) -> str:
    kind, value = feature
    return f'"{" ".join(cast(Words, value)) if kind == "p" else value}"'


def named(text: str, among: list["Approval"]) -> list["Approval"]:
    """The Approvals `text` names by something no other of `among` shares: "merge" only when one is a merge."""
    names = {a.id: _Names.of(a).features() for a in among}
    said = _said(words(text), (p for a in among for p in _Names.of(a).phrases))
    return [a for a in among if (names[a.id] & said) - set().union(*(names[b.id] for b in among if b is not a))]


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
        # The turn under way: the User's message it answers, whether it recorded a yes, and whether a refused approve
        # lets the Talker read back the Approval(s) meant.
        self._heard: str | None = None
        self._approved_in_turn = False
        self._read_back_open = False
        # The Approvals the last finished turn asked the User about, and whether a read-back named them.
        self._last_said: tuple[str, ...] = ()
        self._read_back = False

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
            if target.forbidden:
                return Refused(f"Not asked: {target.change}.")
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
            post = str(spec.get("post") or "").strip() if "x_post" in grants else None
            if post is not None and not _POST.fullmatch(post):
                return Refused(
                    "approval.post is the Pumpkit post id the X post publishes, such as 812: x_post opens the post "
                    "tools for that post only."
                )
            return Approval("", task_id, action, repo, now, description, grants=grants, post=post)
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

    def turn_began(self, heard: str | None) -> None:
        """A Talker turn starts; `heard` is the User's message it answers, None when it answers none."""
        self._heard, self._approved_in_turn, self._read_back_open = heard, False, False

    def turn_ended(self, *, completed: bool, told: Iterable[str], said: str) -> None:
        """What the finished turn asked the User about: the Approvals whose requests its news `told`, or, after a
        refused approve, those the Talker's reply `said` names. A turn cut short may not have been heard."""
        self._read_back = False
        if not completed:
            self._last_said = ()
        elif self._read_back_open and (read := named(said, self.pending())):
            self._last_said, self._read_back = tuple(a.id for a in read), True
        else:
            self._last_said = tuple(told)
        self._heard, self._read_back_open = None, False

    def decide(self, approval_id: object) -> Approval | str:
        """Approves when the User's words in this turn say yes, and to this Approval; else says why not."""
        if self._approved_in_turn:
            return "Not approved: one yes approves one action. Ask about the next one in its own question."
        listing = self.listing()
        refusal = self._refusal(approval_id, listing)
        if refusal is not None:
            return refusal
        approval = cast(Approval, self.get(approval_id))
        approval.state, approval.words, approval.approved = "approved", self._heard, self._clock.now()
        self._approved_in_turn = True
        self._on_change()
        return approval

    def _refusal(self, approval_id: object, listing: str) -> str | None:
        heard = self._heard
        if heard is None:
            self._read_back_open = True
            return f"Not approved: approve works only in your reply to the User's own message. {listing}"
        # Checked first, so a "no" never lets a read-back make the next plain yes count.
        nothing = "Nothing was done; tell them so, and ask again only if they want it."
        held = LEXICONS.holding_back(heard)
        if held is not None:
            return f"Not approved: the User's words hold {held}, so they are not a yes. {nothing}"
        said = words(heard)
        if not LEXICONS.says_yes(said) and not LEXICONS.kinds(said):
            return f'Not approved: the User\'s words hold no yes, such as "yes" or "go ahead". {nothing}'
        approval = self.get(approval_id)
        if approval is None or approval.state != "pending":
            self._read_back_open = True
            return f"Not approved: {approval_id} is not pending. {listing}"
        pending = self.pending()
        candidates = [a for a in pending if a.id in self._last_said]
        if approval not in candidates:
            self._read_back_open = True
            return f"Not approved: {approval.id} was not the last thing you asked the User about. {listing}"
        own = _Names.of(approval)
        if len(candidates) > 1 or (len(pending) > 1 and not self._read_back):
            chosen = named(heard, candidates)
            self._read_back_open = True
            if not chosen:
                return f"Not approved: with more than one Approval in play, a bare yes does not say which. {listing}"
            if chosen != [approval]:
                other = next(a for a in chosen if a is not approval)
                return f"Not approved: the User's words name {other.id} ({other.text}), not {approval.id}. {listing}"
        stray = _said(said, (p for a in pending for p in _Names.of(a).phrases)) - own.features()
        if stray:
            others = [a for a in named(heard, pending) if a is not approval]
            what = f"{others[0].id} ({others[0].text})" if others else _spoken(min(stray, key=str))
            return f"Not approved: the User's words name {what}, which is not {approval.id} ({approval.text}). {nothing}"
        now = {("now",), ("ora",), ("adesso",)} if "schedule" not in own.kinds else set[Words]()
        outside = LEXICONS.outside_plain_yes(said, own.phrases | now, own.kinds, own.numbers)
        if outside is not None:
            return (
                f'Not approved: the User\'s words hold "{outside}", which is not part of a plain yes. {nothing} To '
                f'approve, they say a plain yes, such as "{_plain_yes(approval)}".'
            )
        return None

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
        if code != 0 or len(words) != (2 if approval.action == "merge_pr" else 4):
            return _Target(f"the Hub could not read {what}: {printed.strip() or f'gh exited {code}'}")
        first, second = words[:2]
        sha = str(approval.sha)
        if approval.action == "merge_pr":
            if first != "OPEN":
                return _Target(f"{what} is {first.lower()} now")
            if second.lower() != sha:
                return _Target(f"the head of {what} is now {second[:12]}, not {sha[:12]}")
            return _Target(None)
        if second == "none":
            return _Target(f"{what} no longer exists in {approval.repo}")
        default, rule = words[2:]
        if approval.branch == default or rule != "unprotected":
            kind = "the default branch" if approval.branch == default else "a protected branch"
            never = "and the Hub never deletes the default or a protected branch"
            return _Target(f"{approval.branch} is {kind} of {approval.repo}, {never}", forbidden=True)
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
        if approval.action not in get_args(ApprovalAction):
            return f"{approval.action!r} is not an Approval action."
        spec = {
            "repo": approval.repo,
            "pr": approval.number,
            "branch": approval.branch,
            "sha": approval.sha,
            "description": approval.text,
            "grants": cast(object, approval.grants),
            "post": approval.post,
        }
        shaped = self._shaped(approval.task_id, approval.action, spec)
        if isinstance(shaped, Refused):
            return shaped.reason
        fields = ("repo", "text", "number", "branch", "sha", "grants", "post")
        if any(getattr(shaped, f) != getattr(approval, f) for f in fields):
            return "it does not match the action it names."
        return None
