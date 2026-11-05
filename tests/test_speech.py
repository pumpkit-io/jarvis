"""Spoken replies, driven through the Hub with a fake synthesiser and a recording player."""

import asyncio
import threading
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
    ("Very good, sir.", "am_liam"),
    ("Molto bene, signore.", "im_nicola"),
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

    speech.say("very_good", "en")
    speech.say("very_good", "en")
    speech.say("very_good", "en")
    speech.say("chime", "en")
    await speech.idle()

    assert player.texts == ["Very good, sir.", "Very good, sir.", "Very good, sir.", ""]
    assert player.clips[0] is player.clips[2]
    assert player.clips[3].seconds > 0
    assert sorted(tts.calls) == sorted(STARTUP_LINES)


async def test_a_cached_line_is_said_in_the_language_asked_for(speech: Speech, player: RecordingPlayer) -> None:
    speech.say("very_good", "it")
    speech.say("very_good", "en")
    await speech.idle()

    assert list(zip(player.texts, player.voices)) == [
        ("Molto bene, signore.", "im_nicola"),
        ("Very good, sir.", "am_liam"),
    ]


async def test_slow_generation_starts_with_the_real_answer_without_cached_holding_speech(
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

    assert player.texts == ["Here you are, sir."]
    assert ("One moment, sir.", "am_liam") not in tts.calls
    [row] = speech_rows(metrics)
    assert "holding_line" not in row
    assert row["first_meaningful_audio_ms"] == 2000
    turn = next(r for r in metrics.records if r.get("role") == "talker" and r["kind"] == "user")
    assert row["turn"] == turn["turn"]
    assert row["request_at"] == turn["request_at"]
    assert row["meaningful_audio_basis"] == "queue_estimate"


async def test_slow_italian_generation_starts_with_the_real_answer(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Wait(2.0), Text("Ecco, signore.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Dimmi qualcosa di lungo"))
    await speech.idle()

    assert player.texts == ["Ecco, signore."]


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
    assert "holding_line" not in row


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
    stopped = next(r for r in metrics.records if r["kind"] == "speech_stop")
    turn = next(r for r in metrics.records if r.get("role") == "talker" and r["kind"] == "user")
    assert stopped["turn"] == turn["turn"]
    assert stopped["request_at"] == turn["request_at"]
    assert stopped["stopped_at"] == clock.now()


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
    speech.say("very_good", "en")
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
    speech.say("very_good", "en")
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

    assert player.texts == ["Very good, sir.", "Evening, sir."]


@pytest.mark.parametrize(
    ("prefix", "suffix", "spoken"),
    [
        ("Visit pumpkit.", "io/docs today, sir.", "Visit pumpkit.io/docs today, sir."),
        ("Vedi pumpkit.", "io/docs, signore.", "Vedi pumpkit.io/docs, signore."),
        ("Visit https://pumpkit.io/search?", "q=jarvis today.", "Visit https://pumpkit.io/search?q=jarvis today."),
        ("Visit https://pumpkit.io/path!", "next today.", "Visit https://pumpkit.io/path!next today."),
        ("It costs 3.", "5 dollars, sir.", "It costs 3.5 dollars, sir."),
        ("Ask Dr.", " Banner, sir.", "Ask Dr. Banner, sir."),
        ("Run `uv sync.", " Then test` now.", "Run `uv sync. Then test` now."),
    ],
)
async def test_stream_silence_does_not_cut_an_unfinished_token(
    clock: FakeClock,
    output: RecordingOutput,
    metrics: RecordingMetrics,
    speech: Speech,
    player: RecordingPlayer,
    prefix: str,
    suffix: str,
    spoken: str,
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text(prefix), Wait(0.5), Gate(release), Text(suffix)]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "Where?")))
    await until(lambda: clock.now() >= 1000.5)
    await asyncio.sleep(0.01)
    try:
        assert player.texts == []
    finally:
        release.set()
        await running
        await speech.idle()

    assert player.texts == [spoken]


