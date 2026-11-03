"""The audio input engines Phase 0 chose provisionally (docs/notes/phase-0-spikes.md, `## Audio`), behind the
protocols in `jarvis_hub.voice.input`. `load_engines` is the one place to swap one for the recording session's pick.

Every import is inside a loader, so text mode never pays for them.
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from jarvis_hub.voice.input import FRAME, SAMPLE_RATE, Engines, EveryoneGate, Pcm
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.tts import MLX_CACHE_LIMIT
from jarvis_hub.voice.whisper_features import log_mel

PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
SMART_TURN_REPO = "pipecat-ai/smart-turn-v3"
SMART_TURN_FILE = "smart-turn-v3.2-cpu.onnx"
# CC BY-NC-SA 4.0, non-commercial; the code around it is Apache-2.0.
WAKE_MODEL = "hey_jarvis"

# Parakeet's timestamps come in 80 ms steps, so the wake phrase's last word can seem to end just after the hit.
WORD_END_SLACK_S = 0.08

AecMode = Literal["webrtc", "off"]
WakeEngine = Literal["openwakeword"]
SttEngine = Literal["parakeet"]


@dataclass(frozen=True)
class EngineChoice:
    aec: AecMode = "webrtc"
    wake: WakeEngine = "openwakeword"
    stt: SttEngine = "parakeet"


class NoEchoCancellation:
    """For headphones, where nothing played reaches the microphone."""

    def process(self, frame: Pcm) -> Pcm:
        return frame

    def reference(self, samples: Samples, rate: int) -> None:
        pass


class WebRtcEchoCancellation:
    """LiveKit's WebRTC audio processing: echo cancellation fed with every played sample, noise suppression and a
    high-pass filter, as Phase 0 measured it. The APM takes the 24 kHz reference as it is, in 10 ms frames."""

    def __init__(self) -> None:
        from livekit import rtc

        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(
            echo_cancellation=True, noise_suppression=True, high_pass_filter=True, auto_gain_control=False
        )
        self._lock = threading.Lock()
        self._played: deque[tuple[Samples, int]] = deque()
        self._pending = np.zeros(0, np.int16)
        self._rate = 0

    def reference(self, samples: Samples, rate: int) -> None:
        with self._lock:
            self._played.append((samples.copy(), rate))

    def process(self, frame: Pcm) -> Pcm:
        # The APM is driven from this thread alone; the output thread only queues what it played.
        with self._lock:
            played = list(self._played)
            self._played.clear()
        for samples, rate in played:
            if rate != self._rate:
                self._pending, self._rate = np.zeros(0, np.int16), rate
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            self._pending = np.concatenate([self._pending, pcm])
            size = rate // 100
            while len(self._pending) >= size:
                chunk, self._pending = self._pending[:size], self._pending[size:]
                self._apm.process_reverse_stream(self._rtc.AudioFrame(chunk.tobytes(), rate, 1, size))
        captured = self._rtc.AudioFrame(frame.tobytes(), SAMPLE_RATE, 1, FRAME)
        self._apm.process_stream(captured)
        return np.frombuffer(bytes(captured.data), np.int16).copy()


class OpenWakeWord:
    chunk = 1280

    def __init__(self) -> None:
        import openwakeword.utils  # pyright: ignore[reportMissingTypeStubs]
        from openwakeword.model import Model  # pyright: ignore[reportMissingTypeStubs]

        openwakeword.utils.download_models([WAKE_MODEL])  # pyright: ignore[reportUnknownMemberType]
        # Without openWakeWord's own VAD gate, which cost bare "Jarvis" a take in Phase 0.
        self._model: Any = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")

    def score(self, chunk: Pcm) -> float:
        return float(self._model.predict(chunk)[WAKE_MODEL])


class SileroVad:
    chunk = 512

    def __init__(self) -> None:
        from openwakeword.vad import VAD  # pyright: ignore[reportMissingTypeStubs]

        self._vad: Any = VAD()

    def probability(self, chunk: Pcm) -> float:
        return float(self._vad.predict(chunk, self.chunk))


class SmartTurn:
    def __init__(self) -> None:
        import huggingface_hub
        import onnxruntime  # pyright: ignore[reportMissingTypeStubs]

        hub: Any = huggingface_hub
        ort: Any = onnxruntime
        path = hub.hf_hub_download(SMART_TURN_REPO, SMART_TURN_FILE)
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session: Any = ort.InferenceSession(path, sess_options=options)

    def complete(self, audio: Samples) -> float:
        return float(self._session.run(None, {"input_features": log_mel(audio)})[0][0].item())


class Parakeet:
    """Reports no language; the Hub detects it from the text."""

    def __init__(self) -> None:
        import mlx.core as mx
        from parakeet_mlx import from_pretrained  # pyright: ignore[reportMissingTypeStubs]
        from parakeet_mlx.audio import get_logmel  # pyright: ignore[reportMissingTypeStubs]

        mx.set_cache_limit(MLX_CACHE_LIMIT)
        self._mx: Any = mx
        self._get_logmel: Any = get_logmel
        self._model: Any = from_pretrained(PARAKEET_MODEL)

    def transcribe(self, audio: Samples, *, after: float = 0.0) -> str:
        if len(audio) < SAMPLE_RATE // 10:
            return ""
        mel = self._get_logmel(self._mx.array(audio), self._model.preprocessor_config)
        result = self._model.generate(mel)[0]
        if after <= 0:
            return str(result.text).strip()
        return words_after(result.tokens, after)


def words_after(tokens: Any, after: float) -> str:
    """The text of the words ending later than `after`; a token opening with a space starts a word."""
    words: list[list[Any]] = []
    for token in tokens:
        if token.text.startswith(" ") or not words:
            words.append([token])
        else:
            words[-1].append(token)
    kept = [w for w in words if w[-1].end > after + WORD_END_SLACK_S]
    return "".join(t.text for w in kept for t in w).strip().lstrip(",.;:!? ")


def load_engines(choice: EngineChoice) -> Engines:
    """Loads every engine and runs each once, so the first turn does not pay for a warm-up. Blocking: seconds."""
    echo = WebRtcEchoCancellation() if choice.aec == "webrtc" else NoEchoCancellation()
    engines = Engines(echo=echo, wake=OpenWakeWord(), vad=SileroVad(), turn=SmartTurn(), stt=Parakeet(), gate=EveryoneGate())
    noise = np.random.default_rng(0).normal(0, 0.01, SAMPLE_RATE).astype(np.float32)
    pcm = (noise * 32767).astype(np.int16)
    engines.wake.score(pcm[: engines.wake.chunk])
    engines.vad.probability(pcm[: engines.vad.chunk])
    engines.turn.complete(noise)
    engines.stt.transcribe(noise)
    return engines
