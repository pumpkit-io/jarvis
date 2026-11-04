"""The rules a Subagent request passes before anything runs: model and effort, its branch name, its connector tools."""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from jarvis_hub.agents.backend import Refused
from jarvis_hub.config import EFFORTS, Effort, is_effort

BRANCH_KINDS = ("feat", "fix", "chore")
# Every model accepts every effort level, so the limits are JARVIS_MAX_EFFORT and the User's ask.
MODELS = ("haiku", "sonnet", "opus")
# The levels that spend the User's limits fastest, granted only on a request in one of these forms; anything else
# denies, since a false deny costs the User one plain sentence (docs/notes/phase-0-spikes.md, "Asking for xhigh or max").
_LEVEL: dict[Effort, str] = {"xhigh": r"(?:x[- ]?high|extra[- ]high)", "max": r"max(?:imum)?"}
# The ask opens its sentence or a comma clause, after these words only, and closes the sentence, so reported speech,
# descriptions and sarcasm ("Tony said to use max effort", "use max effort and burn my credits") don't grant.
_OPENING = r"^(?:.*,)?\s*(?:(?:ok(?:ay)?|please|sure|yes|sì|va\s+bene|per\s+favore)\b\s*,?\s*)*"
_CLOSING = r"(?:\s+(?:on|for|per|su)(?:\s+\w+){1,3})?(?:\s*,?\s*(?:please|grazie|per\s+favore))?\W*$"
_EN_VERB = (
    r"(?:with|at|use|go(?:\s+(?:with|at|to))?|give\s+it"
    r"|(?:run|rerun|do|redo|review|fix|build|try|handle|check|write|implement|investigate|debug|plan|design|start)"
    r"\s+(?:it|this|that|them)\s+(?:with|at))\s+(?:the\s+)?"
)
_IT_VERB = r"(?:con|usa|usate|metti(?:ci)?|imposta)\s+"
_IT_MAX = r"(?:(?:lo\s+|l['’]\s*|il\s+)?(?:sforzo|impegno)\s+(?:al\s+)?massimo|il\s+massimo\s+(?:dello\s+sforzo|sforzo|impegno))"


def _ask_form(level: Effort) -> re.Pattern[str]:
    asks = [
        rf"(?:{_EN_VERB})?{_LEVEL[level]}\s+effort",
        rf"set\s+(?:the\s+)?effort\s+(?:to|at)\s+(?:the\s+)?{_LEVEL[level]}",
    ]
    # "effort: max" grants only as the whole sentence, never inside a description of an old config.
    whole = [rf"^\s*(?:please\s*,?\s*)?effort\s*[:=]\s*{_LEVEL[level]}"]
    if level == "max":
        asks.append(rf"{_IT_VERB}{_IT_MAX}")
        whole.append(r"^\s*(?:sforzo|impegno)\s*:\s*massimo")
    return re.compile(rf"(?:{_OPENING}(?:{'|'.join(asks)})|{'|'.join(whole)}){_CLOSING}", re.IGNORECASE)


ON_REQUEST: dict[Effort, re.Pattern[str]] = {level: _ask_form(level) for level in _LEVEL}
# The lower levels count too, so a downgrade such as "make it high" decides the latest turn.
EFFORT_WORD = re.compile(
    r"\b(effort|x[- ]?high|extra[- ]high|max|maximum|high|medium|low|sforzo|impegno|massimo|alto|medio|basso)\b",
    re.IGNORECASE,
)
# Any of these in a sentence that names an effort denies the whole turn.
_QUESTION = r"\?|^\s*(?:is|are|was|were|do|does|did|shall|can|may|why|what|how|when)\b"
_EN_REFUSAL = (
    r"don['’]?t|do\s+not|\w+n['’]t|\w+['’](?:d|ll)|will|never|no|not|nope|nah|none|nothing"
    r"|without|avoid|skip|instead|rather|except|unless|forget|ignore|stop|quit|cancel|drop"
)
_EN_LIMIT = (
    r"unnecessary|needless|overkill|excessive|too|neither|nor|below|under|lower|less|fewer|than|cap|limit|reduce"
)
_EN_HEDGE = r"if|maybe|perhaps|might|could|should|would|whether"
_IT_REFUSAL = (
    r"non|senza|mai|niente|nessun[oa]?|evit\w*|invece|piuttosto|tranne|lascia(?:mo)?\s+(?:perdere|stare)"
    r"|dimentic\w*|basta|anzi"
)
_IT_LIMIT = r"meno|sotto|inferiore|troppo|inutile|esagerat[oa]|eccessiv[oa]|né|neanche|nemmeno|neppure"
_IT_HEDGE = r"se|forse|serve|necessari[oa]|dovremmo"
DOUBT = re.compile(
    rf"{_QUESTION}|\b(?:{_EN_REFUSAL}|{_EN_LIMIT}|{_EN_HEDGE}|{_IT_REFUSAL}|{_IT_LIMIT}|{_IT_HEDGE})\b",
    re.IGNORECASE,
)
# Any of these after the request takes it back, in a later sentence or a later turn.
RETRACT = re.compile(
    r"\b(actually|never\s*mind|forget|scratch\s+that|on\s+second\s+thought|wait|hold\s+on|cancel|undo"
    r"|back\s+to\s+normal|(?:just\s+)?kidding|psych"
    r"|anzi|lascia(?:mo)?\s+(?:stare|perdere)|ripensandoci|aspetta|scherzavo|scherzo)\b",
    re.IGNORECASE,
)
# A turn that is, or opens with, one of these takes back whatever came before; a bare word must stand alone, so
# "No rush." does not.
TAKE_BACK = re.compile(
    r"^\W*(?:(?:ignore\s+that|never\s*mind|just\s+kidding|no\s+grazie)\b"
    r"|(?:no|nah|nope|kidding|psych|scherzo)\b(?!\s+\w))",
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
        if TAKE_BACK.search(turn):
            asked = None
        elif EFFORT_WORD.search(turn) or RETRACT.search(turn):
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


def connector_check(tool_name: str, tool_input: Mapping[str, Any] | None = None) -> Refused | None:
    if tool_name in READ_CONNECTOR_TOOLS:
        return None
    return Refused(
        f"{tool_name} can change something outside the repos, so a Subagent may call it only with an Approval "
        "from the User, and this brief carries none. Report what you would do with it instead."
    )
