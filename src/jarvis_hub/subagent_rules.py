"""The rules a Subagent request passes before anything runs: model and effort, its branch name, its connector tools."""

import re
import unicodedata
from collections.abc import Sequence

from jarvis_hub.agents.backend import Refused
from jarvis_hub.config import EFFORTS, Effort, is_effort

BRANCH_KINDS = ("feat", "fix", "chore")
# Every model accepts every effort level, so the limits are JARVIS_MAX_EFFORT and the User's ask.
MODELS = ("haiku", "sonnet", "opus")
# The levels that spend the User's limits fastest, granted only on a request in one of these forms; anything else
# denies, since a false deny costs the User one plain sentence (docs/notes/phase-0-spikes.md, "Asking for xhigh or max").
_EN_ASK = (
    r"(?:(?:^|,)\s*(?:with|at)|\buse|\bgo(?:\s+(?:with|at|to))?|\bgive\s+it"
    r"|\b(?:run|rerun|do|redo|review|fix|build|try|handle|check|write|implement|investigate|debug|plan|design|start)"
    r"\s+(?:it|this|that|them)\s+(?:with|at))\s+(?:the\s+)?"
)
_EN_SET = r"effort\s*(?::|=|\s+to|\s+at)\s*(?:the\s+)?"
_IT_ASK = r"(?:(?:^|,)\s*con|\busa|\busate|\bmetti(?:ci)?|\bimposta)\s+"
ON_REQUEST: dict[Effort, re.Pattern[str]] = {
    "xhigh": re.compile(
        rf"{_EN_ASK}(?:x[- ]?high|extra[- ]high\s+effort)\b|\b{_EN_SET}(?:x[- ]?high|extra[- ]high)\b"
        r"|(?:^|,)\s*(?:x[- ]?high|extra[- ]high)\s+effort(?:,?\s+please)?\W*$",
        re.IGNORECASE,
    ),
    "max": re.compile(
        rf"{_EN_ASK}max(?:imum)?\s+effort\b|\b{_EN_SET}max(?:imum)?\b"
        r"|(?:^|,)\s*max(?:imum)?\s+effort(?:,?\s+please)?\W*$"
        rf"|{_IT_ASK}(?:lo\s+|l['\u2019]\s*|il\s+)?(?:sforzo|impegno)\s+(?:al\s+)?massimo\b"
        rf"|{_IT_ASK}il\s+massimo\s+(?:dello\s+sforzo|sforzo|impegno)\b|\b(?:sforzo|impegno)\s*:\s*massimo\b",
        re.IGNORECASE,
    ),
}
EFFORT_WORD = re.compile(r"\b(effort|x[- ]?high|extra[- ]high|max|maximum|sforzo|impegno|massimo)\b", re.IGNORECASE)
# Any of these in a sentence that names an effort denies the whole turn: a refusal, a limit, a hedge or a question.
DOUBT = re.compile(
    r"\?|^\s*(is|are|was|were|do|does|did|shall|can|may|why|what|how|when)\b"
    r"|\b(don['\u2019]?t|do\s+not|\w+n['\u2019]t|\w+['\u2019](?:d|ll)|will|never|no|not|nope|nah|none|nothing"
    r"|without|avoid|skip|instead|rather|except|unless|forget|ignore|stop|quit|cancel|drop|unnecessary|needless"
    r"|overkill|excessive|too|neither|nor|below|under|lower|less|fewer|than|cap|limit|reduce"
    r"|if|maybe|perhaps|might|could|should|would|whether"
    r"|non|senza|mai|niente|nessun[oa]?|evit\w*|invece|piuttosto|tranne|lascia(?:mo)?\s+(?:perdere|stare)"
    r"|dimentic\w*|basta|meno|sotto|inferiore|troppo|inutile|esagerat[oa]|eccessiv[oa]|n\u00e9|neanche|nemmeno"
    r"|neppure|se|forse|serve|necessari[oa]|dovremmo|anzi)\b",
    re.IGNORECASE,
)
# Any of these after the request takes it back, in a later sentence or a later turn.
RETRACT = re.compile(
    r"\b(actually|never\s*mind|forget|scratch\s+that|on\s+second\s+thought|wait|hold\s+on|cancel|undo"
    r"|anzi|lascia(?:mo)?\s+(?:stare|perdere)|ripensandoci|aspetta|scherzavo)\b",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"[^.!?;\n]+[.!?;\n]*")

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
    """The on-request effort the User asked for in the latest turn that speaks of effort or takes a request back."""
    asked: Effort | None = None
    for turn in user_words:
        if EFFORT_WORD.search(turn) or RETRACT.search(turn):
            asked = _turn_asks(turn)
    return asked


def _turn_asks(turn: str) -> Effort | None:
    sentences = [m.group().strip() for m in SENTENCE.finditer(turn)]
    asks: list[tuple[int, Effort]] = [
        (i, level) for i, s in enumerate(sentences) for level, form in ON_REQUEST.items() if form.search(s)
    ]
    if not asks:
        return None
    if any(EFFORT_WORD.search(s) and DOUBT.search(s) for s in sentences):
        return None
    first = min(i for i, _ in asks)
    if any(RETRACT.search(s) or DOUBT.search(s) for s in sentences[first + 1 :]):
        return None
    levels: list[Effort] = [level for _, level in asks]
    return max(levels, key=EFFORTS.index)


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
            return Refused(
                f"{effort} is used only when the User asks for it in plain words, such as 'use {effort} effort'; "
                "use high or lower."
            )
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
