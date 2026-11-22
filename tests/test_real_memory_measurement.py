"""Opt-in, spends the User's Max usage: Talker first-reply time at large contexts, the Summariser on a long
Conversation, and whether a planted fact survives several Folds (#201). Run it only on the User's go-ahead, as
`docs/notes/talker-memory.md` says; results go to `.jarvis/measurements/`, ready to paste into those notes."""

import json
import os
import shutil
import time
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeSDKClient

from jarvis_hub import memory
from jarvis_hub.agents.backend import SessionSpec, ToolHandler
from jarvis_hub.agents.sdk import options_for
from jarvis_hub.config import Settings, load_settings
from jarvis_hub.conversation import CHARS_PER_TOKEN, Entry, seed_message
from jarvis_hub.hub import user_message
from jarvis_hub.talker import talker_spec, talker_tools
from jarvis_hub.voice.language import Lang

from tests.conftest import opted_in
from tests.memory_measurement import (
    DAY, FACTS, Exchange, Results, Session, Timed, estimate, estimate_error, fold, folded_conversation, log_of,
    made_up_conversation, markdown_table, recall_tool, refusal, survived_through,
)

pytestmark = pytest.mark.real_model

REPO = Path(__file__).resolve().parents[1]
# Talker context sizes for first-reply time, in tokens the CLI reports.
SIZES = (40_000, 100_000, 200_000)
# User lines sent after each cold seed, each reading the prefix the seed cached.
WARM_LINES: tuple[tuple[str, Lang], ...] = (
    ("Quick one: what were we just talking about? One sentence.", "en"),
    ("E prima di quello, di cosa parlavamo? Una frase.", "it"),
)
SUMMARISER_TOKENS = 100_000
FOLDS = 4
# Smaller than a real Fold's 160,000 tokens (200,000 less the 40,000 Tail) to spend less; the notes say so.
FOLD_BLOCK_TOKENS = 40_000
TAIL_TOKENS = 10_000
RATIO_TOKENS = 15_000


@pytest.fixture(autouse=True)
def refuse_without_the_switch() -> None:
    if not opted_in("real_model", os.environ):
        pytest.fail("refused: this measurement spends the User's Max usage; it needs JARVIS_REAL_MODEL_TESTS=1")
    assert "ANTHROPIC_API_KEY" not in os.environ, "would bill the API, not the Max subscription"


@pytest.fixture(scope="module")
def results() -> Iterator[Results]:
    stamp = datetime.now().astimezone()
    out = Results(REPO / ".jarvis" / "measurements" / f"talker-memory-{stamp:%Y%m%d-%H%M%S}.md",
                  stamp=f"{stamp:%Y-%m-%d %H:%M %Z}")
    yield out
    print(f"\nResults: {out.path}")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(REPO, os.environ)


@asynccontextmanager
async def session(spec: SessionSpec) -> AsyncGenerator[Session]:
    cli = shutil.which("claude")
    assert cli is not None
    async with ClaudeSDKClient(options_for(spec, cli)) as client:
        yield Session(client, spec.model)


async def _noop(args: dict[str, Any]) -> str:
    return "No action was taken."


def talker(settings: Settings, recall: ToolHandler = _noop) -> SessionSpec:
    """The real Talker prompt and tool schemas, so the fixed prefix is the one the Hub sends."""
    tools = talker_tools(delegate=_noop, status=_noop, cancel=_noop, show=_noop, end_conversation=_noop,
                         stand_by=_noop, quiet_mode=_noop, approve=_noop, present_approval=_noop, authorize=_noop,
                         revoke_authorization=_noop, recall=recall, start_afresh=_noop)
    return replace(talker_spec(settings), tools=tools)


def fixed_chars(spec: SessionSpec) -> int:
    """The system prompt, the tool schemas and an empty seed: what every Talker request carries before the talk."""
    tools = sum(len(t.name) + len(t.description) + len(json.dumps(t.input_schema)) for t in spec.tools)
    return len(spec.system_prompt) + tools + len(seed([], None))


