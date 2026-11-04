from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from jarvis_hub.voice.player import Clip, SoundPlayer

RATE = 1000  # Samples per second, so a millisecond is a sample.


def test_device_buffers_speech_across_a_short_inference_scheduling_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

    class BufferedDevice:
        def __init__(self, **settings: Any) -> None:
            self.rate = settings["samplerate"]
            self.latency = 0.035375 if settings["latency"] == "low" else float(settings["latency"])
            self.callback = settings["callback"]
            devices.append(self)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

        def render_through_stall(self, stall_s: float) -> np.ndarray:
            buffered = np.empty((round(self.rate * self.latency), 1), np.float32)
            self.callback(buffered, len(buffered), None, None)
            heard = np.zeros(round(self.rate * stall_s), np.float32)
            copied = min(len(heard), len(buffered))
            heard[:copied] = buffered[:copied, 0]
            return heard

    devices: list[BufferedDevice] = []
    monkeypatch.setattr(sd, "OutputStream", BufferedDevice)
    player = SoundPlayer(24_000)
    try:
        samples = np.full(24_000, 0.25, np.float32)
        player.play(Clip("A continuous sentence.", "am_liam", samples, 24_000))
        np.testing.assert_array_equal(devices[0].render_through_stall(0.05), 0.25)
    finally:
        player.close()


class FakeStream:
    """Stands in for the audio device: the test pulls frames the way the device's callback would."""

    def __init__(self, fill: Callable[[np.ndarray], None]) -> None:
        self.fill = fill
        self.closed = False

    def pull(self, frames: int) -> np.ndarray:
        out = np.full(frames, np.nan, dtype=np.float32)
        self.fill(out)
        return out

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def stream_and_player() -> tuple[FakeStream, SoundPlayer]:
    streams: list[FakeStream] = []

    def open_stream(rate: int, fill: Callable[[np.ndarray], None]) -> FakeStream:
        assert rate == RATE
        streams.append(FakeStream(fill))
        return streams[-1]

    player = SoundPlayer(RATE, open_stream=open_stream, fade_ms=10)
    return streams[0], player


def ramp(start: int, n: int) -> np.ndarray:
    """Sample i holds its own position, so a test can tell which part of a clip was played."""
    return (np.arange(start, start + n, dtype=np.float32) + 1) / 10_000


def clip(text: str, samples: np.ndarray) -> Clip:
    return Clip(text, "am_liam", samples, RATE)


