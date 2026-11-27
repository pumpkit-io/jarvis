"""The chime and the closing sound sit at the level of the speech around them, on the Mac and on the iPhone."""

from typing import Any, cast

import numpy as np
from scipy import signal  # pyright: ignore[reportMissingTypeStubs]

from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.speech import chime, closing
from jarvis_hub.voice.tts import SPEECH_LUFS

RATE = 48_000
# ITU-R BS.1770 K-weighting at 48 kHz: a high shelf for the head, then a high pass.
K_WEIGHTING = (
    ([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585]),
    ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621]),
)


def loudness_while_sounding(samples: Samples) -> float:
    """BS.1770 loudness over the whole cue: shorter than one 400 ms gating block, so nothing is gated out."""
    weighted = samples.astype(np.float64)
    for b, a in K_WEIGHTING:
        weighted = np.asarray(cast(Any, signal).lfilter(b, a, weighted), dtype=np.float64)
    return -0.691 + 10 * float(np.log10(np.mean(weighted**2)))


def test_the_k_weighting_reads_a_997_hz_sine_at_full_scale_as_minus_3_lufs() -> None:
    t = np.arange(RATE) / RATE

    assert abs(loudness_while_sounding(np.sin(2 * np.pi * 997 * t).astype(np.float32)) - -3.01) < 0.05


def test_the_chime_is_as_loud_as_speech() -> None:
    assert abs(loudness_while_sounding(chime(RATE)) - SPEECH_LUFS) < 0.5


def test_the_closing_sound_is_2_lu_softer_than_speech() -> None:
    assert abs(loudness_while_sounding(closing(RATE)) - (SPEECH_LUFS - 2)) < 0.5
