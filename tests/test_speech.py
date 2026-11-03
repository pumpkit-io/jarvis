"""Spoken replies, driven through the Hub with a fake synthesiser and a recording player."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from jarvis_hub.hub import Hub
from jarvis_hub.voice.speech import Speech
from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    FakeTts,
    Gate,
    RecordingMetrics,
    RecordingOutput,
    RecordingPlayer,
    Text,
    Tool,
    Typist,
    Wait,
    lines,
    make_hub,
    until,
)

STARTUP_LINES = [
    ("At your service, sir.", "am_liam"),
    ("One moment, sir.", "am_liam"),
    ("Very good, sir.", "am_liam"),
    ("I've reached the usage limit, sir.", "am_liam"),
    ("Ai suoi ordini, signore.", "im_nicola"),
    ("Un momento, signore.", "im_nicola"),
    ("Molto bene, signore.", "im_nicola"),
    ("Ho raggiunto il limite di utilizzo, signore.", "im_nicola"),
]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def output() -> RecordingOutput:
    return RecordingOutput()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def tts() -> FakeTts:
    return FakeTts()


@pytest.fixture
def player() -> RecordingPlayer:
    return RecordingPlayer()


@pytest.fixture
async def speech(
    clock: FakeClock, tts: FakeTts, player: RecordingPlayer, metrics: RecordingMetrics, output: RecordingOutput
) -> AsyncIterator[Speech]:
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    yield speech
    await speech.close()


def speaking_hub(backend: FakeBackend, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech) -> Hub:
    return make_hub(backend, output, metrics, speech=speech)


def speech_rows(metrics: RecordingMetrics) -> list[dict[str, object]]:
    return [r for r in metrics.records if r["kind"] == "speech"]


async def test_a_reply_is_spoken_sentence_by_sentence_as_it_streams(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Text("Good evening, "), Text("sir. All"), Text(" quiet.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Good evening, Jarvis"))
    await speech.idle()

    assert player.texts == ["Good evening, sir.", "All quiet."]
    assert player.voices == ["am_liam", "am_liam"]
    assert output.replies == ["Good evening, sir. All quiet."]


async def test_the_warm_up_is_never_spoken(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, warm_up=[Text("Ready.")])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock))
    await speech.idle()

    assert player.texts == []


async def test_text_either_side_of_a_tool_call_is_two_sentences(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Text("Let me see, sir."), Tool("status"), Text("Nothing is running")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "What are you working on?"))
    await speech.idle()

    assert player.texts == ["Let me see, sir.", "Nothing is running"]


async def test_the_voice_follows_the_language_of_the_users_last_turn(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("Bene, signore.")], [Text("Certo, signore.")], [Text("Indeed, sir.")]],
    )
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Su cosa stai lavorando?", "ok", "What are you working on?"))
    await speech.idle()

    assert list(zip(player.texts, player.voices)) == [
        ("Bene, signore.", "im_nicola"),
        ("Certo, signore.", "im_nicola"),
        ("Indeed, sir.", "am_liam"),
    ]
    assert backend.sessions[0].received[1:] == [
        '<user lang="it" typed="true">Su cosa stai lavorando?</user>',
        '<user lang="it" typed="true">ok</user>',
        '<user lang="en" typed="true">What are you working on?</user>',
    ]


async def test_news_is_spoken_in_the_language_of_the_users_last_turn(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("Subito, signore."), Tool("delegate", {"request": "PR aperte su pumpkit"})], [Text("Due PR aperte, signore.")]],
        orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "Two open PRs."})]],
    )
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Quali PR sono aperte su pumpkit?"))
    await speech.idle()

    assert list(zip(player.texts, player.voices)) == [
        ("Subito, signore.", "im_nicola"),
        ("Due PR aperte, signore.", "im_nicola"),
    ]


async def test_the_next_sentence_is_synthesised_while_the_one_before_plays(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, tts: FakeTts
) -> None:
    player = RecordingPlayer(hold=True)
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    backend = FakeBackend(clock, [[Text("The first sentence. The second one. And the third.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Go on"))
    await until(lambda: len(player.clips) == 3)

    assert player.played() == ""
    assert player.texts == ["The first sentence.", "The second one.", "And the third."]
    player.finish()
    await speech.idle()
    await speech.close()


async def test_cached_lines_are_synthesised_once_at_startup(
    clock: FakeClock, speech: Speech, tts: FakeTts, player: RecordingPlayer
) -> None:
    await speech.idle()
    assert sorted(tts.calls) == sorted(STARTUP_LINES)

    speech.say("very_good")
    speech.say("startup")
    speech.say("very_good")
    speech.say("chime")
    await speech.idle()

    assert player.texts == ["Very good, sir.", "At your service, sir.", "Very good, sir.", ""]
    assert player.clips[0] is player.clips[2]
    assert player.clips[3].seconds > 0
    assert sorted(tts.calls) == sorted(STARTUP_LINES)


async def test_a_cached_line_is_said_in_the_current_language(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Text("Certo, signore.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Buonasera Jarvis, come va?"))
    speech.say("very_good")
    await speech.idle()

    assert player.texts == ["Certo, signore.", "Molto bene, signore."]


async def test_one_moment_plays_when_no_sentence_is_ready_shortly_after_the_turn(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    speech: Speech,
    tts: FakeTts,
    player: RecordingPlayer,
) -> None:
    backend = FakeBackend(clock, [[Wait(2.0), Text("Here you are, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Tell me something long"))
    await speech.idle()

    assert player.texts == ["One moment, sir.", "Here you are, sir."]
    assert tts.calls.count(("One moment, sir.", "am_liam")) == 1
    [row] = speech_rows(metrics)
    assert row["holding_line"] is True


async def test_one_moment_is_said_in_italian_after_an_italian_turn(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Wait(2.0), Text("Ecco, signore.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Dimmi qualcosa di lungo"))
    await speech.idle()

    assert player.texts == ["Un momento, signore.", "Ecco, signore."]


async def test_no_holding_line_when_the_first_sentence_comes_in_time(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Wait(1.0), Text("Quick, sir. "), Wait(3.0), Text("And slow.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Hello"))
    await speech.idle()

    assert player.texts == ["Quick, sir.", "And slow."]
    [row] = speech_rows(metrics)
    assert row["holding_line"] is False


async def test_no_holding_line_before_relaying_news(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(
        clock,
        [[Text("Right away, sir."), Tool("delegate", {"request": "open PRs"})], [Wait(3.0), Text("Two open PRs, sir.")]],
        orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "Two open PRs."})]],
    )
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "What PRs are open?"))
    await speech.idle()

    assert player.texts == ["Right away, sir.", "Two open PRs, sir."]


async def test_no_holding_line_while_the_previous_reply_is_still_playing(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, tts: FakeTts
) -> None:
    player = RecordingPlayer(hold=True)
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    backend = FakeBackend(clock, [[Text("A long answer, sir.")], [Wait(2.0), Text("Next, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "First", "Second"))
    await until(lambda: len(player.clips) == 2)
    player.finish()
    await speech.idle()
    await speech.close()

    assert player.texts == ["A long answer, sir.", "Next, sir."]


async def test_the_first_clause_and_its_audio_are_timed(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech
) -> None:
    backend = FakeBackend(clock, [[Wait(0.4), Text("I checked the six open pull requests"), Wait(0.3), Text(", all green.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "How are the PRs?"))
    await speech.idle()

    [row] = speech_rows(metrics)
    assert row["first_chunk_ms"] == 700
    assert row["first_audio_ms"] == 700
    assert row["chunk_to_audio_ms"] == 0
    assert row["chunks"] == 2
    assert row["audio_s"] == 2.35
    assert row["lang"] == "en"


async def test_interrupting_a_reply_stops_its_speech(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Text("First sentence. "), AwaitInterrupt(), Text("Late words. ")]])
    hub = speaking_hub(backend, output, metrics, speech)
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("Talk to me")
    await until(lambda: player.texts == ["First sentence."])
    assert await hub.interrupt()
    typist.end()
    await running
    await speech.idle()

    assert player.texts == ["First sentence."]
    assert "stop" in player.events


async def test_a_sentence_that_fails_to_synthesise_is_skipped_with_a_notice(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, player: RecordingPlayer
) -> None:
    speech = Speech(FakeTts(fail_on="Broken."), player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    backend = FakeBackend(clock, [[Text("Broken. Fine.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Hello"))
    await speech.idle()
    await speech.close()

    assert player.texts == ["Fine."]
    assert output.notices == ["Could not speak a sentence: RuntimeError: synthesis failed"]


async def test_without_a_voice_the_replies_still_arrive_as_text(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, player: RecordingPlayer
) -> None:
    speech = Speech(FakeTts(load_error=OSError("no model")), player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    speech.say("startup")
    backend = FakeBackend(clock, [[Text("Evening, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Hello"))
    await speech.idle()
    await speech.close()

    assert output.replies == ["Evening, sir."]
    assert player.texts == []
    assert output.notices == ["Speech is off: the voice could not load: OSError: no model"]


async def test_a_turn_typed_while_the_voice_loads_is_spoken_once_it_has(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, player: RecordingPlayer
) -> None:
    tts = FakeTts()
    tts.hold.clear()
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    speech.say("startup")
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text("Evening, sir."), Gate(release)]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "Hello")))
    await until(lambda: output.log[-1:] == [("reply", "Evening, sir.")])
    tts.hold.set()
    release.set()
    await running
    await speech.idle()
    await speech.close()

    assert player.texts == ["At your service, sir.", "Evening, sir."]


async def test_a_sentence_at_the_end_of_a_delta_is_spoken_once_the_stream_pauses(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text("Good evening, sir."), Wait(0.5), Gate(release), Text(" All quiet.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "Hello")))
    await until(lambda: player.texts == ["Good evening, sir."])
    release.set()
    await running
    await speech.idle()

    assert player.texts == ["Good evening, sir.", "All quiet."]


async def test_a_number_cut_between_deltas_waits_for_the_rest(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text("It costs 3."), Wait(0.5), Gate(release), Text("5 dollars, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "How much?")))
    await until(lambda: backend.clock.now() >= 1000.5)
    await asyncio.sleep(0.01)
    assert player.texts == []
    release.set()
    await running
    await speech.idle()

    assert player.texts == ["It costs 3.5 dollars, sir."]


async def test_a_delta_that_follows_quickly_is_not_cut_off(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Text("Visit pumpkit."), Wait(0.02), Text("io today, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Where?"))
    await speech.idle()

    assert player.texts == ["Visit pumpkit.io today, sir."]


async def test_a_link_cut_between_slow_deltas_stays_whole(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Text("It is on github.com/pumpkit."), Wait(0.5), Text("io today, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Where?"))
    await speech.idle()

    assert player.texts == ["It is on github.com/pumpkit.io today, sir."]


async def test_what_was_played_covers_the_latest_reply_only(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech
) -> None:
    backend = FakeBackend(clock, [[Text("Good evening, sir.")], [Text("All quiet, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Hello", "Anything new?"))
    await speech.idle()

    assert speech.played() == "All quiet, sir."
