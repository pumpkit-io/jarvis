"""Whisper's log-mel features in numpy, as `transformers.WhisperFeatureExtractor` computes them, for Smart Turn.

Written out so the Hub does not depend on `transformers` for one function; `tests/test_whisper_features.py` holds
reference values from it.
"""

from functools import cache

import numpy as np
from numpy.typing import NDArray

from jarvis_hub.voice.input import SAMPLE_RATE

N_FFT = 400
HOP = 160
N_MELS = 80


def _hz_to_mel(hz: NDArray[np.float64]) -> NDArray[np.float64]:
    # Slaney's scale: linear below 1 kHz, logarithmic above.
    mel = 3.0 * hz / 200.0
    log_region = hz >= 1000.0
    return np.where(log_region, 15.0 + np.log(np.maximum(hz, 1e-10) / 1000.0) * (27.0 / np.log(6.4)), mel)


def _mel_to_hz(mel: NDArray[np.float64]) -> NDArray[np.float64]:
    hz = 200.0 * mel / 3.0
    log_region = mel >= 15.0
    return np.where(log_region, 1000.0 * np.exp(np.log(6.4) / 27.0 * (mel - 15.0)), hz)


@cache
def mel_filters() -> NDArray[np.float64]:
    """(N_FFT // 2 + 1, N_MELS) triangular filters, Slaney-normalised, from 0 to 8 kHz."""
    mels = np.linspace(_hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(8000.0)), N_MELS + 2)
    edges = _mel_to_hz(mels)
    fft_freqs = np.linspace(0, SAMPLE_RATE // 2, N_FFT // 2 + 1)
    slopes = edges[None, :] - fft_freqs[:, None]
    widths = np.diff(edges)
    down = -slopes[:, :-2] / widths[:-1]
    up = slopes[:, 2:] / widths[1:]
    filters = np.maximum(0.0, np.minimum(down, up))
    return filters * (2.0 / (edges[2:] - edges[:-2]))[None, :]


def log_mel(audio: NDArray[np.float32], seconds: int = 8) -> NDArray[np.float32]:
    """(1, N_MELS, frames) features of the last `seconds` of `audio`, zero-padded at the start as Smart Turn expects."""
    n = seconds * SAMPLE_RATE
    wave = audio[-n:].astype(np.float64)
    wave = np.pad(wave, (n - len(wave), 0))
    wave = (wave - wave.mean()) / np.sqrt(wave.var() + 1e-7)
    padded = np.pad(wave, (N_FFT // 2, N_FFT // 2), mode="reflect")
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    mel = np.maximum(power @ mel_filters(), 1e-10)
    spec = np.log10(mel).T[:, :-1]
    spec = np.maximum(spec, spec.max() - 8.0)
    return ((spec + 4.0) / 4.0)[None].astype(np.float32)
