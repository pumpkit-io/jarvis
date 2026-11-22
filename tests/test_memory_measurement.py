"""The pure half of the opt-in Memory measurement (`test_real_memory_measurement.py`): no model is called here."""

from pathlib import Path

import pytest
from claude_agent_sdk import RateLimitEvent, StreamEvent, SystemMessage
from claude_agent_sdk.types import ModelUsage, RateLimitInfo

from jarvis_hub.conversation import CHARS_PER_TOKEN
from jarvis_hub.secret_patterns import find_secret

from tests.memory_measurement import (
    FACTS, Exchange, Results, Session, estimate_error, folded_conversation, log_of, made_up_conversation,
    markdown_table, opted_in, recall_tool, survived_through,
)
from tests.test_sdk_context import ScriptedClient, model_usage, result

START = 1_790_000_000.0
DAY = 86400.0


def text_delta(text: str) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event={
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})


def estimated_tokens(exchanges: list[Exchange]) -> float:
    return sum(len(e.entry().render()) for e in exchanges) / CHARS_PER_TOKEN


def test_a_made_up_conversation_is_about_the_size_asked_for() -> None:
    for tokens in (2_000, 40_000):
        exchanges = made_up_conversation(tokens, language="mixed", start=START, seed=1)

        assert tokens * 0.97 <= estimated_tokens(exchanges) <= tokens * 1.03


def test_an_english_or_italian_conversation_holds_only_that_language() -> None:
    for language in ("en", "it"):
        exchanges = made_up_conversation(5_000, language=language, start=START, seed=2)

        assert {e.lang for e in exchanges} == {language}


def test_a_made_up_conversation_holds_nothing_the_secret_patterns_would_remove() -> None:
    exchanges = made_up_conversation(40_000, language="mixed", start=START, seed=3, facts=FACTS)

    assert [e for e in exchanges if find_secret(e.said) or find_secret(e.reply)] == []


def test_planted_facts_are_said_once_each_in_order_inside_the_block() -> None:
    exchanges = made_up_conversation(20_000, language="mixed", start=START, seed=4, facts=FACTS[:2])

    said = [e.said for e in exchanges]
    first, second = (said.index(fact.statement) for fact in FACTS[:2])
    assert [said.count(fact.statement) for fact in FACTS[:2]] == [1, 1]
    assert 0 < first < second < len(exchanges) - 1
    assert [e.at for e in exchanges] == sorted(e.at for e in exchanges)


def test_no_filler_line_gives_away_a_planted_answer() -> None:
    for seed in range(5):
        text = " ".join(f"{e.said} {e.reply}" for e in made_up_conversation(20_000, language="mixed", start=START,
                                                                              seed=seed)).lower()

        assert [fact.answer for fact in FACTS if fact.answer.lower() in text] == []


def test_each_fold_block_holds_its_own_fact_and_the_whole_lies_within_recalls_thirty_days() -> None:
    now = START + 40 * DAY
    blocks, tail = folded_conversation(4, block_tokens=5_000, tail_tokens=2_000, now=now, seed=5)

    assert [[e.said for e in block if e.said == fact.statement] for block, fact in zip(blocks, FACTS)] == [
        [fact.statement] for fact in FACTS
    ]
    assert not {e.said for e in tail} & {fact.statement for fact in FACTS}
    times = [e.at for block in (*blocks, tail) for e in block]
    assert times == sorted(times)
    assert now - 30 * DAY < times[0] and times[-1] < now


PUPPY = FACTS[0]


def test_a_fact_the_summary_kept_survived_through_the_summary() -> None:
    survived = survived_through(PUPPY, summary="- Sister's puppy: Brunello.", recalled=[],
                                reply="It's called Brunello, sir.")

    assert survived == "summary"


def test_a_fact_only_recall_found_survived_through_recall() -> None:
    survived = survived_through(PUPPY, summary="- Sister got a puppy.", recalled=["<recall>nothing</recall>",
                                '<match from="user">my sister\'s new puppy is called Brunello.</match>'],
                                reply="Brunello, sir.")

    assert survived == "recall"