def seed(conversation: Sequence[Entry], summary: str | None) -> str:
    """A launch seed with no Tasks, Approvals or spoken scopes."""
    return seed_message("launch", conversation, summary=summary, open_tasks="No open Tasks.", authorizations="",
                        approvals="", scopes="")


def entries(exchanges: Sequence[Exchange]) -> list[Entry]:
    return [e.entry() for e in exchanges]


def run_seed() -> int:
    """A new seed each run, so no earlier run's cache holds the made-up Conversation."""
    return int(time.time())


async def test_the_fixed_prefix_and_the_chars_to_tokens_estimate_for_english_and_italian(
    settings: Settings, results: Results
) -> None:
    spec = talker(settings)
    async with session(spec) as s:
        empty = await s.turn(seed([], None))
    limits(results, "Usage-limit readings: fixed prefix", "empty seed", empty)
    assert empty.context is not None, empty.end
    window = empty.end.context.window if empty.end.context else None
    results.row("Fixed prefix",
                ("Model", "Window", "Context of an empty seed", "Estimated from chars",
                 *(f"Share of {size:,}" for size in SIZES)),
                (spec.model, window, empty.context, estimate(fixed_chars(spec)),
                 *(f"{empty.context / size:.1%}" for size in SIZES)))
    for language in ("en", "it"):
        made_up = entries(made_up_conversation(RATIO_TOKENS, language=language, start=time.time() - 2 * DAY,
                                               seed=run_seed()))
        async with session(spec) as s:
            seeded = await s.turn(seed(made_up, None))
        assert seeded.context is not None, seeded.end
        # The seed with no exchanges says "None yet." where these exchanges go.
        chars = len("\n".join(e.render() for e in made_up)) - len("None yet.")
        tokens = seeded.context - empty.context
        results.row("Chars-to-tokens estimate",
                    ("Language", "Chars", "Tokens counted", "Chars per token",
                     f"Error of {CHARS_PER_TOKEN} chars per token"),
                    (language, chars, tokens, chars / tokens, f"{estimate_error(chars=chars, tokens=tokens):+.1%}"))


async def test_first_reply_time_at_large_contexts_with_the_cache_cold_and_warm(
    settings: Settings, results: Results
) -> None:
    spec = talker(settings)
    fixed = estimate(fixed_chars(spec))
    outcomes: list[str] = []

    def measured(size: int, cache: str, timed: Timed) -> None:
        outcome = timed.end.outcome if timed.end.outcome == "completed" else f"{timed.end.outcome}: {timed.end.error}"
        outcomes.append(outcome)
        results.row("First reply",
                    ("Size", "Context reported", "Cache", "First text ms", "Reply ms", "Input", "Cache read",
                     "Cache write", "Output", "Outcome"),
                    (size, timed.context, cache, timed.first_text_ms, timed.total_ms, *timed.usage_cells(), outcome))
        limits(results, "Usage-limit readings: first reply", f"{size:,} {cache}", timed)

    for size in SIZES:
        made_up = entries(made_up_conversation(size - fixed, language="mixed", start=time.time() - 3 * DAY,
                                               seed=run_seed() + size))
        async with session(spec) as s:
            measured(size, "cold", last := await s.turn(seed(made_up, None)))
            for line, lang in WARM_LINES:
                if last.end.outcome != "completed":
                    break
                measured(size, "warm", last := await s.turn(user_message(line, lang)))
    assert all(outcome == "completed" for outcome in outcomes), outcomes


async def summarise(settings: Settings, previous: str | None, folded: Sequence[Exchange]) -> Timed:
    async with session(memory.summariser_spec(settings)) as s:
        return await s.turn(memory.summariser_message(previous, entries(folded), now=time.time()))


def facts_in(summary: str) -> str:
    return ", ".join(f.answer for f in FACTS if f.answer.lower() in summary.lower()) or "none"


