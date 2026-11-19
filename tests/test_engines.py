from dataclasses import dataclass

import pytest

from jarvis_hub.voice.engines import words_after


@dataclass
class Token:
    text: str
    start: float
    end: float


# "A Jarvus, cancel that." as parakeet times it: the wake phrase misheard, then the request.
TOKENS = [
    Token(" A", 0.0, 0.16),
    Token(" J", 0.24, 0.32),
    Token("ar", 0.32, 0.48),
    Token("vus", 0.48, 0.64),
    Token(",", 0.64, 0.72),
    Token(" can", 0.88, 1.04),
    Token("cel", 1.04, 1.2),
    Token(" that", 1.28, 1.52),
    Token(".", 1.52, 1.6),
]


@pytest.mark.parametrize(
    ("after", "text"),
    [
        (0.0, "A Jarvus, cancel that."),
        (0.7, "cancel that."),
        (0.66, "cancel that."),
        (1.1, "cancel that."),
        (1.2, "that."),
        (2.0, ""),
    ],
)
def test_words_after_the_wake_word_hit(after: float, text: str) -> None:
    assert words_after(TOKENS, after) == text


@pytest.mark.parametrize('detector_kind', ['wake', 'vad'])
def test_streaming_detector_adapters_reset_the_underlying_context(
    monkeypatch: pytest.MonkeyPatch, detector_kind: str,
) -> None:
    import sys
    from types import ModuleType
    import numpy as np
    from jarvis_hub.voice.engines import OpenWakeWord, SileroVad, WAKE_MODEL

    class StreamingModel:
        def __init__(self, **kwargs: object) -> None:
            self.previous = 0

        def reset(self) -> None:
            self.previous = 0

        def reset_states(self) -> None:
            self.reset()

        def predict(self, chunk: np.ndarray, frame_size: int | None = None) -> object:
            current = int(chunk[0])
            score = float(self.previous == 2000 and current == 1000)
            self.previous = current
            return {WAKE_MODEL: score} if frame_size is None else score

    package, utils, model, vad = (ModuleType(name) for name in
                                 ('openwakeword', 'openwakeword.utils', 'openwakeword.model', 'openwakeword.vad'))
    monkeypatch.setattr(package, 'utils', utils, raising=False)
    def download_models(models: list[str]) -> None:
        pass

    monkeypatch.setattr(utils, 'download_models', download_models, raising=False)
    monkeypatch.setattr(model, 'Model', StreamingModel, raising=False)
    monkeypatch.setattr(vad, 'VAD', StreamingModel, raising=False)
    for module in (package, utils, model, vad):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    detector = OpenWakeWord() if detector_kind == 'wake' else SileroVad()
    score = detector.score if isinstance(detector, OpenWakeWord) else detector.probability
    prefix, suffix = np.full(detector.chunk, 2000, np.int16), np.full(detector.chunk, 1000, np.int16)
    assert score(prefix) == 0
    assert score(suffix) == 1
    score(prefix)
    detector.reset()
    assert score(suffix) == 0
    assert score(prefix) == 0
    assert score(suffix) == 1