def test_clips_play_back_to_back_with_no_gap(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", ramp(0, 150)))
    player.play(clip("Two.", ramp(150, 100)))

    out = np.concatenate([stream.pull(64) for _ in range(5)])

    np.testing.assert_array_equal(out[:250], ramp(0, 250))
    np.testing.assert_array_equal(out[250:], 0)
    assert player.pending() == 0


def test_silence_when_there_is_nothing_to_play(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, _ = stream_and_player

    np.testing.assert_array_equal(stream.pull(32), 0)


def test_pending_counts_the_seconds_left_to_play(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", ramp(0, 500)))
    player.play(clip("Two.", ramp(0, 500)))

    stream.pull(300)

    assert player.pending() == pytest.approx(0.7)


def test_pause_fades_out_then_holds_silence(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("A long sentence.", np.full(1000, 0.5, dtype=np.float32)))
    stream.pull(100)

    player.pause()
    fade = stream.pull(20)

    assert fade[0] > 0.4
    assert np.all(np.diff(fade[:10]) < 0)
    np.testing.assert_array_equal(fade[10:], 0)
    np.testing.assert_array_equal(stream.pull(200), 0)
    assert player.pending() == pytest.approx(0.891)


def test_resume_rewinds_and_fades_back_in(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("A long sentence.", ramp(0, 1000)))
    stream.pull(500)
    player.pause()
    stream.pull(10)  # The fade plays samples 500 to 508, then silence.
    stream.pull(50)

    player.resume(rewind_ms=300)
    out = stream.pull(40)

    # Back at sample 209, 300 ms before the fade ended, rising from silence.
    assert out[9] < ramp(218, 1)[0]
    np.testing.assert_array_equal(out[10:], ramp(219, 30))


def test_stop_fades_out_and_flushes_everything_queued(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", np.full(500, 0.5, dtype=np.float32)))
    player.play(clip("Two.", np.full(500, 0.5, dtype=np.float32)))
    stream.pull(100)

    player.stop()
    out = stream.pull(100)

    assert np.all(np.diff(out[:10]) < 0)
    np.testing.assert_array_equal(out[10:], 0)
    assert player.pending() == 0
    player.play(clip("Three.", np.full(50, 0.25, dtype=np.float32)))
    np.testing.assert_array_equal(stream.pull(50), 0.25)


def test_reports_the_text_actually_played(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("Right away, sir.", ramp(0, 100)))
    player.play(clip("I have opened the pull request on pumpkit.", ramp(0, 800)))
    assert player.played() == ""

    stream.pull(100)
    assert player.played() == "Right away, sir."

    stream.pull(400)  # Half of the second clip: four of its eight words.
    assert player.played() == "Right away, sir. I have opened the"

    player.stop()
    stream.pull(50)
    assert player.played() == "Right away, sir. I have opened the"


def test_a_new_reply_starts_a_new_record(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("First reply.", ramp(0, 100)), starts_reply=True)
    stream.pull(200)
    assert player.played() == "First reply."

    player.play(clip("Second reply.", ramp(0, 100)), starts_reply=True)
    stream.pull(100)

    assert player.played() == "Second reply."


def test_a_new_reply_queued_behind_another_starts_its_record_when_it_plays(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    player.play(clip("First reply.", ramp(0, 100)), starts_reply=True)
    player.play(clip("Second reply.", ramp(0, 100)), starts_reply=True)
    stream.pull(50)
    assert player.played() == "First"

    stream.pull(100)

    assert player.played() == "Second"
    stream.pull(50)
    assert player.played() == "Second reply."


def test_a_pause_between_sentences_keeps_the_record(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One moment, sir.", ramp(0, 100)), starts_reply=True)
    stream.pull(300)

    player.play(clip("Here you are.", ramp(0, 100)))
    stream.pull(100)

    assert player.played() == "One moment, sir. Here you are."


def test_close_closes_the_stream(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player

    player.close()

    assert stream.closed


def test_resume_cannot_undo_a_stop(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", np.full(500, 0.5, dtype=np.float32)))
    stream.pull(100)

    player.stop()
    player.resume()

    stream.pull(20)
    np.testing.assert_array_equal(stream.pull(100), 0)
    assert player.pending() == 0


def test_a_clip_queued_while_a_stop_fades_out_still_plays(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", np.full(500, 0.5, dtype=np.float32)))
    stream.pull(100)

    player.stop()
    player.play(clip("Next reply.", np.full(50, 0.25, dtype=np.float32)), starts_reply=True)
    stream.pull(10)

    np.testing.assert_array_equal(stream.pull(50), 0.25)


def test_a_pause_keeps_every_sample_the_fade_did_not_play(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("A long sentence.", ramp(0, 1000)))
    stream.pull(100)

    player.pause()
    fade = stream.pull(20)

    assert np.count_nonzero(fade) == 9
    assert player.pending() == pytest.approx(0.891)


def test_a_fade_out_across_two_clips_holds_the_place_in_the_second(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", ramp(0, 100)))
    player.play(clip("Two.", ramp(100, 100)))
    stream.pull(95)

    player.pause()
    stream.pull(20)  # Five samples of the first clip and four of the second are heard while the gain falls.
    player.resume(rewind_ms=0)
    out = stream.pull(20)

    np.testing.assert_array_equal(out[10:], ramp(114, 10))


def test_resume_rewinds_into_the_clip_before(stream_and_player: tuple[FakeStream, SoundPlayer]) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", ramp(0, 100)))
    player.play(clip("Two.", ramp(100, 100)))
    stream.pull(105)

    player.pause()
    stream.pull(20)
    player.resume(rewind_ms=50)
    out = stream.pull(30)

    # The fade ended after sample 113, so playback goes back to sample 64, in the first clip.
    np.testing.assert_array_equal(out[10:], ramp(74, 20))
    stream.pull(50)
    assert player.played() == "One."


def test_a_stop_whose_clips_end_during_the_fade_leaves_the_player_ready(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", np.full(100, 0.5, dtype=np.float32)))
    stream.pull(97)

    player.stop()
    stream.pull(20)
    player.play(clip("Next reply.", np.full(50, 0.25, dtype=np.float32)), starts_reply=True)

    np.testing.assert_array_equal(stream.pull(50), 0.25)


def test_a_clip_queued_after_a_stop_is_not_faded_when_the_stop_fade_outlasts_the_stopped_clip(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", ramp(0, 15)))
    stream.pull(10)

    player.stop()  # Five samples are left in the clip, fewer than the ten of the fade.
    player.play(clip("Next reply.", ramp(100, 50)), starts_reply=True)
    fade = stream.pull(10)
    out = stream.pull(50)

    assert np.all(np.diff(fade[:5]) < 0)
    np.testing.assert_array_equal(out, ramp(100, 50))
    assert player.played() == "Next reply."


def test_a_stop_after_a_pause_leaves_the_player_clean_unpaused_and_empty(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    player.play(clip("One.", np.full(500, 0.5, dtype=np.float32)))
    player.play(clip("Two.", np.full(500, 0.5, dtype=np.float32)))
    stream.pull(100)
    player.pause()
    stream.pull(30)

    player.stop()

    assert player.pending() == 0
    player.resume()
    player.play(clip("Next reply.", np.full(50, 0.25, dtype=np.float32)), starts_reply=True)
    np.testing.assert_array_equal(stream.pull(50), 0.25)
    assert player.played() == "Next reply."


def test_resume_rewinds_across_as_many_short_clips_as_it_takes(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    for i in range(4):
        player.play(clip(f"Clip {i}.", ramp(i * 20, 20)))
    stream.pull(70)

    player.pause()
    stream.pull(20)  # The fade plays up to sample 78, in the fourth clip.
    player.resume(rewind_ms=70)
    out = stream.pull(40)

    # Back at sample 9, three clips before the one where the fade ended.
    np.testing.assert_array_equal(out[10:], ramp(19, 30))


def test_resume_rewinds_no_further_than_two_seconds_of_finished_clips(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    for i in range(4):
        player.play(clip(f"Clip {i}.", ramp(i * 1000, 1000)))
    stream.pull(3500)  # Three seconds of clips have finished: the first is past the two-second history.

    player.pause()
    stream.pull(20)
    player.resume(rewind_ms=10_000)
    out = stream.pull(40)

    # Back at the start of the second clip, the oldest one kept.
    np.testing.assert_array_equal(out[20:], ramp(1020, 20))


def test_resume_keeps_one_finished_clip_even_when_it_is_over_the_history_budget(
    stream_and_player: tuple[FakeStream, SoundPlayer],
) -> None:
    stream, player = stream_and_player
    player.play(clip("Long.", ramp(0, 3000)))
    player.play(clip("Short.", ramp(3000, 500)))
    stream.pull(3100)

    player.pause()
    stream.pull(20)
    player.resume(rewind_ms=10_000)
    out = stream.pull(40)

    np.testing.assert_array_equal(out[20:], ramp(20, 20))


def test_every_block_played_goes_to_echo_cancellation_silence_included() -> None:
    streams: list[FakeStream] = []
    played: list[tuple[np.ndarray, int]] = []

    def open_stream(rate: int, fill: Callable[[np.ndarray], None]) -> FakeStream:
        streams.append(FakeStream(fill))
        return streams[-1]

    player = SoundPlayer(RATE, open_stream=open_stream, on_played=lambda s, r: played.append((s.copy(), r)))
    player.play(clip("One.", ramp(0, 100)))
    out = np.concatenate([streams[0].pull(64) for _ in range(3)])

    np.testing.assert_array_equal(np.concatenate([s for s, _ in played]), out)
    assert {r for _, r in played} == {RATE}