def test_a_wrong_or_missing_answer_is_lost_even_when_the_summary_held_it() -> None:
    assert survived_through(PUPPY, summary="Brunello", recalled=[], reply="I don't remember, sir.") == "lost"


def test_a_right_answer_with_no_source_is_reported_as_neither() -> None:
    assert survived_through(PUPPY, summary="", recalled=[], reply="brunello?") == "neither"


def test_a_results_table_is_markdown_with_readable_numbers() -> None:
    table = markdown_table(("Size", "Cache", "First delta ms", "Ratio", "Passed", "Error"),
                           [(40_000, "cold", 1834, 3.4567, True, None), (200_000, "warm", None, 2.0, False, "HTTP 500")])

    assert table == (
        "| Size | Cache | First delta ms | Ratio | Passed | Error |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| 40,000 | cold | 1,834 | 3.46 | yes | n/a |\n"
        "| 200,000 | warm | n/a | 2.00 | no | HTTP 500 |"
    )


def test_the_estimate_error_is_how_far_the_starting_ratio_misses_the_counted_tokens() -> None:
    assert estimate_error(chars=7_000, tokens=2_500) == pytest.approx(-0.2)
    assert estimate_error(chars=7_000, tokens=1_600) == pytest.approx(0.25)


def test_results_append_sections_under_one_heading(tmp_path: Path) -> None:
    results = Results(tmp_path / "out" / "run.md", stamp="2026-11-22 10:00")

    results.add("Fixed prefix", "| a |")
    results.add("First reply", "| b |")

    assert results.path.read_text() == (
        "# Talker Memory measurement, 2026-11-22 10:00\n\n## Fixed prefix\n\n| a |\n\n## First reply\n\n| b |\n\n"
    )


def test_only_the_real_model_switch_opts_in() -> None:
    assert opted_in({"JARVIS_REAL_MODEL_TESTS": "1"})
    assert not opted_in({})
    assert not opted_in({"JARVIS_REAL_MODEL_TESTS": "true"})


def test_the_real_measurement_is_marked_real_model_so_it_is_skipped_by_default() -> None:
    from tests import test_real_memory_measurement

    assert test_real_memory_measurement.pytestmark.name == "real_model"


async def test_recall_over_the_made_up_log_finds_the_fact_from_the_first_block(tmp_path: Path) -> None:
    now = START + 40 * DAY
    blocks, tail = folded_conversation(4, block_tokens=5_000, tail_tokens=2_000, now=now, seed=6)
    found: list[str] = []

    with log_of(tmp_path / "events.sqlite3", [e for block in (*blocks, tail) for e in block], now) as log:
        shown = await recall_tool(log, found)({"query": "sister puppy"})
        await recall_tool(log, found)({"query": "puppy", "from": "1 March"})

    assert "Brunello" in shown
    assert found == [shown, "from and to are dates such as 2026-11-15."]


async def test_a_timed_turn_keeps_the_text_the_context_and_the_usage_limit_readings() -> None:
    models: dict[str, ModelUsage] = {"claude-sonnet-5-5": model_usage(1_000_000, 9_000)}
    start = StreamEvent(uuid="u", session_id="s", event={
        "type": "message_start", "message": {"usage": {"input_tokens": 7, "cache_read_input_tokens": 40_000}}})
    reading = RateLimitInfo(status="allowed", rate_limit_type="five_hour", utilization=0.25)
    client = ScriptedClient([
        SystemMessage("init", {"model": "claude-sonnet-5-5"}), start,
        RateLimitEvent(rate_limit_info=reading, uuid="u", session_id="s"),
        text_delta("Brunello"), text_delta(", sir."),
        result(model_usage=models, usage={"input_tokens": 7, "cache_read_input_tokens": 40_000}),
    ])

    timed = await Session(client, "sonnet").turn("hi")  # type: ignore[arg-type]

    assert (timed.text, timed.context, timed.limits, timed.end.outcome) == ("Brunello, sir.", 40_007, (reading,),
                                                                            "completed")
    assert timed.first_text_ms is not None and timed.first_text_ms <= timed.total_ms
