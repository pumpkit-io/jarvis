"""The microphone pipeline on 10 ms frames, with stand-in engines: what it reports and what each engine is given."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from jarvis_hub.voice.bargein import Partial
from jarvis_hub.voice.input import Engines, InputPipeline, Listening
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.state import (
    BargeIn,
    Event,
    Heard,
    Pause,
    ReplyAudio,
    Resume,
    SpeechStarted,
    Tick,
    Timing,
    Voice,
    WakeWord,
    barge_in_from,
    step,
)

RATE = 16_000
FRAME = 160
SPEECH, WAKE = 1000, 2000
T0 = 100.0


class PassThrough:
    def __init__(self) -> None:
        self.frames = 0

    def process(self, frame: np.ndarray) -> np.ndarray:
        self.frames += 1
        return frame

    def reference(self, samples: Samples, rate: int) -> None:
        pass


class MarkerWake:
    chunk = 1280

    def score(self, chunk: np.ndarray) -> float:
        return 1.0 if (chunk == WAKE).any() else 0.0


class LoudVad:
    chunk = 512

    def probability(self, chunk: np.ndarray) -> float:
        return 1.0 if np.abs(chunk.astype(np.float32)).mean() > 500 else 0.0


@dataclass
class ScriptedTurn:
    """Says "complete" unless told otherwise, one verdict per call."""

    verdicts: list[float] = field(default_factory=lambda: [])
    heard: list[int] = field(default_factory=lambda: [])

    def complete(self, audio: Samples) -> float:
        self.heard.append(len(audio))
        return self.verdicts.pop(0) if self.verdicts else 0.9


@dataclass
class ScriptedStt:
    text: str = "Open a PR"
    heard: list[Samples] = field(default_factory=lambda: [])
    after: list[float] = field(default_factory=lambda: [])

    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        self.heard.append(audio)
        self.after.append(after)
        return self.text


class Everyone:
    def admits(self, audio: Samples) -> bool:
        return True


@dataclass
class Rig:
    pipeline: InputPipeline
    turn: ScriptedTurn
    stt: ScriptedStt
    echo: PassThrough
    t: float = T0
    events: list[object] = field(default_factory=lambda: [])
    records: list[dict[str, object]] = field(default_factory=lambda: [])

    def feed(self, kind: str, seconds: float) -> None:
        value = {"speech": SPEECH, "silence": 0, "wake": WAKE}[kind]
        for _ in range(round(seconds * 100)):
            self.t = round(self.t + 0.01, 6)
            frame = np.full(FRAME, value, dtype=np.int16)
            self.events += self.pipeline.push(frame, self.t)

    def heard(self) -> list[Heard]:
        return [e for e in self.events if isinstance(e, Heard)]


def rig(
    verdicts: list[float] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    echo: PassThrough | None = None,
    **settings: Any,
) -> Rig:
    turn, stt, echo = ScriptedTurn(list(verdicts or [])), ScriptedStt(), echo or PassThrough()
    engines = Engines(echo=echo, wake=MarkerWake(), vad=LoudVad(), turn=turn, stt=stt, gate=Everyone())
    records: list[dict[str, object]] = []
    pipeline = InputPipeline(engines, Listening(**settings), record=records.append, clock=clock)
    return Rig(pipeline, turn, stt, echo, records=records)


def test_a_turn_ends_after_half_a_second_of_silence_when_smart_turn_says_complete() -> None:
    r = rig()
    r.pipeline.capture(True)
    r.feed("silence", 0.3)
    r.feed("speech", 1.0)
    r.feed("silence", 0.45)
    assert r.heard() == []
    r.feed("silence", 0.1)

    [heard] = r.heard()
    assert (heard.text, heard.lang) == ("Open a PR", None)
    assert heard.at == pytest.approx(T0 + 1.3, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(r.events[0], SpeechStarted)
    assert r.events[0].at == pytest.approx(T0 + 0.3, abs=0.04)  # pyright: ignore[reportUnknownMemberType]


def test_smart_turn_hears_the_turn_up_to_half_a_second_into_the_silence() -> None:
    r = rig()
    r.pipeline.capture(True)
    r.feed("silence", 1.0)
    r.feed("speech", 1.0)
    r.feed("silence", 1.0)

    [samples] = r.turn.heard
    # From the onset, less the small pad kept before it, to 500 ms past the last speech.
    assert samples / RATE == pytest.approx(0.2 + 1.0 + 0.5, abs=0.04)  # pyright: ignore[reportUnknownMemberType]


def test_incomplete_extends_listening_up_to_one_and_a_half_seconds() -> None:
    r = rig(verdicts=[0.1])
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 1.95)
    assert r.heard() == []
    r.feed("silence", 0.1)

    [heard] = r.heard()
    assert heard.at == pytest.approx(T0 + 1.0, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    assert r.records[-1]["end_wait_ms"] == pytest.approx(2000, abs=40)  # pyright: ignore[reportUnknownMemberType]


def test_speech_after_an_incomplete_verdict_continues_the_same_turn() -> None:
    r = rig(verdicts=[0.1, 0.9])
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 0.8)
    r.feed("speech", 0.5)
    r.feed("silence", 0.6)

    [heard] = r.heard()
    assert heard.at == pytest.approx(T0 + 2.3, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    # The turn began with the first frame, so there was nothing before it to keep.
    assert len(r.stt.heard[0]) / RATE == pytest.approx(2.3 + 0.5, abs=0.06)  # pyright: ignore[reportUnknownMemberType]
    assert len([e for e in r.events if isinstance(e, SpeechStarted)]) == 1


def test_thresholds_are_settings() -> None:
    r = rig(verdicts=[0.1], end_silence_s=0.3, end_extend_s=0.5)
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 0.75)
    assert r.heard() == []
    r.feed("silence", 0.1)
    assert len(r.heard()) == 1


def test_without_capture_there_are_no_turns_but_the_wake_word_is_heard() -> None:
    r = rig()
    r.feed("speech", 1.0)
    r.feed("wake", 0.1)
    r.feed("silence", 1.0)

    assert [type(e) for e in r.events] == [WakeWord]
    assert r.stt.heard == []
    assert r.turn.heard == []


def test_one_wake_word_hit_per_phrase() -> None:
    r = rig()
    r.feed("wake", 0.5)
    r.feed("silence", 2.0)
    r.feed("wake", 0.1)

    assert [type(e) for e in r.events] == [WakeWord, WakeWord]


def test_capture_turned_on_mid_phrase_keeps_the_pre_roll_from_the_onset() -> None:
    r = rig()
    r.feed("silence", 1.0)
    r.feed("speech", 0.6)
    r.feed("wake", 0.1)
    r.pipeline.capture(True)
    r.feed("speech", 0.8)
    r.feed("silence", 0.6)

    [wake, started, heard] = r.events
    assert isinstance(wake, WakeWord) and isinstance(started, SpeechStarted) and isinstance(heard, Heard)
    # The whole phrase, from 200 ms before its onset: "Hey Jarvis, open..." said in one breath.
    assert len(r.stt.heard[0]) / RATE == pytest.approx(0.2 + 1.5 + 0.5, abs=0.06)  # pyright: ignore[reportUnknownMemberType]


def test_the_pre_roll_is_capped() -> None:
    r = rig(preroll_s=1.5)
    r.feed("speech", 3.0)
    r.pipeline.capture(True)
    r.feed("speech", 0.5)
    r.feed("silence", 0.6)

    assert len(r.stt.heard[0]) / RATE == pytest.approx(1.5 + 0.5 + 0.5, abs=0.06)  # pyright: ignore[reportUnknownMemberType]


def test_turning_capture_off_drops_the_turn_in_progress() -> None:
    r = rig()
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.pipeline.capture(False)
    r.feed("silence", 1.0)

    assert r.heard() == []
    assert r.stt.heard == []


def test_every_frame_goes_through_echo_cancellation() -> None:
    r = rig()
    r.feed("silence", 0.5)
    assert r.echo.frames == 50


def test_each_turn_is_recorded_without_its_words() -> None:
    r = rig()
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 0.6)

    [row] = r.records
    assert row["kind"] == "turn"
    assert row["words"] == 3
    assert set(row) >= {"speech_s", "end_wait_ms", "smart_turn", "smart_turn_ms", "stt_ms"}
    assert "Open" not in str(row)


def test_words_before_the_wake_word_hit_are_left_out_of_the_turn() -> None:
    r = rig()
    r.feed("silence", 1.0)
    r.feed("speech", 0.6)
    r.feed("wake", 0.1)
    r.pipeline.capture(True)
    r.feed("speech", 0.8)
    r.feed("silence", 0.6)

    [wake] = [e for e in r.events if isinstance(e, WakeWord)]
    # The turn starts at 0.8 s; the hit, 0.88 s later, closes the wake phrase whatever STT makes of it.
    assert wake.at - T0 == pytest.approx(1.68, abs=0.01)  # pyright: ignore[reportUnknownMemberType]
    assert r.stt.after == [pytest.approx(0.88, abs=0.02)]  # pyright: ignore[reportUnknownMemberType]


def test_a_turn_without_the_wake_word_is_transcribed_whole() -> None:
    r = rig()
    r.feed("wake", 0.1)
    r.feed("silence", 3.0)
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 0.6)

    assert r.stt.after == [0.0]


def test_jarvis_said_later_in_the_turn_cuts_nothing_more() -> None:
    r = rig()
    r.feed("silence", 1.0)
    r.feed("speech", 0.6)
    r.feed("wake", 0.1)
    r.pipeline.capture(True)
    r.feed("speech", 2.5)
    # "...tell Ada that Jarvis will handle the deploy": a second hit, well past the refractory time.
    r.feed("wake", 0.1)
    r.feed("speech", 0.5)
    r.feed("silence", 0.6)

    assert len([e for e in r.events if isinstance(e, WakeWord)]) == 2
    assert r.stt.after == [pytest.approx(0.88, abs=0.02)]  # pyright: ignore[reportUnknownMemberType]


def test_a_turn_ends_at_the_longest_turn_setting_whatever_vad_says() -> None:
    r = rig(max_turn_s=3.0)
    r.pipeline.capture(True)
    r.feed("speech", 2.9)
    assert r.heard() == []
    r.feed("speech", 0.2)
    assert len(r.heard()) == 1


def test_the_pad_before_the_onset_and_the_wake_refractory_time_are_settings() -> None:
    r = rig(onset_pad_s=0.5, wake_refractory_s=0.5)
    r.feed("silence", 1.0)
    r.feed("wake", 0.1)
    r.feed("silence", 0.6)
    r.feed("wake", 0.1)
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 0.6)

    assert len([e for e in r.events if isinstance(e, WakeWord)]) == 2
    # The second wake phrase is the onset: 1.1 s of speech, with 0.5 s before it and 0.5 s of silence after.
    assert len(r.stt.heard[0]) / RATE == pytest.approx(0.5 + 1.1 + 0.5, abs=0.06)  # pyright: ignore[reportUnknownMemberType]


def test_smart_turn_and_stt_are_timed_on_the_given_clock() -> None:
    ticks = iter(range(0, 1000, 7))
    r = rig(clock=lambda: next(ticks) / 1000)
    r.pipeline.capture(True)
    r.feed("speech", 1.0)
    r.feed("silence", 0.6)

    [row] = r.records
    assert (row["smart_turn_ms"], row["stt_ms"]) == (7.0, 7.0)


def barge_ins(r: Rig) -> list[BargeIn]:
    return [e for e in r.events if isinstance(e, BargeIn)]


def partials(r: Rig) -> list[Partial]:
    return [e for e in r.events if isinstance(e, Partial)]


def test_speech_held_over_jarvis_for_the_minimum_is_a_barge_in_from_its_onset() -> None:
    r = rig()
    r.pipeline.barge_in(T0)
    r.feed("silence", 0.3)
    r.feed("speech", 0.18)
    assert barge_ins(r) == []
    r.feed("speech", 0.1)

    [barge] = barge_ins(r)
    assert barge.onset - T0 == pytest.approx(0.3, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    assert barge.at - barge.onset == pytest.approx(0.2, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    assert [type(e) for e in r.events] == [BargeIn]


def test_a_barge_in_is_transcribed_at_about_400_and_700_ms_from_a_little_before_its_onset() -> None:
    r = rig()
    r.stt.text = "okay"
    r.pipeline.barge_in(T0)
    r.feed("silence", 1.0)
    r.feed("speech", 0.35)
    r.feed("silence", 1.0)

    early, quiet, late = partials(r)
    onset = barge_ins(r)[0].onset
    assert (early.text, late.text) == ("okay", "okay")
    assert early.at - onset == pytest.approx(0.4, abs=0.02)  # pyright: ignore[reportUnknownMemberType]
    # Also as the speech goes quiet, so a Backchannel said and done resumes without waiting for the next one.
    assert quiet.silent_s == pytest.approx(0.2, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    assert late.at - onset == pytest.approx(0.7, abs=0.02)  # pyright: ignore[reportUnknownMemberType]
    assert late.speech_s == pytest.approx(0.35, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    assert late.silent_s == pytest.approx(0.35, abs=0.04)  # pyright: ignore[reportUnknownMemberType]
    # From 300 ms before the onset to the moment of each partial.
    assert len(r.stt.heard[2]) / RATE == pytest.approx(0.3 + 0.7, abs=0.04)  # pyright: ignore[reportUnknownMemberType]


def test_a_cough_shorter_than_the_minimum_is_no_barge_in() -> None:
    r = rig()
    r.pipeline.barge_in(T0)
    r.feed("speech", 0.12)
    r.feed("silence", 0.1)
    r.feed("speech", 0.12)
    r.feed("silence", 1.0)

    assert r.events == []
    assert r.stt.heard == []


def test_the_minimum_speech_is_a_setting() -> None:
    r = rig(bargein_min_speech_s=0.4)
    r.pipeline.barge_in(T0)
    r.feed("speech", 0.3)
    assert barge_ins(r) == []
    r.feed("speech", 0.15)
    assert len(barge_ins(r)) == 1


def test_without_barge_in_watching_speech_is_nothing() -> None:
    r = rig()
    r.feed("speech", 1.0)
    r.pipeline.barge_in(T0)
    r.pipeline.barge_in(None)
    r.feed("silence", 1.0)
    r.feed("speech", 1.0)

    assert r.events == []


def test_jarvis_cancelled_out_of_the_input_is_never_a_barge_in() -> None:
    class CancelsAll(PassThrough):
        def process(self, frame: np.ndarray) -> np.ndarray:
            return np.zeros_like(frame)

    r = rig(echo=CancelsAll())
    r.pipeline.barge_in(T0)
    r.feed("speech", 2.0)

    assert r.events == []


def test_watching_again_forgets_the_barge_in_and_needs_speech_held_anew() -> None:
    r = rig()
    r.pipeline.barge_in(T0)
    r.feed("speech", 0.5)
    r.pipeline.barge_in(T0)
    r.feed("speech", 1.0)
    assert len(barge_ins(r)) == 1
    assert len(partials(r)) == 1

    r.feed("silence", 0.2)
    r.feed("speech", 0.25)
    assert len(barge_ins(r)) == 2


def test_a_turn_after_a_barge_in_keeps_the_users_audio_from_its_onset() -> None:
    r = rig()
    r.pipeline.barge_in(T0)
    r.feed("silence", 1.0)
    r.feed("speech", 0.5)
    r.pipeline.capture(True)
    r.pipeline.barge_in(None)
    r.feed("speech", 1.0)
    r.feed("silence", 0.6)

    assert [type(e) for e in r.events] == [BargeIn, Partial, SpeechStarted, Heard]
    # From the pad before the onset, through 1.5 s of speech, to half a second of silence.
    assert len(r.stt.heard[-1]) / RATE == pytest.approx(0.2 + 1.5 + 0.5, abs=0.06)  # pyright: ignore[reportUnknownMemberType]


def test_each_time_the_barge_in_goes_quiet_it_is_transcribed_once() -> None:
    r = rig()
    r.pipeline.barge_in(T0)
    r.feed("speech", 0.8)
    r.feed("silence", 0.3)
    r.feed("speech", 0.2)
    r.feed("silence", 0.3)

    quiet = [p for p in partials(r) if p.silent_s >= 0.2]
    assert len(quiet) == 2
    assert len(partials(r)) == 4


def test_a_barge_in_ends_with_its_speech() -> None:
    r = rig()
    r.pipeline.barge_in(T0)
    r.feed("speech", 0.3)
    r.feed("silence", 2.0)
    r.feed("speech", 0.3)

    assert len(barge_ins(r)) == 2


TIMING = Timing(follow_up_s=6.0, announce_follow_up_s=4.0, listen_s=6.0, chime_s=0.5)


class Duet:
    """The pipeline's events through the voice state machine, which tells the pipeline from when Barge-ins count as
    voice mode does, with Jarvis speaking a reply from `speak`."""

    def __init__(self) -> None:
        self.r = rig()
        self.voice = Voice()
        self.actions: list[object] = []
        self.since: float | None = None
        self.step(WakeWord(T0))
        self.step(Heard("Tell me a story", "en", T0))

    def step(self, event: Event) -> None:
        self.voice, actions = step(self.voice, event, TIMING)
        self.actions += actions
        if (since := barge_in_from(self.voice, TIMING)) != self.since:
            self.since = since
            self.r.pipeline.barge_in(since)

    def speak(self) -> None:
        self.step(ReplyAudio(self.r.t, T0))

    def feed(self, kind: str, seconds: float) -> None:
        for _ in range(round(seconds * 100)):
            seen = len(self.r.events)
            self.r.feed(kind, 0.01)
            for event in self.r.events[seen:]:
                assert isinstance(event, (WakeWord, SpeechStarted, Heard, BargeIn, Partial))
                self.step(event)
            self.step(Tick(self.r.t))


def test_echo_inside_the_guard_does_not_stop_a_later_barge_in() -> None:
    d = Duet()
    d.speak()
    d.feed("speech", 0.22)
    d.feed("silence", 0.3)
    d.feed("speech", 1.5)

    assert Pause() in d.actions


def test_the_user_still_talking_as_a_reply_starts_is_a_barge_in_once_the_guard_has_passed() -> None:
    d = Duet()
    d.feed("speech", 0.3)
    d.speak()
    d.feed("speech", 1.5)

    assert Pause() in d.actions


def test_a_second_barge_in_just_after_a_resume_still_pauses() -> None:
    d = Duet()
    d.speak()
    d.feed("silence", 0.5)
    d.r.stt.text = "Mhm."
    d.feed("speech", 0.3)
    d.feed("silence", 0.2)
    assert d.actions[-2:] == [Pause(), Resume()]
    d.r.stt.text = "Wait"
    d.feed("speech", 1.0)

    assert [a for a in d.actions if a in (Pause(), Resume())] == [Pause(), Resume(), Pause()]