async def test_a_sentence_waits_for_whitespace_after_a_stream_pause(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text("Good evening, sir."), Wait(0.5), Gate(release), Text(" All quiet.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "Hello")))
    await until(lambda: clock.now() >= 1000.5)
    await asyncio.sleep(0.01)
    try:
        assert player.texts == []
    finally:
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


async def test_interrupting_after_the_text_has_ended_still_stops_the_speech(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, tts: FakeTts
) -> None:
    player = RecordingPlayer(hold=True)
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    backend = FakeBackend(clock, [[Text("A long answer, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    await hub.run(lines(clock, "Talk to me"))
    await until(lambda: player.texts == ["A long answer, sir."])
    await hub.interrupt()

    assert player.pending() == 0
    assert "stop" in player.events
    await speech.close()


async def test_waiting_behind_a_busy_talker_inserts_no_holding_speech(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(clock, [[Text("First, sir. "), Wait(3.0), Gate(release)], [Wait(1.0), Text("Second, sir.")]])
    hub = speaking_hub(backend, output, metrics, speech)

    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, "One", "Two")))
    await until(lambda: player.texts == ["First, sir."])
    await speech.idle()
    release.set()
    await running
    await speech.idle()

    assert player.texts == ["First, sir.", "Second, sir."]


async def test_idle_gives_up_on_audio_that_stops_playing(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, tts: FakeTts
) -> None:
    player = RecordingPlayer(hold=True)
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    speech.say("very_good", "en")
    await until(lambda: player.texts == ["Very good, sir."])

    idling = asyncio.create_task(speech.idle())
    await asyncio.sleep(0.01)
    assert not idling.done()
    clock.advance(5.0)

    await until(idling.done)
    await speech.close()


async def test_a_voice_still_loading_never_keeps_the_process_from_exiting(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, player: RecordingPlayer
) -> None:
    tts = FakeTts()
    tts.hold.clear()
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()

    await asyncio.wait_for(speech.close(), timeout=1)

    synthesis = [t for t in threading.enumerate() if t.name.startswith("tts")]
    assert synthesis and all(t.daemon for t in synthesis)
    tts.hold.set()


async def test_the_chime_reports_when_it_starts_playing(
    clock: FakeClock, tts: FakeTts, metrics: RecordingMetrics, output: RecordingOutput
) -> None:
    player = RecordingPlayer(hold=True)
    starts: list[float] = []
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice, on_chime=starts.append)
    speech.start()
    try:
        speech.say("very_good", "en")
        speech.say("chime", "en")
        await until(lambda: len(player.clips) == 2)

        # After the startup line still queued ahead of it.
        assert starts == [pytest.approx(clock.now() + player.clips[0].seconds)]  # pyright: ignore[reportUnknownMemberType]
    finally:
        await speech.close()


async def test_cached_speech_does_not_claim_a_meaningful_response(
    clock: FakeClock, metrics: RecordingMetrics, speech: Speech
) -> None:
    speech.begin("en", turn=45, heard_at=clock.now())
    speech.say("very_good", "en", in_reply=True)
    await speech.idle()
    clock.advance(0.8)
    speech.feed("The build passed.")
    speech.end()
    await speech.idle()

    [row] = speech_rows(metrics)
    assert row["first_audio_ms"] == 0
    assert row["first_meaningful_audio_ms"] == 800


async def test_stopping_after_normal_playback_does_not_supersede_the_request(
    clock: FakeClock, metrics: RecordingMetrics, speech: Speech
) -> None:
    speech.begin("en", turn=70, heard_at=clock.now())
    speech.feed("The build passed.")
    speech.end()
    await speech.idle()
    speech.stop()

    assert not any(r["kind"] == "speech_stop" for r in metrics.records)
    assert len(speech_rows(metrics)) == 1


@pytest.mark.parametrize("drained", [False, True])
async def test_stop_traces_only_reply_audio_left_ahead_of_a_chime(
    clock: FakeClock, metrics: RecordingMetrics, tts: FakeTts, output: RecordingOutput, drained: bool
) -> None:
    player = RecordingPlayer(hold=True)
    speech = Speech(tts, player, clock, metrics=metrics, notice=output.notice)
    speech.start()
    try:
        speech.begin("en", turn=71, heard_at=clock.now())
        speech.feed("The build passed.")
        speech.end()
        await until(lambda: len(speech_rows(metrics)) == 1)
        speech.say("chime", "en")
        await until(lambda: len(player.clips) == 2)
        if drained:
            player.finish(1)
        speech.stop()

        stops = [r["turn"] for r in metrics.records if r["kind"] == "speech_stop"]
        assert stops == ([] if drained else [71])
        assert player.pending() == 0
    finally:
        await speech.close()


@pytest.mark.parametrize(
    ("utterance", "response"),
    [
        ("Check the open Jarvis PRs", "I'll check Jarvis's PRs, sir."),
        ("Controlla le PR aperte di Jarvis", "Controllo le PR di Jarvis, signore."),
    ],
)
async def test_request_specific_response_precedes_background_work(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech,
    player: RecordingPlayer, utterance: str, response: str,
) -> None:
    release = asyncio.Event()
    backend = FakeBackend(
        clock,
        [[Text(response + " "), Gate(release), Tool("delegate", {"request": utterance})], [Text("The checks passed.")]],
        orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "The checks passed."})]],
    )
    hub = speaking_hub(backend, output, metrics, speech)
    await hub.start()
    running = asyncio.create_task(hub.run(lines(clock, utterance)))
    try:
        await until(lambda: player.texts == [response])
        assert backend.session("talker").tool_results == []
        assert all(not s.received for s in backend.sessions if s.spec.role == "orchestrator")
    finally:
        release.set()
        await running
        await speech.idle()

    turn = next(r for r in metrics.records if r.get("role") == "talker" and r["kind"] == "user")
    assert turn["tool_before_text"] is False
    assert backend.session("talker").tool_results[0].input == {"request": utterance}


async def test_cancelling_slow_generation_before_any_text_emits_no_filler(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    backend = FakeBackend(clock, [[Wait(3.0), AwaitInterrupt(), Text("Obsolete answer.")]])
    hub = speaking_hub(backend, output, metrics, speech)
    typist = Typist(clock)
    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("Explain the pending work")
    await until(lambda: clock.now() >= 1003.0)
    await asyncio.sleep(0.01)
    assert player.texts == []
    assert await hub.interrupt()
    typist.end()
    await running
    await speech.idle()

    assert player.texts == []
    stopped = next(r for r in metrics.records if r["kind"] == "speech_stop")
    assert stopped["first_meaningful_audio_ms"] is None


async def test_backend_failure_emits_operational_status_without_a_spoken_fallback(
    clock: FakeClock, output: RecordingOutput, metrics: RecordingMetrics, speech: Speech, player: RecordingPlayer
) -> None:
    from tests.fakes import Fail

    backend = FakeBackend(clock, [[Fail("transport disconnected")]])
    hub = speaking_hub(backend, output, metrics, speech)
    await hub.start()
    await hub.run(lines(clock, "Check the pending work"))
    await speech.idle()

    assert player.texts == []
    assert any("Model unavailable" in n and "transport disconnected" in n and "Try a new request" in n for n in output.notices)
    assert len(backend.session("talker").received) == 2
