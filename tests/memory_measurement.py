"""Made-up Conversations, planted facts and result tables for the opt-in Memory measurement
(`test_real_memory_measurement.py`, `docs/notes/talker-memory.md`). Nothing here opens a model session."""

import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import ClaudeSDKClient, RateLimitEvent, ResultMessage, StreamEvent, SystemMessage
from claude_agent_sdk.types import RateLimitInfo

from jarvis_hub.agents.backend import ToolHandler, TurnEnd
from jarvis_hub.agents.sdk import binding, turn_end
from jarvis_hub.conversation import CHARS_PER_TOKEN, Entry
from jarvis_hub.event_log import RECALL_LIMIT, EventLog
from jarvis_hub.hub import RECALL_DATES, user_message
from jarvis_hub.voice.language import Lang

Language = Literal["en", "it", "mixed"]
DAY = 86400.0
OPT_IN = "JARVIS_REAL_MODEL_TESTS"


@dataclass(frozen=True)
class Exchange:
    said: str
    lang: Lang
    reply: str
    at: float
    ended: float

    def entry(self) -> Entry:
        """As the Hub saves a typed line from the Mac."""
        return Entry(user_message(self.said, self.lang), self.reply, (), "Mac", "user", self.at, self.ended)


@dataclass(frozen=True)
class Fact:
    """Something the User says once and is asked about after several Folds; `answer` is the word a right answer
    holds."""

    statement: str
    question: str
    answer: str
    lang: Lang
    reply: str


# Two standing facts and two one-off details, none of which a filler line mentions.
FACTS = (
    Fact("By the way, my sister's new puppy is called Brunello.", "What is my sister's new puppy called?",
         "Brunello", "en", "Brunello, noted, sir."),
    Fact("Ricordati che la cena di fine anno con il team sarà a Matera.",
         "Dove sarà la cena di fine anno con il team?", "Matera", "it", "Annotato: la cena sarà a Matera."),
    Fact("The plumber who fixed the boiler on Saturday was Ottavio Brambilla.",
         "What was the name of the plumber who fixed the boiler?", "Brambilla", "en", "Ottavio Brambilla, noted."),
    Fact("Il mio gusto di gelato preferito è il pistacchio di Bronte.", "Qual è il mio gusto di gelato preferito?",
         "pistacchio", "it", "Pistacchio di Bronte, me lo segno."),
)

_REPOS = ("jarvis", "pumpkit", "sdlc", "the landing site", "the billing service", "the iOS app")
_EN_THINGS = ("the release branch", "the nightly build", "the flaky login test", "the README", "the deploy script",
              "the open pull request", "the dependency update", "the cache settings", "the error dashboard")
_EN_STATES = ("still red", "green again", "waiting for review", "merged", "stuck on a lint error", "half done")
_EN_ASKS = (
    "Could you check whether {thing} in {repo} is {state}?",
    "What happened to {thing} in {repo} since this morning?",
    "Remind me why we touched {thing} in {repo} last week.",
    "Is {thing} in {repo} something I need to look at today, or can it wait?",
    "Tell me in two sentences where {thing} in {repo} stands.",
    "I was reading about {topic} on the train; does it matter for {repo}?",
)
_EN_REPLIES = (
    "{thing} in {repo} is {state}, sir. The last change came from the overnight run and nobody has commented since.",
    "From what I can see, {thing} in {repo} is {state}. I would leave it until the review comes back.",
    "It is {state}, sir. The only open question is whether {topic} changes the plan for {repo}.",
    "{repo} has {thing} {state}; I can look closer if you want, but nothing there needs you right now.",
    "Not much for {repo}, sir. {topic} would mostly change how we write the tests, not what ships.",
)
_EN_TOPICS = ("structured logging", "property-based testing", "smaller pull requests", "feature flags",
              "trunk-based development", "SQLite in production", "prompt caching", "on-device speech models")
_IT_THINGS = ("il branch di rilascio", "la build notturna", "il test di login instabile", "il README",
              "lo script di deploy", "la pull request aperta", "l'aggiornamento delle dipendenze",
              "le impostazioni della cache", "la dashboard degli errori")
_IT_STATES = ("ancora rosso", "di nuovo verde", "in attesa di revisione", "unito", "bloccato su un errore di lint",
              "a metà")
