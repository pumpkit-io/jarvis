"""The Max usage limit through the Hub: what is said, what waits, and what resumes after the reset."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from jarvis_hub.hub import Hub
from jarvis_hub.limits import reset_sentence
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.speech import Speech
from tests.fakes import (
    FakeBackend,
    FakeClock,
    FakeTts,
    Limited,
    RecordingMetrics,
    RecordingOutput,
    RecordingPlayer,
    Text,
    Tool,
    Typist,
    make_hub,
    until,
)

LINE = "I've reached the usage limit, sir."


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The reset time is spoken in local time; the fake clock's epoch is 1000 s past midnight UTC.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


def delegate(request: str, task_id: str | None = None) -> Tool:
    args = {"request": request, "context": ""}
    return Tool("delegate", args if task_id is None else {**args, "task_id": task_id})


def spawn(brief: str) -> Tool:
    return Tool(
        "spawn_subagent",
        {"repo": "pumpkit", "title": brief, "brief": brief, "model": "sonnet", "effort": "medium", "writes": True},
    )


def report(task_id: str, spoken: str) -> Tool:
    return Tool("report", {"task_id": task_id, "spoken": spoken})


def talker_received(backend: FakeBackend) -> list[str]:
    return backend.session("talker").received


async def running(hub: Hub, typist: Typist) -> "asyncio.Task[None]":
    await hub.start()
    return asyncio.create_task(hub.run(typist.lines()))


async def test_a_limited_talker_says_the_cached_line_and_the_reset_time_and_is_not_called_again(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Limited(3600)], [Text("Back at it, sir.")]])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Hello")
    typist.type("Anyone there?")
    await until(lambda: len(output.replies) == 2)

    assert output.replies == [f"{LINE} It resets at 1:16 AM."] * 2
    assert len(talker_received(backend)) == 2  # the warm-up and "Hello": the second line cost no call
    assert not [n for n in output.notices if "failed" in n]

    typist.end()
    clock.advance(3600)
    await run
    await hub.close()


async def test_a_limited_talker_plays_the_cached_clip_then_the_reset_time(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    player = RecordingPlayer()
    speech = Speech(FakeTts(), player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    backend = FakeBackend(clock, [[Limited(3600)]])
    hub = make_hub(backend, output, metrics, speech=speech)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Hello")
    await until(lambda: output.replies)
    await speech.idle()

    assert player.texts == [LINE, "It resets at 1:16 AM."]
    typist.end()
    clock.advance(3600)
    await run
    await hub.close()
    await speech.close()


async def test_an_unknown_reset_time_leaves_the_line_without_one(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Limited(None)]])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Hello")
    await until(lambda: output.replies)

    assert output.replies == [LINE]
    typist.end()
    clock.advance(15 * 60)
    await run
    await hub.close()


async def test_the_reset_is_announced_once_and_the_talker_is_called_again(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Limited(3600)], [Text("The limit has reset, sir.")], [Text("Hello again, sir.")]])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Hello")
    await until(lambda: output.replies)

    clock.advance(3600)
    await until(lambda: len(output.replies) == 2)
    typist.type("Hi")
    await until(lambda: len(output.replies) == 3)

    assert output.replies[1:] == ["The limit has reset, sir.", "Hello again, sir."]
    resets = [m for m in talker_received(backend) if 'reason="usage_reset"' in m]
    assert len(resets) == 1
    assert [r["kind"] for r in metrics.records if r["kind"].startswith("usage")] == ["usage_limit", "usage_reset"]
    typist.end()
    await run
    await hub.close()


async def test_news_a_limited_talker_could_not_tell_is_told_after_the_reset(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Check the CI")],
            [Limited(600)],
            [Text("The CI is green, sir.")],
            [Text("The limit has reset, sir.")],
        ],
        orchestrator=[[report("T1", "The CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in output.replies))
    typist.end()
    await asyncio.sleep(0.05)
    assert not run.done()

    clock.advance(600)
    await run

    assert output.replies[-2:] == ["The CI is green, sir.", "The limit has reset, sir."]
    assert sum("The CI is green." in m for m in talker_received(backend)) == 2
    await hub.close()


async def test_a_limited_orchestrator_is_announced_once_and_its_task_resumes_after_the_reset(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Check the CI")],
            [Text("The limit has reset, sir.")],
            [Text("The CI is green, sir.")],
        ],
        orchestrator=[[Limited(1800)], [report("T1", "The CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in output.replies))

    assert output.replies[-1] == f"{LINE} It resets at 12:46 AM."
    [session] = backend.all_sessions("orchestrator")
    assert len(session.received) == 1
    typist.end()
    await asyncio.sleep(0.05)
    assert not run.done()  # the Task still waits, so the run does too

    clock.advance(1800)
    await run

    assert len(session.received) == 2 and session.received[0] == session.received[1]
    assert sum(LINE in r for r in output.replies) == 1
    assert output.replies[-2:] == ["The limit has reset, sir.", "The CI is green, sir."]
    assert not [n for n in output.notices if "failed" in n]
    await hub.close()


async def test_a_task_waiting_for_the_reset_says_so_in_its_status(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("Right away, sir."), delegate("Check the CI")], [Tool("status", {})]],
        orchestrator=[[Limited(1800)]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in output.replies))

    typist.type("Status?")
    await until(lambda: len(backend.session("talker").tool_results) == 2)

    assert "waiting for the usage limit to reset" in (backend.session("talker").tool_results[1].result or "")
    typist.end()
    clock.advance(1800)
    await run
    await hub.close()


async def test_a_subagent_started_while_limited_queues_and_starts_after_the_reset(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    first, second = "Fix the first typo.", "Fix the second typo."
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Fix the typos")],
            [Text("A second one, sir."), delegate("Fix another", task_id="T1")],
            [Text("The limit has reset, sir.")],
            [Text("Both are done, sir.")],
            [Text("Both are done, sir.")],
        ],
        orchestrator=[[spawn(first)], [spawn(second)], [report("T1", "First done.")], [report("T1", "Second done.")]],
        subagents={first: [Limited(600)], "The usage limit has reset. Carry on from where you stopped.": [Text("Done.")]},
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Fix the typos")
    await until(lambda: any(LINE in r for r in output.replies))
    typist.type("And another")
    await until(lambda: len([r for s in backend.all_sessions("orchestrator") for r in s.tool_results]) == 2)

    spawned = [json.loads(r.result or "{}") for s in backend.all_sessions("orchestrator") for r in s.tool_results]
    assert spawned[1]["state"] == "queued"
    await asyncio.sleep(0.05)
    assert len(backend.all_sessions("subagent")) == 1

    typist.end()
    clock.advance(600)
    await run

    started, queued = backend.all_sessions("subagent")
    assert started.received == [first, "The usage limit has reset. Carry on from where you stopped."]
    assert queued.received == [second]
    assert sum(LINE in r for r in output.replies) == 1
    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 1
    await hub.close()


async def test_every_result_logs_its_usage_and_duration_per_role(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    from jarvis_hub.agents.backend import Usage
    from tests.fakes import Wait

    brief = "Fix the typo."
    backend = FakeBackend(
        clock,
        [[Usage(10, 20, 30, 40), Wait(2), Text("Right away, sir."), delegate("Fix it")], [Text("Done, sir.")]],
        orchestrator=[[Usage(1, 2, 3, 4), Wait(3), spawn(brief)], [report("T1", "Done.")]],
        subagents={brief: [Usage(5, 6, 7, 8), Wait(4), Text("Fixed.")]},
    )
    hub = make_hub(backend, output, metrics)

    await hub.start()
    await hub.run(_one_line(clock, "Fix it"))
    await hub.close()

    rows: dict[str, dict[str, Any]] = {}
    for r in metrics.records:
        if r.get("kind") in ("user", "task", "run"):
            rows.setdefault(r["role"], r)
    assert {role: (r["usage"], r["duration_ms"]) for role, r in rows.items()} == {
        "talker": (dict(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_creation_tokens=40), 2000),
        "orchestrator": (dict(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_creation_tokens=4), 3000),
        "subagent": (dict(input_tokens=5, output_tokens=6, cache_read_tokens=7, cache_creation_tokens=8), 4000),
    }


async def _one_line(clock: FakeClock, text: str) -> AsyncIterator[Any]:
    from jarvis_hub.hub import Line

    yield Line(text, clock.now())


@pytest.mark.parametrize(
    ("resets_at", "lang", "expected"),
    [
        (4600, "en", "It resets at 1:16 AM."),
        (3600 * 15, "en", "It resets at 3 PM."),
        (3600 * 12, "en", "It resets at 12 PM."),
        (3600 * 24 + 60, "en", "It resets on Friday at 12:01 AM."),
        (3600 * 15 + 40 * 60, "it", "Si riazzera alle 15:40."),
        (3600 * 15, "it", "Si riazzera alle 15."),
        (3600 * 24 * 3 + 3600 * 9, "en", "It resets on Sunday at 9 AM."),
        (3600 * 24 * 3 + 3600 * 9, "it", "Si riazzera domenica alle 9."),
        (None, "en", ""),
    ],
)
def test_the_reset_time_is_spoken_in_local_time_and_names_the_day_when_it_is_far_off(
    resets_at: float | None, lang: Lang, expected: str
) -> None:
    assert reset_sentence(resets_at, lang, now=1000) == expected
