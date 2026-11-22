"""The Talker restarts between turns once its context reaches the ceiling, which only Folds that keep failing let it
reach, seeded with Memory, the Tail of the saved Conversation and the Hub's own records."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from jarvis_hub.agents.backend import Context
from jarvis_hub.config import MemorySettings
from jarvis_hub.hub import RESTART_RETRY_AFTER, Line
from tests.fakes import (
    Fail,
    FakeBackend,
    FakeClock,
    Gate,
    NoContext,
    RecordingMetrics,
    RecordingOutput,
    Text,
    Tool,
    Typist,
    lines,
    make_hub,
    until,
)

WINDOW = 1_000_000


def at(tokens: int) -> Context:
    return Context(tokens, WINDOW)


# The default ceiling, which is past the Fold's trigger too.
FULL = at(300_000)
# A context well below the Fold's trigger.
SMALL = at(30_000)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


def user_messages(*sessions: object) -> list[str]:
    return [m for s in sessions for m in s.received if m.startswith("<user")]  # type: ignore[attr-defined]


async def test_context_at_the_ceiling_restarts_the_talker_with_the_recent_exchanges_then_the_tasks(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    hold_task = asyncio.Event()
    backend = FakeBackend(
        clock,
        [
            [Text("Evening, sir."), SMALL],
            [Text("Right away, sir."), Tool("delegate", {"request": "Check the open PRs"}), FULL],
            [Text("Still on it, sir."), SMALL],
        ],
        orchestrator=[[Gate(hold_task)]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "Hello", "Look at the open PRs", "How is it going?")))
    await until(lambda: len(output.replies) == 3)
    hold_task.set()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    old, new = backend.all_sessions("talker")
    seed = new.received[0]
    assert seed.startswith('<event source="hub"')
    assert "not the User" in seed
    assert "T1" in seed and "Check the open PRs" in seed
    assert "Hello" in seed and "Evening, sir." in seed
    assert "Look at the open PRs" in seed and "Right away, sir." in seed
    assert seed.index("</conversation>") < seed.index("<open_tasks>\n")
    assert old.closed
    assert new.spec.system_prompt == old.spec.system_prompt
    assert user_messages(new) == ['<user lang="en" typed="true">How is it going?</user>']
    assert [m for m in new.received[1:] if m.startswith("<event")] == [
        '<event source="hub" task="T1" needs_user="false">T1 failed: the Orchestrator ended its turn without a report.</event>'
    ]
    assert output.replies[:3] == ["Evening, sir.", "Right away, sir.", "Still on it, sir."]
    assert [r["kind"] for r in metrics.records if r["role"] == "talker"] == [
        "warm_up", "user", "user", "restart", "user", "event",
    ]


async def test_no_turn_is_lost_or_doubled_across_the_restart(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("One."), FULL], [Text("Two.")], [Text("Three."), FULL], [Text("Four.")]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a", "b", "c", "d"))
    await hub.close()

    sessions = backend.all_sessions("talker")
    assert len(sessions) == 3
    assert user_messages(*sessions) == [f'<user lang="en" typed="true">{x}</user>' for x in "abcd"]
    assert [len(user_messages(s)) for s in sessions] == [1, 2, 1]
    assert output.replies == ["One.", "Two.", "Three.", "Four."]


async def test_a_line_typed_during_the_restart_waits_for_the_new_session(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text("One."), FULL], [Text("Two.")]], seed_reply=[Gate(release), Text("Ready.")])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("a")
    await until(lambda: output.replies == ["One."] and len(backend.all_sessions("talker")) == 2)
    typist.type("b")
    typist.end()
    await asyncio.sleep(0.01)
    old, new = backend.all_sessions("talker")
    assert user_messages(new) == [] and not old.closed
    release.set()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    assert user_messages(old) == ['<user lang="en" typed="true">a</user>']
    assert user_messages(new) == ['<user lang="en" typed="true">b</user>']
    assert output.replies == ["One.", "Two."]


async def test_context_below_the_ceiling_keeps_the_session(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("One."), at(299_999)], [Text("Two."), at(299_999)]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a", "b"))
    await hub.close()

    assert len(backend.all_sessions("talker")) == 1


async def test_a_turn_without_a_context_report_never_restarts(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("One.")], [Text("Two.")]])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a", "b"))
    await hub.close()

    assert len(backend.all_sessions("talker")) == 1


async def test_the_ceiling_is_a_setting(clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics) -> None:
    backend = FakeBackend(clock, [[Text("One."), at(250_000)], [Text("Two.")]])
    hub = make_hub(backend, output, metrics, memory=MemorySettings(ceiling=250_000))

    await hub.start()
    await hub.run(lines(clock, "a", "b"))
    await hub.close()

    assert len(backend.all_sessions("talker")) == 2


async def test_a_finished_task_is_not_an_open_task(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("On it."), Tool("delegate", {"request": "Old job"})], [Text("Done, sir."), FULL]],
        orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "Finished."})]],
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "Do the old job"))
    await until(lambda: len(backend.all_sessions("talker")) == 2 and backend.all_sessions("talker")[0].closed)
    await hub.close()

    seed = backend.all_sessions("talker")[1].received[0]
    assert "No open Tasks." in seed
    assert "<you>Done, sir.</you>" in seed


async def test_the_seed_holds_the_newest_exchanges_up_to_40000_tokens(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    # Each line is about 16,000 tokens at 3.5 characters a token, so the newest two fit and three do not.
    words = ["alpha", "bravo", "charlie", "delta"]
    said = [(word + " ") * (56_000 // (len(word) + 1)) for word in words]
    scripts = [[Text("Noted.")] for _ in range(3)] + [[Text("Noted."), FULL], [Text("after.")]]
    backend = FakeBackend(clock, scripts)
    hub = make_hub(backend, output, metrics)

    async def inputs() -> AsyncIterator[Line]:
        for text in [*said, "last"]:
            yield Line(text, clock.now(), lang="en")

    await hub.start()
    await hub.run(inputs())
    await hub.close()

    seed = backend.all_sessions("talker")[1].received[0]
    assert [word for word in words if f"{word} {word}" in seed] == ["charlie", "delta"]


async def test_a_restart_that_cannot_open_keeps_the_session_and_the_conversation_goes_on(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("One."), FULL], [Text("Two."), FULL]], restart_error=RuntimeError("no CLI"))
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a", "b"))
    await hub.close()

    assert output.replies == ["One.", "Two."]
    assert len(backend.all_sessions("talker")) == 1
    assert output.notices == ["The Talker could not restart, so the old session goes on: no CLI"]


async def test_a_failed_restart_waits_some_turns_before_trying_again_and_tells_the_user_once(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    turns = RESTART_RETRY_AFTER + 2
    backend = FakeBackend(clock, [[Text(f"{i}."), FULL] for i in range(turns)], restart_error=RuntimeError("no CLI"))
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, *[str(i) for i in range(turns)]))
    await hub.close()

    # The launch, then a try after the first turn and one more once the back-off has passed.
    assert backend.talker_opens == 3
    assert output.notices == ["The Talker could not restart, so the old session goes on: no CLI"]


async def test_a_failed_seed_is_one_notice_and_waits_like_a_failed_open(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("One."), FULL], [Text("Two."), FULL]], seed_reply=[Fail("overloaded")])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a", "b"))
    await hub.close()

    assert backend.talker_opens == 2
    assert output.notices == ["The Talker could not restart, so the old session goes on: overloaded"]


async def test_a_turn_whose_context_is_unknown_is_logged_and_never_restarts_on_an_older_reading(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    turns = RESTART_RETRY_AFTER + 2
    scripts = [[Text("0."), FULL]] + [[Text(f"{i}."), NoContext("no entry")] for i in range(1, turns)]
    backend = FakeBackend(clock, scripts, restart_error=RuntimeError("no CLI"))
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, *[str(i) for i in range(turns)]))
    await hub.close()

    assert backend.talker_opens == 2
    user_turns = [r for r in metrics.records if r["role"] == "talker" and r["kind"] == "user"]
    assert [r.get("no_context") for r in user_turns] == [None] + ["no entry"] * (turns - 1)


async def test_closing_the_hub_while_a_restart_opens_closes_the_session_once_it_has_opened(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    opened = asyncio.Event()
    backend = FakeBackend(clock, [[Text("One."), FULL]], restart_open_gate=opened)
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a"))
    await until(lambda: backend.talker_opens == 2)
    closing = asyncio.create_task(hub.close())
    await asyncio.sleep(0.01)
    opened.set()
    await asyncio.wait_for(closing, timeout=1)

    old, new = backend.all_sessions("talker")
    assert old.closed and new.closed
    assert new.received == []


async def test_the_seed_lists_each_open_tasks_subagents(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    release = asyncio.Event()
    brief = "Fix the typo in pumpkit's README."
    spawn = Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": "Fix typo X in pumpkit", "brief": brief, "model": "sonnet", "effort": "medium",
         "writes": True, "branch_kind": "fix"},
    )
    backend = FakeBackend(
        clock,
        [[Text("On it."), Tool("delegate", {"request": "Fix typo X", "context": ""})], [Text("Working."), FULL]],
        orchestrator=[[spawn], [Tool("report", {"task_id": "T1", "spoken": "Fixed."})]],
        subagents={brief: [Gate(release), Text("Done.")]},
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("Fix typo X")
    await until(lambda: len(backend.all_sessions("subagent")) == 1)
    typist.type("How is it going?")
    await until(lambda: len(backend.all_sessions("talker")) == 2 and len(backend.all_sessions("talker")[1].received) > 0)
    release.set()
    typist.end()
    await asyncio.wait_for(running, timeout=1)
    await hub.close()

    seed = backend.all_sessions("talker")[1].received[0]
    assert "T1 working" in seed
    assert "\n  S1 running" in seed and "pumpkit on fix/fix-typo-x-in-pumpkit: Fix typo X in pumpkit" in seed


async def test_a_seed_the_new_session_fails_on_keeps_the_old_session_and_closes_the_new_one(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("One."), FULL], [Text("Two.")]], seed_reply=[Fail("overloaded")])
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(lines(clock, "a", "b"))
    await hub.close()

    old, *seeded = backend.all_sessions("talker")
    assert user_messages(old) == [f'<user lang="en" typed="true">{x}</user>' for x in "ab"]
    assert seeded and all(s.closed for s in seeded)
    assert output.replies == ["One.", "Two."]