_IT_ASKS = (
    "Puoi controllare se {thing} in {repo} è {state}?",
    "Cosa è successo a {thing} in {repo} da stamattina?",
    "Ricordami perché abbiamo toccato {thing} in {repo} la settimana scorsa.",
    "Devo guardare {thing} in {repo} oggi, o può aspettare?",
    "Dimmi in due frasi a che punto è {thing} in {repo}.",
    "Stavo leggendo di {topic} in treno; conta qualcosa per {repo}?",
)
_IT_REPLIES = (
    "{thing} in {repo} è {state}. L'ultima modifica viene dal giro notturno e nessuno ha commentato da allora.",
    "Da quello che vedo, {thing} in {repo} è {state}. Aspetterei che torni la revisione.",
    "È {state}. L'unica domanda aperta è se {topic} cambia i piani per {repo}.",
    "In {repo}, {thing} è {state}; posso guardare meglio se vuoi, ma per ora non serve il tuo intervento.",
    "Poco per {repo}. {topic} cambierebbe soprattutto come scriviamo i test, non cosa rilasciamo.",
)
_IT_TOPICS = ("i log strutturati", "i test basati su proprietà", "pull request più piccole", "i feature flag",
              "lo sviluppo su trunk", "SQLite in produzione", "la cache dei prompt", "i modelli vocali sul dispositivo")


def _filler(rng: random.Random, lang: Lang) -> tuple[str, Lang, str]:
    if lang == "en":
        asks, replies, things, states, topics = _EN_ASKS, _EN_REPLIES, _EN_THINGS, _EN_STATES, _EN_TOPICS
    else:
        asks, replies, things, states, topics = _IT_ASKS, _IT_REPLIES, _IT_THINGS, _IT_STATES, _IT_TOPICS
    slots = {"repo": rng.choice(_REPOS), "thing": rng.choice(things), "state": rng.choice(states),
             "topic": rng.choice(topics)}
    said = rng.choice(asks).format(**slots)
    reply = " ".join(rng.choice(replies).format(**slots) for _ in range(rng.randint(1, 3)))
    return said, lang, reply[0].upper() + reply[1:]


def made_up_conversation(tokens: int, *, language: Language, start: float, seed: int, facts: Sequence[Fact] = (),
                         step: float = 120.0) -> list[Exchange]:
    """Exchanges whose rendered entries come to about `tokens` at the Hub's starting estimate, one every `step`
    seconds from `start`, with `facts` said in order at even intervals. The same seed gives the same Conversation."""
    rng = random.Random(seed)
    planted = [Exchange(fact.statement, fact.lang, fact.reply, 0.0, 0.0) for fact in facts]
    chars = sum(len(e.entry().render()) for e in planted)
    said: list[Exchange] = []
    while chars / CHARS_PER_TOKEN < tokens:
        lang: Lang = rng.choice(("en", "it")) if language == "mixed" else language
        said.append(Exchange(*_filler(rng, lang), 0.0, 0.0))
        chars += len(said[-1].entry().render())
    gap = len(said) // (len(planted) + 1)
    for n, exchange in enumerate(planted, start=1):
        said.insert(n * gap + n - 1, exchange)
    return [replace(e, at=start + i * step, ended=start + i * step + 20.0) for i, e in enumerate(said)]



def folded_conversation(folds: int, *, block_tokens: int, tail_tokens: int, now: float,
                        seed: int) -> tuple[list[list[Exchange]], list[Exchange]]:
    """One block per Fold, the first fact said in the first block and so on, then a Tail with no fact. The blocks
    start four days apart, from 20 days before `now`, so `recall` reaches all of them."""
    if folds > len(FACTS):
        raise ValueError(f"at most {len(FACTS)} folds, one per planted fact")
    start = now - 20 * DAY
    blocks = [made_up_conversation(block_tokens, language="mixed", start=start + i * 4 * DAY, seed=seed * 100 + i,
                                   facts=(FACTS[i],)) for i in range(folds)]
    tail = made_up_conversation(tail_tokens, language="mixed", start=start + folds * 4 * DAY, seed=seed * 100 + folds)
    return blocks, tail


Survival = Literal["summary", "recall", "neither", "lost"]


def survived_through(fact: Fact, *, summary: str, recalled: Sequence[str], reply: str) -> Survival:
    """Where a right answer about `fact` came from: the summary before `recall`, since the Talker read it first."""
    def holds(text: str) -> bool:
        return fact.answer.lower() in text.lower()

    if not holds(reply):
        return "lost"
    if holds(summary):
        return "summary"
    return "recall" if any(holds(result) for result in recalled) else "neither"


def opted_in(environ: Mapping[str, str]) -> bool:
    """The switch every real-model test needs (`tests/conftest.py`): a run spends the User's Max usage."""
    return environ.get(OPT_IN) == "1"


def estimate_error(*, chars: int, tokens: int) -> float:
    """How far the Hub's starting estimate is from `tokens` the CLI counted, as a share of them: +0.1 is 10% over."""
    return (chars / CHARS_PER_TOKEN - tokens) / tokens


