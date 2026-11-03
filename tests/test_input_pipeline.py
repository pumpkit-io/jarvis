"""The microphone pipeline on 10 ms frames, with stand-in engines: what it reports and what each engine is given."""

from dataclasses import dataclass, field

import numpy as np
import pytest

from jarvis_hub.voice.input import Engines, InputPipeline, Listening
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.state import Heard, SpeechStarted, WakeWord

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

    def transcribe(self, audio: Samples) -> str:
        self.heard.append(audio)
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


def rig(verdicts: list[float] | None = None, **settings: float) -> Rig:
    turn, stt, echo = ScriptedTurn(list(verdicts or [])), ScriptedStt(), PassThrough()
    engines = Engines(echo=echo, wake=MarkerWake(), vad=LoudVad(), turn=turn, stt=stt, gate=Everyone())
    records: list[dict[str, object]] = []
    pipeline = InputPipeline(engines, Listening(**settings), record=records.append)
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
