"""Integrated loudness per ITU-R BS.1770-4, as ffmpeg's `ebur128` reports it, for mono audio at any rate."""

import math
from typing import Any, cast

import numpy as np

from jarvis_hub.voice.player import Samples

BLOCK_S = 0.4
STEP_S = 0.1
ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0


# K-weighting is defined at 48 kHz; at 24 kHz the bilinear transform reads a 1 kHz tone 0.26 dB low.
RATE = 48_000


def _k_weighting(rate: int) -> list[tuple[list[float], list[float]]]:
    """libebur128's analogue derivation of the two stages, which reproduces the standard's 48 kHz coefficients."""
    k = math.tan(math.pi * 1681.974450955533 / rate)
    q = 0.7071752369554196
    vh = 10 ** (3.999843853973347 / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = ([(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0],
             [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    k = math.tan(math.pi * 38.13547087602444 / rate)
    q = 0.5003270373238773
    a0 = 1 + k / q + k * k
    highpass = ([1.0, -2.0, 1.0], [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    return [shelf, highpass]


def integrated_lufs(audio: Samples, rate: int) -> float:
    from scipy import signal  # pyright: ignore[reportMissingTypeStubs]

    scipy = cast(Any, signal)
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if rate != RATE:
        g = math.gcd(rate, RATE)
        x = scipy.resample_poly(x, RATE // g, rate // g)
        rate = RATE
    block, step = round(BLOCK_S * rate), round(STEP_S * rate)
    if len(x) < block:
        raise ValueError(f"audio too short to measure: {len(x) / rate:.2f} s, under {BLOCK_S} s")
    for stage in _k_weighting(rate):
        x = scipy.lfilter(*stage, x)
    squares = np.asarray(x, dtype=np.float64) ** 2
    power = np.array([squares[i:i + block].mean() for i in range(0, len(squares) - block + 1, step)])
    with np.errstate(divide="ignore"):
        loud = -0.691 + 10 * np.log10(power)
    gated = power[loud > ABSOLUTE_GATE]
    if not len(gated):
        raise ValueError("audio is silent: nothing above -70 LUFS to measure")
    relative = -0.691 + 10 * math.log10(float(gated.mean())) + RELATIVE_GATE
    kept = power[(loud > ABSOLUTE_GATE) & (loud > relative)]
    return -0.691 + 10 * math.log10(float(kept.mean()))
