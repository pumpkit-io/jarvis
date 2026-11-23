"""A Fold that fails or meets the Usage limit keeps the Talker's session going and tells the User nothing; only a
context that reaches the ceiling restarts the Talker, from the last good summary and the Tail, with one notice."""

import asyncio
from pathlib import Path

import pytest

from jarvis_hub.config import MemorySettings
from jarvis_hub.hub import FOLD_RETRY_AFTER, SUMMARISER_TIMEOUT
from tests.fakes import (
    AwaitInterrupt,
    Fail,
    FakeBackend,
    FakeClock,
    Gate,
    Limited,
    Text,
    Tool,
    Typist,
    a_summary,
    until,
)
from tests.test_fold import SHORT, a_hub, fold_once, received, tokens


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / ".jarvis" / "conversation.json"


async def say(typist: Typist, backend: FakeBackend, line: str) -> None:
    """Types `line` and waits for the Talker to have read it and the Hub to have settled after its turn."""
    before = received(backend)
    typist.type(line)
    await until(lambda: received(backend) > before)
    await asyncio.sleep(0.01)


async def test_a_failed_fold_keeps_the_session_and_tries_again_quietly_after_five_more_turns(
    clock: FakeClock, path: Path
) -> None:
    turns = [[Text(f"{i}."), tokens(200_000)] for i in range(FOLD_RETRY_AFTER + 1)]
    hub, backend, output, _ = a_hub(clock, path, turns, memory=SHORT, summaries=[[Fail("overloaded")]] * 2)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "Line 0.")
    await until(lambda: len(backend.all_sessions("summariser")) == 2
                and all(s.closed for s in backend.all_sessions("summariser")))
    for i in range(1, FOLD_RETRY_AFTER):
        await say(typist, backend, f"Line {i}.")
    assert len(backend.all_sessions("summariser")) == 2
    await say(typist, backend, f"Line {FOLD_RETRY_AFTER}.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert len(backend.all_sessions("summariser")) == 3
    assert output.notices == [] and output.shown == []
    assert output.replies == [f"{i}." for i in range(FOLD_RETRY_AFTER + 1)]


async def test_a_summariser_that_never_answers_is_a_failed_fold_after_the_timeout(clock: FakeClock, path: Path) -> None:
    never = asyncio.Event()
    turns = [[Text(f"{i}."), tokens(200_000)] for i in range(FOLD_RETRY_AFTER + 1)]
    hub, backend, output, metrics = a_hub(clock, path, turns, memory=SHORT,
                                          summaries=[[Gate(never), Text(a_summary())]])
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "Line 0.")
    await until(lambda: len(backend.all_sessions("summariser")) == 1)
    clock.advance(SUMMARISER_TIMEOUT - 1)
    await asyncio.sleep(0.01)
    assert not backend.all_sessions("summariser")[0].closed
    clock.advance(1)
    await until(lambda: backend.all_sessions("summariser")[0].closed)
    for i in range(1, FOLD_RETRY_AFTER + 1):
        await say(typist, backend, f"Line {i}.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert len(backend.all_sessions("summariser")) == 2
    runs = [r for r in metrics.records if r["role"] == "summariser"]
    assert [(r["attempt"], r["outcome"]) for r in runs] == [(1, "timeout"), (1, "completed")]
    assert runs[0]["duration_ms"] == SUMMARISER_TIMEOUT * 1000 and "error" in runs[0]
    assert output.notices == []


async def test_a_usage_limit_holds_folds_until_its_reset_then_the_next_quiet_moment_starts_one(
    clock: FakeClock, path: Path
) -> None:
    hub, backend, output, metrics = a_hub(
        clock, path, [[Text("One."), tokens(200_000)], [Text("Two."), tokens(210_000)]], memory=SHORT,
        summaries=[[Limited(3600)], [Text(a_summary("Sister: Giulia."))]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await until(lambda: any(r["kind"] == "usage_limit" for r in metrics.records))
    await say(typist, backend, "Still there?")
    assert len(backend.all_sessions("summariser")) == 1
    clock.advance(3600)
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert len(backend.all_sessions("summariser")) == 2
    assert "Sister: Giulia." in backend.all_sessions("talker")[1].received[0]
    assert output.notices == [] and output.replies == ["One.", "Two."]


def conversation_of(seed: str) -> str:
    return seed.split("<conversation>", 1)[1].split("</conversation>", 1)[0]


def memory_of(seed: str) -> str:
    return seed.split("<memory>\n", 1)[1].split("</memory>", 1)[0]


# A Tail that holds one short exchange, so a Fold of three folds two.
ONE_EXCHANGE = MemorySettings(tail=60)
RESTARTED = ("The conversation could not be folded into Memory, so the Talker restarted from the last summary and the "
             "latest exchanges.")
RESTARTED_BARE = "The conversation could not be folded into Memory, so the Talker restarted from the latest exchanges."


async def test_at_the_ceiling_with_folds_still_failing_the_talker_restarts_from_the_last_good_summary_and_the_tail(
    clock: FakeClock, path: Path
) -> None:
    hub, backend, output, _ = a_hub(
        clock, path,
        [[Text("One.")], [Text("Two.")], [Text("Three."), tokens(200_000)], [Text("Four."), tokens(250_000)],
         [Text("Five."), tokens(300_000)], [Text("Six."), tokens(300_000)]],
        memory=ONE_EXCHANGE,
        summaries=[[Text(a_summary("Sister: Giulia."))], [Fail("overloaded")], [Fail("overloaded")]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    for line in ("My sister is Giulia.", "Line two.", "Line three."):
        await say(typist, backend, line)
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    await say(typist, backend, "Line four.")
    await until(lambda: len(backend.all_sessions("summariser")) == 3
                and all(s.closed for s in backend.all_sessions("summariser")))
    await say(typist, backend, "Line five.")
    await until(lambda: len(backend.all_sessions("talker")) == 3)
    await say(typist, backend, "Line six.")
    await until(lambda: len(backend.all_sessions("talker")) == 4)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    restarted = backend.all_sessions("talker")[2].received[0]
    assert restarted.startswith('<event source="hub" reason="restart">')
    assert "Sister: Giulia." in memory_of(restarted)
    assert "Line five." in conversation_of(restarted)
    assert "My sister is Giulia." not in conversation_of(restarted)
    assert output.notices == [RESTARTED]
    assert output.replies == ["One.", "Two.", "Three.", "Four.", "Five.", "Six."]


async def test_a_fold_that_succeeds_ends_the_failing_episode_so_a_later_ceiling_is_told_again(
    clock: FakeClock, path: Path
) -> None:
    waiting = [[Text(f"{i}.")] for i in range(FOLD_RETRY_AFTER - 2)]
    hub, backend, output, _ = a_hub(
        clock, path,
        [[Text("One.")], [Text("Two.")], [Text("Three."), tokens(200_000)], [Text("Full."), tokens(300_000)],
         *waiting, [Text("Fold."), tokens(200_000)], [Text("Full again."), tokens(300_000)]],
        memory=ONE_EXCHANGE, summaries=[[Fail("overloaded")], [Fail("overloaded")], [Text(a_summary("Dog: Fido."))]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    for line in ("My dog is Fido.", "Line two.", "Line three."):
        await say(typist, backend, line)
    await until(lambda: len(backend.all_sessions("summariser")) == 2
                and all(s.closed for s in backend.all_sessions("summariser")))
    await say(typist, backend, "Line four.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    assert output.notices == [RESTARTED_BARE]
    for i in range(len(waiting)):
        await say(typist, backend, f"Waiting {i}.")
    await say(typist, backend, "Fold now.")
    await until(lambda: len(backend.all_sessions("talker")) == 3)
    await say(typist, backend, "Line last.")
    await until(lambda: len(output.notices) == 2)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert "None yet" in memory_of(backend.all_sessions("talker")[1].received[0])
    assert backend.all_sessions("talker")[2].received[0].startswith('<event source="hub" reason="fold">')
    assert output.notices == [RESTARTED_BARE, RESTARTED]


async def test_a_ceiling_restart_seeded_while_the_hub_closes_is_never_swapped_in(clock: FakeClock, path: Path) -> None:
    seeded = asyncio.Event()
    brief = "Look at pumpkit's open PRs."
    spawn = Tool("spawn_subagent", {"repo": "pumpkit", "title": "Open PRs", "brief": brief, "model": "sonnet",
                                    "effort": "medium", "writes": False})
    hub, backend, output, _ = a_hub(
        clock, path, [[Text("On it."), Tool("delegate", {"request": "Check the open PRs"}), tokens(300_000)]],
        orchestrator=[[spawn]], subagents={brief: [AwaitInterrupt()]}, seed_reply=[Gate(seeded), Text("Ready.")],
    )
    backend.subagent_close_gate = asyncio.Event()
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    try:
        typist.type("Check the open PRs")
        await until(lambda: backend.all_sessions("subagent") and len(backend.all_sessions("talker")) == 2)
        typist.end()
        closing = asyncio.create_task(hub.close())
        await asyncio.sleep(0.01)
        seeded.set()
        await until(lambda: backend.all_sessions("talker")[1].closed)
        await asyncio.sleep(0.05)
        old_open = not backend.all_sessions("talker")[0].closed
    finally:
        backend.subagent_close_gate.set()
    await asyncio.wait_for(closing, timeout=1)
    await asyncio.wait_for(running, timeout=1)

    assert old_open
    assert output.notices == []


async def test_after_a_ceiling_restart_folds_still_retry_while_the_saved_conversation_is_past_the_trigger(
    clock: FakeClock, path: Path
) -> None:
    long = "word " * 100
    hub, backend, _, _ = a_hub(
        clock, path,
        [[Text("One.")], [Text("Two.")], [Text("Three."), tokens(200_000)], [Text("Full."), tokens(300_000)]],
        memory=MemorySettings(fold_at=400, tail=60), summaries=[[Fail("overloaded")], [Fail("overloaded")]],
    )
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    for line in ("Line one.", "Line two.", "Line three."):
        await say(typist, backend, line)
    await until(lambda: len(backend.all_sessions("summariser")) == 2
                and all(s.closed for s in backend.all_sessions("summariser")))
    await say(typist, backend, "Line four.")
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    for i in range(FOLD_RETRY_AFTER - 2):
        await say(typist, backend, f"{i} {long}")
    assert len(backend.all_sessions("summariser")) == 2
    await say(typist, backend, f"last {long}")
    await until(lambda: len(backend.all_sessions("talker")) == 3)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert len(backend.all_sessions("summariser")) == 3


async def test_a_summary_that_could_not_be_read_is_never_seeded_as_nothing_folded(clock: FakeClock, path: Path) -> None:
    await fold_once(clock, path, "My sister is Giulia.", summaries=[[Text(a_summary("Sister: Giulia."))]])
    (path.parent / "memory" / "summary-1.json").unlink()

    hub, backend, output, _ = a_hub(clock, path, [])
    await hub.start()
    await until(lambda: received(backend) == 1)
    await hub.close()

    memory = memory_of(backend.all_sessions("talker")[0].received[0])
    assert "nothing has been folded" not in memory
    assert "could not be read" in memory
    assert len(output.notices) == 1


async def test_a_fold_held_by_the_usage_limit_swaps_in_after_the_reset_even_if_nobody_speaks(
    clock: FakeClock, path: Path
) -> None:
    hub, backend, output, metrics = a_hub(clock, path, [[Text("One."), tokens(200_000)]], memory=SHORT,
                                          summaries=[[Limited(3600)], [Text(a_summary("Sister: Giulia."))]])
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await say(typist, backend, "My sister is Giulia.")
    await until(lambda: any(r["kind"] == "usage_limit" for r in metrics.records))
    await asyncio.sleep(0.01)
    clock.advance(3600)
    await until(lambda: len(backend.all_sessions("talker")) == 2)
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert "Sister: Giulia." in backend.all_sessions("talker")[1].received[0]
    assert output.notices == []
