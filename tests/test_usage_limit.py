"""The Max usage limit through the Hub: what is said, what waits, and what resumes after the reset."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.hub import Hub
from jarvis_hub.agents.backend import TurnEnd
from jarvis_hub.limits import Notice, UsageLimit, reset_sentence
from jarvis_hub.voice.language import Lang
from jarvis_hub.voice.speech import Speech
from tests.fakes import (
    FakeBackend,
    FakeClock,
    FakeTts,
    Gate,
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

LINE = "Model unavailable: usage limit."
NEXT = "Held work retries automatically; try your request again after reset."


def status(reset: str = "Reset time unavailable.") -> str:
    return f"{LINE} {reset} {NEXT}"


def screen_lines(output: RecordingOutput) -> list[str]:
    out: list[str] = []
    current = ""
    for kind, text in output.log:
        if kind == "reply":
            current += text
        elif kind == "end" or (kind == "notice" and text.startswith(LINE)):
            if current:
                out.append(current)
                current = ""
            if kind == "notice":
                out.append(text)
    return out


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
    await until(lambda: len(screen_lines(output)) == 2)

    assert screen_lines(output) == [status("It resets at 1:16 AM.")] * 2
    assert len(talker_received(backend)) == 2  # the warm-up and "Hello": the second line cost no call
    assert not [n for n in output.notices if "failed" in n]

    typist.end()
    clock.advance(3600)
    await run
    await hub.close()


async def test_a_limited_talker_shows_status_without_cached_or_generated_failure_speech(
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
    await until(lambda: any("Model unavailable: usage limit" in n for n in output.notices))
    await speech.idle()

    assert player.texts == []
    assert not any("usage limit" in r for r in output.replies)
    assert any("It resets at 1:16 AM." in n and "try your request again" in n for n in output.notices)
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
    await until(lambda: screen_lines(output))

    assert screen_lines(output) == [status()]
    typist.end()
    clock.advance(15 * 60)
    await run
    await hub.close()


async def test_the_reset_is_announced_once_a_held_talker_completes_a_turn_not_when_the_hold_clears(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Limited(3600)], [Text("Hello again, sir.")], [Text("The limit has reset, sir.")]])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Hello")
    await until(lambda: screen_lines(output))

    clock.advance(3600)
    await until(lambda: any(r["kind"] == "usage_reset" for r in metrics.records))
    await asyncio.sleep(0.05)
    assert len(screen_lines(output)) == 1  # the hold cleared, but nothing has resumed yet
    assert not any("recovery confirmed" in n for n in output.notices)
    typist.type("Hi")
    await until(lambda: len(screen_lines(output)) == 3)

    assert screen_lines(output)[1:] == ["Hello again, sir.", "The limit has reset, sir."]
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
    await until(lambda: any(LINE in r for r in screen_lines(output)))
    typist.end()
    await asyncio.sleep(0.05)
    assert not run.done()

    clock.advance(600)
    await run

    assert screen_lines(output)[-2:] == ["The CI is green, sir.", "The limit has reset, sir."]
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
    await until(lambda: any(LINE in r for r in screen_lines(output)))

    assert screen_lines(output)[-1] == status("It resets at 12:46 AM.")
    [session] = backend.all_sessions("orchestrator")
    assert len(session.received) == 1
    typist.end()
    await asyncio.sleep(0.05)
    assert not run.done()  # the Task still waits, so the run does too

    clock.advance(1800)
    await run

    assert len(session.received) == 2 and session.received[0] == session.received[1]
    assert sum(LINE in r for r in screen_lines(output)) == 1
    assert screen_lines(output)[-2:] == ["The limit has reset, sir.", "The CI is green, sir."]
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
    await until(lambda: any(LINE in r for r in screen_lines(output)))

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
    await until(lambda: any(LINE in r for r in screen_lines(output)))
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
    assert sum(LINE in r for r in screen_lines(output)) == 1
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
        (3600, "it", "Si riazzera all'una."),
        (3600 * 13, "it", "Si riazzera alle 13."),
        (3600 + 20 * 60, "it", "Si riazzera all'1:20."),
        (3600 * 24, "it", "Si riazzera venerdì a mezzanotte."),
        (3600 * 12, "it", "Si riazzera a mezzogiorno."),
        (3600 * 24 * 3 + 3600 * 9, "en", "It resets on Sunday at 9 AM."),
        (3600 * 24 * 3 + 3600 * 9, "it", "Si riazzera domenica alle 9."),
        (None, "en", ""),
    ],
)
def test_the_reset_time_is_spoken_in_local_time_and_names_the_day_when_it_is_far_off(
    resets_at: float | None, lang: Lang, expected: str
) -> None:
    assert reset_sentence(resets_at, lang, now=1000) == expected


async def stopped(hub: Hub, run: "asyncio.Task[None]") -> None:
    """Closes the Hub as a restart would, with the input still open."""
    await hub.close()
    run.cancel()
    await asyncio.wait([run])


async def test_a_usage_limit_and_its_reset_time_survive_a_restart(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    before = RecordingOutput()
    first = make_hub(FakeBackend(clock, [[Limited(3600)]]), before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Hello")
    await until(lambda: screen_lines(before))
    await stopped(first, run)
    clock.advance(1800)

    output = RecordingOutput()
    backend = FakeBackend(clock, [[Text("Hello again, sir.")], [Text("The limit has reset, sir.")]])
    second = make_hub(backend, output, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(second, typist)
    typist.type("Hi")
    await until(lambda: len(screen_lines(output)) == 2)

    assert screen_lines(output) == [status("It resets at 1:16 AM.")] * 2
    assert len(talker_received(backend)) == 1  # only the warm-up: the limited Talker is not called

    clock.advance(1800)
    await until(lambda: any(r["kind"] == "usage_reset" for r in metrics.records))
    typist.type("Hi again")
    await until(lambda: len([r for r in output.replies if r]) == 2)
    assert [r for r in output.replies if r] == ["Hello again, sir.", "The limit has reset, sir."]
    typist.end()
    await run
    await second.close()


async def test_news_the_limited_talker_could_not_tell_survives_a_restart_and_is_told_after_the_reset(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    before = RecordingOutput()
    first_backend = FakeBackend(
        clock,
        [[Text("Right away, sir."), delegate("Check the CI")], [Limited(600)]],
        orchestrator=[[report("T1", "The CI is green.")]],
    )
    first = make_hub(first_backend, before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in screen_lines(before)))
    await stopped(first, run)

    output = RecordingOutput()
    backend = FakeBackend(clock, [[Text("The CI is green, sir.")], [Text("The limit has reset, sir.")]])
    second = make_hub(backend, output, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(second, typist)
    typist.end()
    await asyncio.sleep(0.05)
    assert not run.done() and len(talker_received(backend)) == 1

    clock.advance(600)
    await run

    assert output.replies == ["The CI is green, sir.", "The limit has reset, sir."]
    assert "The CI is green." in talker_received(backend)[1]
    await second.close()


async def test_an_orchestrator_message_held_for_the_limit_survives_a_restart_and_goes_after_the_reset(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    before = RecordingOutput()
    first_backend = FakeBackend(
        clock, [[Text("Right away, sir."), delegate("Check the CI")]], orchestrator=[[Limited(1800)]]
    )
    first = make_hub(first_backend, before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in screen_lines(before)))
    [task_message] = first_backend.session("orchestrator").received
    await stopped(first, run)

    output = RecordingOutput()
    warm_ready, recovered = asyncio.Event(), asyncio.Event()
    ready: list[bool] = []
    backend = FakeBackend(
        clock,
        [[Text("The limit has reset, sir.")], [Text("The CI is green, sir.")]],
        warm_up=[Gate(warm_ready), Text("Ready.")],
        orchestrator=[[Gate(recovered), report("T1", "The CI is green.")]],
    )
    second = make_hub(backend, output, metrics, state_path=state, on_model_ready=lambda: ready.append(True))
    typist = Typist(clock)
    run = await running(second, typist)
    typist.end()
    await asyncio.sleep(0.05)
    assert backend.all_sessions("orchestrator") == []

    clock.advance(1800)
    await until(lambda: bool(backend.all_sessions("orchestrator")))
    warm_ready.set()
    await until(lambda: sum(r.get("kind") == "warm_up" for r in metrics.records) == 2)
    assert ready == []
    assert not any("recovery confirmed" in n for n in output.notices)
    recovered.set()
    await run
    assert any("recovery confirmed" in n for n in output.notices)

    [recovery] = backend.session("orchestrator").received
    assert recovery.startswith('<event source="hub" reason="restart">')
    assert f'<unanswered task="T1">{task_message}</unanswered>' in recovery
    assert output.replies == ["The limit has reset, sir.", "The CI is green, sir."]
    await second.close()


async def test_a_subagent_queued_for_the_limit_survives_a_restart_and_starts_after_the_reset(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    first_brief, second_brief = "Fix the first typo.", "Fix the second typo."
    before = RecordingOutput()
    first_backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Fix the typos")],
            [Text("A second one, sir."), delegate("Fix another", task_id="T1")],
        ],
        orchestrator=[[spawn(first_brief)], [spawn(second_brief)]],
        subagents={first_brief: [Limited(600)]},
    )
    first = make_hub(first_backend, before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Fix the typos")
    await until(lambda: any(LINE in r for r in screen_lines(before)))
    typist.type("And another")
    await until(lambda: len(first_backend.session("orchestrator").tool_results) == 2)
    await stopped(first, run)
    saved = {s["id"]: s["state"] for s in json.loads(state.read_text())["subagents"]}
    assert saved == {"S1": "interrupted", "S2": "queued"}

    output = RecordingOutput()
    backend = FakeBackend(
        clock,
        orchestrator=[[report("T1", "The first fix was cut off; the second waits.")], [report("T1", "Done.")]],
        subagents={second_brief: [Text("Fixed.")]},
    )
    second = make_hub(backend, output, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(second, typist)
    typist.end()
    await until(lambda: backend.all_sessions("orchestrator") != [])
    await asyncio.sleep(0.05)
    recovery = backend.session("orchestrator").received[0]
    assert '<interrupted id="S1"' in recovery and "S2 queued" in recovery
    assert backend.all_sessions("subagent") == []

    clock.advance(600)
    await run

    [queued] = backend.all_sessions("subagent")
    assert queued.received == [second_brief]
    assert backend.session("orchestrator").received[1].startswith('<event source="subagent" id="S2" task="T1"')
    await second.close()


async def test_a_retry_that_keeps_being_limited_is_silent_and_the_reset_is_told_once_a_retry_completes(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Check the CI")],
            [Text("The CI is green, sir.")],
            [Text("The limit has reset, sir.")],
        ],
        orchestrator=[[Limited(-30)], [Limited(-30)], [Limited(-30)], [report("T1", "The CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in screen_lines(output)))
    [session] = backend.all_sessions("orchestrator")
    typist.end()

    async def calls_after(seconds: float) -> int:
        clock.advance(seconds)
        await asyncio.sleep(0.05)
        return len(session.received)

    assert [await calls_after(s) for s in (59, 1, 119, 1, 239, 1)] == [1, 2, 2, 3, 3, 4]
    await run

    assert sum(LINE in r for r in screen_lines(output)) == 1
    assert not any("expected to reset" in r for r in screen_lines(output))
    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 1
    assert screen_lines(output)[-2:] == ["The CI is green, sir.", "The limit has reset, sir."]
    await hub.close()


async def test_a_limit_after_a_successful_turn_is_a_new_one_and_is_announced_again(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Check the CI")],
            [Text("The limit has reset, sir.")],
            [Text("The CI is green, sir.")],
            [Text("On it, sir."), delegate("Check the PRs")],
            [Text("The limit has reset, sir.")],
            [Text("No open PRs, sir.")],
        ],
        orchestrator=[
            [Limited(600)],
            [report("T1", "The CI is green.")],
            [Limited(600)],
            [report("T2", "No open PRs.")],
        ],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in screen_lines(output)))
    clock.advance(600)
    await until(lambda: "The CI is green, sir." in screen_lines(output))

    typist.type("Any PRs?")
    await until(lambda: sum(LINE in r for r in screen_lines(output)) == 2)
    clock.advance(600)
    typist.end()
    await run

    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 2
    await hub.close()


async def test_a_talker_limit_already_past_still_holds_for_a_minute(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Limited(-30)], [Text("Back at it, sir.")]])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Hello")
    await until(lambda: screen_lines(output))
    typist.type("Anyone there?")
    await until(lambda: len(screen_lines(output)) == 2)

    assert screen_lines(output) == [status("It resets at 12:17 AM.")] * 2
    assert len(talker_received(backend)) == 2
    typist.end()
    clock.advance(60)
    await run
    await hub.close()


async def test_a_retry_limited_again_after_a_restart_is_silent_and_the_reset_is_told_when_one_completes(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    before = RecordingOutput()
    first_backend = FakeBackend(
        clock, [[Text("Right away, sir."), delegate("Check the CI")]], orchestrator=[[Limited(600)]]
    )
    first = make_hub(first_backend, before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in screen_lines(before)))
    await stopped(first, run)

    output = RecordingOutput()
    backend = FakeBackend(
        clock,
        [[Text("The CI is green, sir.")], [Text("The limit has reset, sir.")]],
        orchestrator=[[Limited(-30)], [report("T1", "The CI is green.")]],
    )
    second = make_hub(backend, output, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(second, typist)
    typist.end()
    clock.advance(600)
    await until(lambda: [len(s.received) for s in backend.all_sessions("orchestrator")] == [1])
    await asyncio.sleep(0.05)
    clock.advance(120)
    await run

    assert sum(LINE in n for n in output.notices) == 1
    assert not any("expected to reset" in n for n in output.notices)
    assert output.replies == ["The CI is green, sir.", "The limit has reset, sir."]
    await second.close()


@pytest.mark.parametrize("speak", [False, True], ids=["text", "spoken"])
async def test_limit_status_during_a_reply_does_not_inject_speech(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speak: bool
) -> None:
    player = RecordingPlayer()
    speech = Speech(FakeTts(), player, clock, metrics=metrics, notice=output.notice) if speak else None
    if speech is not None:
        speech.start()
    hit = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("Right away, sir."), delegate("Check the CI"), Gate(hit), Text("I'll let you know.")]],
        orchestrator=[[Limited(600)]],
    )
    hub = make_hub(backend, output, metrics, speech=speech)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Is the CI green?")
    await until(lambda: any(r.get("kind") == "usage_limit" for r in metrics.records))
    await asyncio.sleep(0.05)
    hit.set()
    await until(lambda: output.replies == ["Right away, sir. I'll let you know."])

    assert output.replies == ["Right away, sir. I'll let you know."]
    assert status("It resets at 12:26 AM.") in output.notices
    if speech is not None:
        await speech.idle()
        assert player.texts == ["Right away, sir.", "I'll let you know."]
    typist.end()
    clock.advance(600)
    await run
    await hub.close()
    if speech is not None:
        await speech.close()


async def test_a_limit_a_day_after_the_last_one_cleared_is_a_new_episode_even_if_its_role_never_completed(
    clock: FakeClock,
) -> None:
    hits: list[tuple[str, bool]] = []
    clears: list[frozenset[str]] = []
    resets: list[None] = []
    limit = UsageLimit(
        clock,
        clock.now,
        lambda role, news: hits.append((role, news)),
        clears.append,
        lambda: resets.append(None),
    )

    limit.observe("subagent", TurnEnd("limited", resets_at=clock.now() + 600))
    limit.tell_news()
    clock.advance(600)
    await until(lambda: len(clears) == 1)
    limit.observe("orchestrator", TurnEnd("completed"))
    clock.advance(86400)
    limit.observe("subagent", TurnEnd("limited", resets_at=clock.now() - 30))

    assert hits == [("subagent", True), ("subagent", True)]
    assert resets == []
    assert limit.resets_at("subagent") == clock.now() + 60
    limit.tell_news()
    clock.advance(60)
    await until(lambda: len(clears) == 2)
    await limit.close()


async def test_a_subagent_cancelled_while_it_waits_for_the_reset_does_not_keep_the_episode_open(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("On it, sir."), delegate("Fix A")],
            [Text("Cancelling."), Tool("cancel", {"task_id": "T1"})],
            [Text("Stopped, sir.")],
            [Text("On it, sir."), delegate("Fix B")],
        ],
        orchestrator=[[spawn("A")], [spawn("B")]],
        subagents={"A": [Limited(600)], "B": [Limited(-30)]},
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Fix A")
    await until(lambda: sum(LINE in r for r in screen_lines(output)) == 1)
    typist.type("Cancel that")
    await until(lambda: "Stopped, sir." in screen_lines(output))
    clock.advance(600)
    await until(lambda: any(r["kind"] == "usage_reset" for r in metrics.records))

    clock.advance(86400)
    typist.type("Fix B")
    await until(lambda: sum(LINE in r for r in screen_lines(output)) == 2)

    # The minimum wait, not a doubled one: 88,060 s past the epoch.
    assert screen_lines(output)[-1] == status("It resets at 12:27 AM.")
    typist.end()
    clock.advance(60)
    await run
    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 1
    await hub.close()


async def test_a_task_cancelled_while_its_turn_waits_for_the_reset_does_not_keep_the_episode_open(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("On it, sir."), delegate("Check the CI")],
            [Text("Cancelling."), Tool("cancel", {"task_id": "T1"})],
            [Text("On it, sir."), delegate("Check the PRs")],
        ],
        orchestrator=[[Limited(600)], [Limited(-30)]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Is the CI green?")
    await until(lambda: sum(LINE in r for r in screen_lines(output)) == 1)
    typist.type("Cancel that")
    await until(lambda: "Cancelling." in screen_lines(output))
    clock.advance(600)
    await until(lambda: any(r["kind"] == "usage_reset" for r in metrics.records))

    clock.advance(86400)
    typist.type("Any PRs?")
    await until(lambda: sum(LINE in r for r in screen_lines(output)) == 2)

    assert screen_lines(output)[-1] == status("It resets at 12:27 AM.")
    typist.end()
    clock.advance(60)
    await run
    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 1
    await hub.close()


async def test_a_limit_hit_by_the_warm_up_is_told_at_the_first_user_turn(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, warm_up=[Limited(3600)])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    await until(lambda: any(r.get("kind") == "usage_limit" for r in metrics.records))

    typist.type("Hello")
    await until(lambda: screen_lines(output))

    assert screen_lines(output) == [status("It resets at 1:16 AM.")]
    typist.end()
    clock.advance(3600)
    await run
    await hub.close()


async def test_a_warm_up_limit_shows_status_until_a_user_turn_confirms_recovery(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(clock, [[Text("Hello, sir.")], [Text("Recovered, sir.")]], warm_up=[Limited(-30)])
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    await until(lambda: any(r.get("kind") == "usage_limit" for r in metrics.records))
    clock.advance(60)
    await until(lambda: any(r.get("kind") == "usage_reset" for r in metrics.records))

    typist.type("Hello")
    typist.end()
    await run

    assert output.replies == ["Hello, sir.", "Recovered, sir."]
    assert sum(LINE in n for n in output.notices) == 1
    assert any('reason="usage_reset"' in m for m in talker_received(backend))
    assert any("recovery confirmed" in n for n in output.notices)
    await hub.close()


async def test_a_notice_held_past_the_hold_that_started_it_tells_the_hold_still_on_and_its_reset(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    replying, orchestrating = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it, sir."), delegate("Fix A"), Gate(replying), Text("Working on it.")], [Text("Reset, sir.")]],
        orchestrator=[[spawn("A"), Gate(orchestrating), Limited(None)]],
        subagents={"A": [Limited(-30)]},
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Fix A")

    def hits() -> list[str]:
        return [r["role"] for r in metrics.records if r.get("kind") == "usage_limit"]

    await until(lambda: hits() == ["subagent"])
    orchestrating.set()
    await until(lambda: hits() == ["subagent", "orchestrator"])
    clock.advance(60)
    await until(lambda: any(r.get("kind") == "usage_reset" for r in metrics.records))
    replying.set()
    await until(lambda: output.replies == ["On it, sir. Working on it."])

    assert output.replies == ["On it, sir. Working on it."]
    assert status() in output.notices
    assert sum(LINE in n for n in output.notices) == 2

    clock.advance(840)
    await until(lambda: any('reason="usage_reset"' in m for m in talker_received(backend)))
    await stopped(hub, run)


async def test_a_later_hit_that_pushes_the_reset_back_tells_the_user_once(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    orchestrating = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text("On it, sir."), delegate("Fix A")]],
        orchestrator=[[spawn("A"), Gate(orchestrating), Limited(3600)]],
        subagents={"A": [Limited(600)]},
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Fix A")
    await until(lambda: any(LINE in r for r in screen_lines(output)))
    orchestrating.set()
    await until(lambda: len(screen_lines(output)) == 3)
    await asyncio.sleep(0.05)

    assert screen_lines(output) == [
        "On it, sir.",
        status("It resets at 12:26 AM."),
        status("It's now expected to reset at 1:16 AM."),
    ]
    await stopped(hub, run)


async def test_visible_limit_status_is_restored_once_after_a_restart_while_it_holds(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    before = RecordingOutput()
    never = asyncio.Event()
    first_backend = FakeBackend(
        clock, [[Text("On it, sir."), delegate("Check the CI"), Gate(never)]], orchestrator=[[Limited(600)]]
    )
    first = make_hub(first_backend, before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(r.get("kind") == "usage_limit" for r in metrics.records))
    await stopped(first, run)
    assert any(LINE in r for r in screen_lines(before))

    output = RecordingOutput()
    backend = FakeBackend(clock, [[Text("Reset, sir.")]])
    second = make_hub(backend, output, metrics, state_path=state)
    run = await running(second, Typist(clock))
    await until(lambda: backend.all_sessions("talker") != [])
    await asyncio.sleep(0.05)

    assert sum(LINE in n for n in output.notices) == 1
    await stopped(second, run)


async def test_a_visible_limit_is_announced_recovered_after_a_successful_restart_turn(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    never = asyncio.Event()
    first_backend = FakeBackend(
        clock, [[Text("On it, sir."), delegate("Check the CI"), Gate(never)]], orchestrator=[[Limited(600)]]
    )
    first = make_hub(first_backend, RecordingOutput(), metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(r.get("kind") == "usage_limit" for r in metrics.records))
    await stopped(first, run)
    clock.advance(600)

    output = RecordingOutput()
    backend = FakeBackend(clock, [[Text("Reset, sir.")]], orchestrator=[[Text("Checking.")]])
    second = make_hub(backend, output, metrics, state_path=state)
    run = await running(second, Typist(clock))
    await until(lambda: sum(r.get("kind") == "usage_reset" for r in metrics.records) == 1)
    await asyncio.sleep(0.05)

    assert sum(LINE in n for n in output.notices) == 1
    assert any('reason="usage_reset"' in m for m in talker_received(backend))
    assert any("recovery confirmed" in n for n in output.notices)
    await stopped(second, run)


async def test_the_notice_names_the_latest_reset_among_the_held_roles_and_drops_once_none_holds(
    clock: FakeClock,
) -> None:
    hits: list[tuple[str, bool]] = []
    limit = UsageLimit(clock, clock.now, lambda role, news: hits.append((role, news)), lambda roles: None, lambda: None)

    limit.observe("subagent", TurnEnd("limited", resets_at=clock.now() + 600))
    limit.observe("orchestrator", TurnEnd("limited", resets_at=clock.now() + 1800))
    assert limit.tell_news() == Notice(clock.now() + 1800, update=False)
    assert limit.tell_news() is None

    limit.observe("talker", TurnEnd("limited"))
    assert hits == [("subagent", True), ("orchestrator", True), ("talker", True)]
    assert limit.tell_news() == Notice(None, update=True)

    clock.advance(1800)
    await until(lambda: not limit.active)
    assert limit.tell_news() is None
    await limit.close()


async def test_a_reset_is_announced_when_a_held_role_completes_and_a_hit_after_it_is_news_again(
    clock: FakeClock,
) -> None:
    hits: list[tuple[str, bool]] = []
    clears: list[frozenset[str]] = []
    resets: list[None] = []
    limit = UsageLimit(
        clock,
        clock.now,
        lambda role, news: hits.append((role, news)),
        clears.append,
        lambda: resets.append(None),
    )

    limit.observe("orchestrator", TurnEnd("limited"))
    assert limit.tell_news() == Notice(None, update=False)
    clock.advance(900)
    await until(lambda: len(clears) == 1)
    assert resets == []  # the hold ran out, but nothing has resumed

    limit.observe("orchestrator", TurnEnd("limited"))
    assert hits == [("orchestrator", True), ("orchestrator", False)]
    assert limit.tell_news() is None
    clock.advance(900)
    await until(lambda: len(clears) == 2)
    assert resets == []

    limit.observe("orchestrator", TurnEnd("completed"))
    limit.observe("orchestrator", TurnEnd("completed"))
    assert resets == [None]

    limit.observe("orchestrator", TurnEnd("limited"))
    assert hits[-1] == ("orchestrator", True)
    assert limit.tell_news() == Notice(None, update=False)
    await limit.close()


async def test_a_retry_that_reports_a_later_reset_is_one_update_and_a_past_one_is_none(clock: FakeClock) -> None:
    hits: list[tuple[str, bool]] = []
    limit = UsageLimit(clock, clock.now, lambda role, news: hits.append((role, news)), lambda roles: None, lambda: None)

    limit.observe("orchestrator", TurnEnd("limited", resets_at=clock.now() - 30))
    assert limit.tell_news() == Notice(clock.now() + 60, update=False)
    clock.advance(500)
    limit.observe("orchestrator", TurnEnd("limited", resets_at=clock.now() - 30))
    limit.observe("orchestrator", TurnEnd("limited", resets_at=clock.now() + 3600))
    assert [news for _, news in hits] == [True, False, True]
    assert limit.tell_news() == Notice(clock.now() + 3600, update=True)
    await limit.close()


async def test_a_limit_still_on_after_the_hold_cleared_is_not_told_again_and_the_reset_is_told_once(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Check the CI")],
            [Text("The CI is green, sir.")],
            [Text("The limit has reset, sir.")],
        ],
        orchestrator=[[Limited()], [Limited()], [report("T1", "The CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)
    typist.type("Is the CI green?")
    await until(lambda: any(LINE in r for r in screen_lines(output)))
    typist.end()
    clock.advance(900)
    await until(lambda: len(backend.session("orchestrator").received) == 2)
    clock.advance(900)
    await run

    assert sum(LINE in r for r in screen_lines(output)) == 1
    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 1
    assert screen_lines(output)[-1] == "The limit has reset, sir."
    await hub.close()


async def test_an_episode_that_expired_ends_silently_when_another_role_completes_a_turn_later(
    clock: FakeClock,
) -> None:
    resets: list[None] = []
    limit = UsageLimit(clock, clock.now, lambda role, news: None, lambda roles: None, lambda: resets.append(None))

    limit.observe("subagent", TurnEnd("limited"))
    assert limit.tell_news() == Notice(None, update=False)
    clock.advance(900)
    await until(lambda: not limit.active)
    clock.advance(3 * 3600)
    limit.observe("subagent", TurnEnd("completed"))

    assert resets == []
    limit.observe("subagent", TurnEnd("limited"))
    assert limit.tell_news() == Notice(None, update=False)
    await limit.close()


async def test_a_reset_the_talker_was_telling_when_it_hit_the_limit_again_is_not_told_twice(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics
) -> None:
    backend = FakeBackend(
        clock,
        [
            [Text("Right away, sir."), delegate("Check the CI")],
            [Text("CI green, sir.")],
            [Limited(600)],  # the turn that tells the reset hits the limit
            [Text("Told again, sir.")],
            [Text("More, sir.")],
        ],
        orchestrator=[[Limited(1800)], [report("T1", "The CI is green.")]],
    )
    hub = make_hub(backend, output, metrics)
    typist = Typist(clock)
    run = await running(hub, typist)

    typist.type("Check the CI")
    await until(lambda: any(LINE in r for r in screen_lines(output)))
    await asyncio.sleep(0.05)
    clock.advance(1800)
    await asyncio.sleep(0.2)
    clock.advance(600)
    await asyncio.sleep(0.2)
    typist.end()
    await asyncio.wait_for(run, 2)

    assert sum('reason="usage_reset"' in m for m in talker_received(backend)) == 1
    await hub.close()


async def test_showing_a_restored_limit_preserves_saved_work_before_async_recovery(
    clock: FakeClock, metrics: RecordingMetrics, tmp_path: Path
) -> None:
    state = tmp_path / ".jarvis" / "state.json"
    before = RecordingOutput()
    first_backend = FakeBackend(
        clock, [[Text("I'll check the CI, sir."), delegate("Check the CI")]],
        orchestrator=[[Limited(600)]],
    )
    first = make_hub(first_backend, before, metrics, state_path=state)
    typist = Typist(clock)
    run = await running(first, typist)
    typist.type("Check the CI")
    await until(lambda: any(LINE in n for n in before.notices))
    await stopped(first, run)
    saved = json.loads(state.read_text())
    assert [t["id"] for t in saved["tasks"]] == ["T1"]
    assert saved["unanswered"]

    output = RecordingOutput()
    backend = FakeBackend(clock)
    second = make_hub(backend, output, metrics, state_path=state)
    try:
        await second.start()
        assert backend.sessions == []
        assert any(LINE in n for n in output.notices)
        restored = json.loads(state.read_text())
        for field in ("tasks", "subagents", "approvals", "unanswered", "held", "parked", "waits"):
            assert restored[field] == saved[field]
    finally:
        await second.close()