def _cell(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def markdown_table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [header, ["---"] * len(header), *[[_cell(value) for value in row] for row in rows]]
    return "\n".join(f"| {' | '.join(line)} |" for line in lines)


class Results:
    """A run's results as Markdown, one section per measurement, ready to paste into `docs/notes/talker-memory.md`."""

    def __init__(self, path: Path, *, stamp: str) -> None:
        self.path = path
        self._stamp = stamp

    def add(self, title: str, body: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out:
            if out.tell() == 0:
                out.write(f"# Talker Memory measurement, {self._stamp}\n\n")
            out.write(f"## {title}\n\n{body}\n\n")


def recall_tool(log: EventLog, found: list[str]) -> ToolHandler:
    """A copy of the Hub's `recall` handler on `log` that keeps each result, so the check can tell where an answer
    came from."""
    async def recall(args: dict[str, Any]) -> str:
        found.append(_recall(log, args))
        return found[-1]
    return recall


def _recall(log: EventLog, args: dict[str, Any]) -> str:
    try:
        start, end = _day(args.get("from")), _day(args.get("to"), following=True)
    except (ValueError, OverflowError):
        return RECALL_DATES
    wanted = args.get("speaker")
    speaker: Literal["user", "jarvis"] | None = wanted if wanted in ("user", "jarvis") else None
    limit = args.get("limit")
    try:
        found = log.search(str(args.get("query") or ""), start=start, end=end, speaker=speaker,
                           limit=limit if isinstance(limit, int) else RECALL_LIMIT)
    except ValueError as e:
        return str(e)
    return found.render()


def _day(value: object, *, following: bool = False) -> float | None:
    if not value:
        return None
    day = date.fromisoformat(str(value))
    return datetime.combine(day + timedelta(days=1) if following else day, datetime.min.time()).timestamp()


def log_of(path: Path, exchanges: Sequence[Exchange], now: float) -> EventLog:
    """An event log holding `exchanges` as the Hub logs a typed line and its reply, at their own times."""
    clock = [now]
    log = EventLog(path, wall=lambda: clock[0])
    for n, e in enumerate(exchanges):
        clock[0] = e.at
        log.append("heard", {"request_id": f"r{n}", "text": e.said, "spoken": False})
        clock[0] = e.ended
        log.append("reply_end", {"reply_id": f"p{n}", "request_id": f"r{n}", "text": e.reply, "reason": "complete"})
    clock[0] = now
    return log


@dataclass(frozen=True)
class Timed:
    """One turn as the User would feel it: time to the first text and to the end, from sending the message."""

    first_text_ms: int | None
    total_ms: int
    text: str
    end: TurnEnd
    # The usage-limit windows the CLI reported during the turn, if any: it reports them only when they change.
    limits: tuple[RateLimitInfo, ...]

    @property
    def context(self) -> int | None:
        return self.end.context.tokens if self.end.context else None

    def usage_cells(self) -> tuple[int, int, int, int]:
        u = self.end.usage
        return u.input_tokens, u.cache_read_tokens, u.cache_creation_tokens, u.output_tokens


class Session:
    def __init__(self, client: ClaudeSDKClient, model: str) -> None:
        self._client = client
        self._model = model

    async def turn(self, message: str) -> Timed:
        """Like `SdkSession.send`, timed, keeping the usage-limit readings it drops."""
        started = time.perf_counter()
        first: int | None = None
        text: list[str] = []
        last_request: dict[str, Any] | None = None
        limits: list[RateLimitInfo] = []
        await self._client.query(message)
        async for m in self._client.receive_response():
            if isinstance(m, ResultMessage):
                end = turn_end(m, self._model, last_request, binding(limits), now=time.time())
                return Timed(first, _ms(started), "".join(text), end, tuple(limits))
            if isinstance(m, RateLimitEvent):
                limits.append(m.rate_limit_info)
            if isinstance(m, SystemMessage) and m.subtype == "init" and isinstance(m.data.get("model"), str):
                self._model = m.data["model"]
            if not isinstance(m, StreamEvent) or m.parent_tool_use_id is not None:
                continue
            if m.event.get("type") == "message_start":
                last_request = m.event.get("message", {}).get("usage") or last_request
            delta: dict[str, Any] = m.event.get("delta", {}) if m.event.get("type") == "content_block_delta" else {}
            if delta.get("type") == "text_delta":
                first = _ms(started) if first is None else first
                text.append(delta.get("text", ""))
        raise AssertionError("the stream ended without a result")


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
