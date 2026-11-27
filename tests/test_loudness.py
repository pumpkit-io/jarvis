import numpy as np
import pytest

from jarvis_hub.voice.loudness import integrated_lufs


def sine(amplitude: float, rate: int, seconds: float = 5.0, hz: float = 997.0) -> np.ndarray:
    t = np.arange(round(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.float32)


@pytest.mark.parametrize("rate", [48_000, 24_000, 44_100])
def test_a_full_scale_997_hz_sine_reads_minus_3_01_lufs_as_bs_1770_specifies(rate: int) -> None:
    assert integrated_lufs(sine(1.0, rate), rate) == pytest.approx(-3.01, abs=0.05)


def test_a_sine_20_db_down_reads_20_lu_lower() -> None:
    assert integrated_lufs(sine(0.1, 24_000), 24_000) == pytest.approx(-23.01, abs=0.05)


def test_silence_between_sentences_does_not_lower_the_reading() -> None:
    speech = sine(0.1, 24_000, seconds=3.0)
    gaps = np.concatenate([speech, np.zeros(24_000 * 3, dtype=np.float32), speech])

    # Ungated, a third of silence would read 1.76 dB lower; only the blocks straddling each edge count.
    assert integrated_lufs(gaps, 24_000) == pytest.approx(integrated_lufs(speech, 24_000), abs=0.5)


def test_audio_too_short_or_too_quiet_to_measure_is_refused() -> None:
    with pytest.raises(ValueError, match="too short"):
        integrated_lufs(sine(0.5, 24_000, seconds=0.2), 24_000)
    with pytest.raises(ValueError, match="silent"):
        integrated_lufs(np.zeros(24_000 * 2, dtype=np.float32), 24_000)