async def test_the_summariser_on_a_long_conversation(settings: Settings, results: Results) -> None:
    made_up = made_up_conversation(SUMMARISER_TOKENS, language="mixed", start=time.time() - 5 * DAY,
                                   seed=run_seed(), facts=FACTS)

    run = await summarise(settings, None, made_up)

    results.row("Summariser",
                ("Input (context reported)", "Duration ms", "Output tokens", "Estimated tokens", "Cap", "Checks",
                 "Planted facts kept", "Outcome"),
                (run.context, run.total_ms, run.end.usage.output_tokens, estimate(len(run.text)), memory.SUMMARY_TOKENS,
                 refusal(run) or "pass", f"{facts_in(run.text)} of {len(FACTS)}", run.end.outcome))
    results.add("Summariser: the summary", f"```markdown\n{run.text}\n```")
    limits(results, "Usage-limit readings: Summariser", "summariser", run)
    assert run.end.outcome == "completed", run.end


async def test_a_fact_from_three_folds_back_survives_through_the_summary_or_recall(
    settings: Settings, results: Results, tmp_path: Path
) -> None:
    now = time.time()
    blocks, tail = folded_conversation(FOLDS, block_tokens=FOLD_BLOCK_TOKENS, tail_tokens=TAIL_TOKENS, now=now,
                                       seed=run_seed())
    summary: str | None = None
    unfolded: list[Exchange] = []
    committed = 0
    readings = "Usage-limit readings: Memory quality"
    for n, block in enumerate(blocks, start=1):
        unfolded += block
        tries: list[Timed] = []

        async def summarised() -> Timed:
            run = await summarise(settings, summary, list(unfolded))
            tries.append(run)
            results.row("Memory quality: folds",
                        ("Fold", "Attempt", "Exchanges folded", "Duration ms", "Output tokens", "Checks",
                         "Planted facts in the summary"),
                        (n, len(tries), len(unfolded), run.total_ms, run.end.usage.output_tokens,
                         refusal(run) or "pass", facts_in(run.text)))
            limits(results, readings, f"fold {n} attempt {len(tries)}", run)
            return run

        done = await fold(summarised)
        # As in the Hub, a Fold with no summary taken leaves its exchanges for the next one.
        if done.summary is not None:
            summary, unfolded, committed = done.summary, [], committed + 1
    results.add("Memory quality: the final summary", f"```markdown\n{summary or 'None: every Fold was refused.'}\n```")
    fact = FACTS[0]
    found: list[str] = []
    with log_of(tmp_path / "events.sqlite3", [e for block in (*blocks, tail) for e in block], now) as log:
        async with session(talker(settings, recall_tool(log, found))) as s:
            seeded = await s.turn(seed(entries([*unfolded, *tail]), summary))
            limits(results, readings, "seed", seeded)
            # Only the question's recall calls count: the seed asks for one word, so any call during it is noise.
            found.clear()
            asked = await s.turn(user_message(fact.question, fact.lang))
            limits(results, readings, "question", asked)
    survived = survived_through(fact, summary=summary or "", recalled=found, reply=asked.text)
    # The first Fold taken folded the fact's block; each one taken after it summarised that summary again.
    folds_since = committed - 1 if committed else None
    results.add("Memory quality: the question", markdown_table(
        ("Fact", "Folds since", "In the final summary", "recall calls", "recall found it", "Answer right",
         "Survived through"),
        [(fact.answer, folds_since, fact.answer.lower() in (summary or "").lower(), len(found),
          any(fact.answer.lower() in r.lower() for r in found), survived != "lost", survived)])
        + f"\n\nAsked: {fact.question}\n\nReply: {asked.text}")
    assert seeded.end.outcome == asked.end.outcome == "completed", (seeded.end, asked.end)


def limits(results: Results, title: str, step: str, timed: Timed) -> None:
    """What the CLI said about the usage-limit windows during the turn `step`, to read how cache reads count."""
    header = ("Turn", "Window", "Status", "Utilization")
    for info in timed.limits:
        results.row(title, header, (step, info.rate_limit_type, info.status, info.utilization))
    if not timed.limits:
        results.row(title, header, (step, "none reported", None, None))
