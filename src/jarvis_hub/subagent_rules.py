"""The rules a Subagent request passes before anything runs: model and effort, its branch name, its connector tools."""

import re
import unicodedata
from collections.abc import Sequence

from jarvis_hub.agents.backend import Refused
from jarvis_hub.config import EFFORTS, Effort, is_effort

BRANCH_KINDS = ("feat", "fix", "chore")
# Every model accepts every effort level, so the limits are JARVIS_MAX_EFFORT and the User's ask.
MODELS = ("haiku", "sonnet", "opus")
# The levels that spend the User's limits fastest, used only when the User's own words ask for one by name.
ON_REQUEST: dict[Effort, re.Pattern[str]] = {
    "xhigh": re.compile(r"\bx[- ]?high\b|\bextra[- ]high\s+effort\b", re.IGNORECASE),
    "max": re.compile(
        r"\b(max|maximum)\s+effort\b|\beffort[:=]?\s+(at\s+|to\s+|of\s+)?(max|maximum)\b"
        r"|\bsforzo\s+massimo\b|\bmassimo\s+(dello\s+)?sforzo\b|\bmassimo\s+impegno\b|\bimpegno\s+massimo\b",
        re.IGNORECASE,
    ),
}

# A negation earlier in the same clause turns the request around: "don't use xhigh", "non usare sforzo massimo".
NEGATION = re.compile(r"\b(don['\u2019]?t|do\s+not|never|no|not|without|non|senza|mai)\b", re.IGNORECASE)
CLAUSE_END = re.compile(r"[.,;:!?\n]")

# Pumpkit's tools that only read, under both names it reaches a session by: the claude.ai connector of the User's
# account, and a `pumpkit` MCP server in the User's own config, such as a local Instance (ADR 0008).
PUMPKIT_READS = ("get_post", "list_posts", "get_usage", "list_inspiration_authors", "list_past_communities")
PUMPKIT_SERVERS = ("claude_ai_Pumpkit", "pumpkit")
CLAUDE_DOCS_READS = ("guide", "read", "query")
# Every other MCP tool is refused in a Subagent (ADR 0008).
READ_CONNECTOR_TOOLS = frozenset(
    [f"mcp__{server}__{tool}" for server in PUMPKIT_SERVERS for tool in PUMPKIT_READS]
    + [f"mcp__claude_ai_Claude_Docs__{tool}" for tool in CLAUDE_DOCS_READS]
)


def asked_effort(user_words: Sequence[str]) -> Effort | None:
    """The highest on-request effort the User's own words name, if any."""
    asked: list[Effort] = [level for level, pattern in ON_REQUEST.items() if any(_requests(pattern, w) for w in user_words)]
    return max(asked, key=EFFORTS.index) if asked else None


def _requests(pattern: re.Pattern[str], words: str) -> bool:
    """Whether the words match the pattern somewhere that no negation in the same clause precedes."""
    for match in pattern.finditer(words):
        clause = CLAUSE_END.split(words[: match.start()])[-1]
        if not NEGATION.search(clause):
            return True
    return False


def check_choice(model: str, effort: str, *, max_effort: Effort, user_words: Sequence[str]) -> Refused | None:
    """Whether a Subagent may run on this model at this effort, given the cap and what the User has said."""
    if not any(re.fullmatch(rf"(claude-)?{family}(-[\w.-]+)?", model) for family in MODELS):
        return Refused(f"{model!r} is not a model; use haiku, sonnet or opus.")
    if not is_effort(effort):
        return Refused(f"{effort!r} is not an effort; use one of {', '.join(EFFORTS)}.")
    if EFFORTS.index(effort) > EFFORTS.index(max_effort):
        return Refused(f"{effort} is above JARVIS_MAX_EFFORT={max_effort}; use {max_effort} or lower.")
    if effort in ON_REQUEST:
        asked = asked_effort(user_words)
        if asked is None or EFFORTS.index(asked) < EFFORTS.index(effort):
            return Refused(f"{effort} is used only when the User asks for it; use high or lower.")
    return None


def branch_name(kind: str, title: str) -> str:
    """An sdlc-style branch for the work: `<kind>/<slug of the title>`."""
    return f"{kind}/{slug(title)}"


def slug(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40].strip("-") or "work"


def connector_check(tool_name: str) -> Refused | None:
    if tool_name in READ_CONNECTOR_TOOLS:
        return None
    return Refused(
        f"{tool_name} can change something outside the repos, so a Subagent may call it only with an Approval "
        "from the User, and this brief carries none. Report what you would do with it instead."
    )
