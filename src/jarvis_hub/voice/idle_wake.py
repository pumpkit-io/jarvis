"""Bounded idle utterances sent to a local recognizer without blocking capture."""

import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from jarvis_hub.voice.player import Samples

Pcm = NDArray[np.int16]
MAX_RETAINED_FRAMES = 4200


@dataclass(frozen=True)
class WakeMatch:
    accepted: bool
    phrase_only: bool = False


class IdleRecognizer(Protocol):
    def recognizes(self, audio: Samples) -> WakeMatch: ...
    def close(self) -> None: ...


@dataclass
class IdleUtterance:
    identity: int
    epoch: int
    owner: int
    loss_epoch: int
    start: float
    onset: float
    speech_at: float
    frames: list[Pcm]
    announced: bool = False
    phrase_only: bool = False
    recognized_speech_at: float = float('-inf')

    @property
    def wake_only(self) -> bool:
        return self.phrase_only and self.speech_at == self.recognized_speech_at


@dataclass(frozen=True)
class IdleResult:
    utterance: IdleUtterance
    accepted: bool
    error: str | None = None
    phrase_only: bool = False


class IdleWake:
    def __init__(
        self, recognizer: IdleRecognizer, completed: Callable[[IdleResult], None],
        record: Callable[[dict[str, Any]], None],
    ) -> None:
        self._recognizer, self._completed, self._record = recognizer, completed, record
        self._jobs: queue.Queue[tuple[IdleUtterance, Samples] | None] = queue.Queue(maxsize=1)
        self._pending: dict[int, IdleUtterance] = {}
        self._lead: deque[tuple[Pcm, float]] = deque(maxlen=25)
        self._active: IdleUtterance | None = None
        self._sequence = 0
        self._enabled = True
        self._epoch = self._owner = self._loss_epoch = 0
        self._closed = False
        self._blocked = False
        self._last_speech = float('-inf')
        self._thread = threading.Thread(target=self._run, name='idle-wake', daemon=True)
        self._thread.start()

    def mode(self, enabled: bool, epoch: int, owner: int | None, loss_epoch: int) -> None:
        self.invalidate('mode_changed')
        self._enabled = enabled
        self._epoch, self._owner, self._loss_epoch = epoch, owner if owner is not None else 0, loss_epoch

    def invalidate(self, reason: str, *, loss_epoch: int | None = None) -> None:
        if self._active is not None or self._pending:
            self._record({'kind': 'idle_wake', 'stage': 'invalidated', 'reason': reason})
        self._active = None
        self._pending.clear()
        self._lead.clear()
        self._blocked = reason == 'overflow'
        if loss_epoch is not None:
            self._loss_epoch = loss_epoch

    def loss(self, epoch: int) -> None:
        if epoch != self._loss_epoch:
            self.invalidate('overflow', loss_epoch=epoch)

    def observe(self, frame: Pcm, at: float, vad: list[tuple[float, bool]]) -> None:
        if not self._enabled or self._closed:
            return
        frame = frame.copy()
        self._lead.append((frame, at))
        for identity, utterance in list(self._pending.items()):
            utterance.frames.append(frame)
            if len(utterance.frames) > MAX_RETAINED_FRAMES:
                del self._pending[identity]
                self._record({'kind': 'idle_wake', 'stage': 'rejected', 'reason': 'retention_limit'})
        if self._active is not None:
            self._active.frames.append(frame)
        for ends, speech in vad:
            if not speech:
                continue
            self._last_speech = ends
            for utterance in self._pending.values():
                utterance.speech_at = ends
            if self._active is None and not self._blocked:
                self._sequence += 1
                onset = ends - .032
                frames = [(pcm, stamp) for pcm, stamp in self._lead if stamp > onset - .2]
                first = frames[0][1] - .01
                self._active = IdleUtterance(self._sequence, self._epoch, self._owner, self._loss_epoch,
                                             first, onset, ends, [x[0] for x in frames])
            elif self._active is not None:
                self._active.speech_at = ends
        if at - self._last_speech >= .5:
            self._blocked = False
        utterance = self._active
        if utterance is None:
            return
        if at - utterance.onset >= 30:
            self._active = None
            self._blocked = True
            self._record({'kind': 'idle_wake', 'stage': 'rejected', 'reason': 'duration_limit'})
            return
        if at - utterance.speech_at < .5:
            return
        self._active = None
        if utterance.speech_at - utterance.onset < .2:
            self._record({'kind': 'idle_wake', 'stage': 'rejected', 'reason': 'short_span'})
            return
        if len(self._pending) >= 2:
            self._record({'kind': 'idle_wake', 'stage': 'rejected', 'reason': 'queue_limit'})
            return
        audio = np.concatenate(utterance.frames).astype(np.float32) / np.float32(32768)
        utterance.recognized_speech_at = utterance.speech_at
        try:
            self._jobs.put_nowait((utterance, audio))
        except queue.Full:
            self._record({'kind': 'idle_wake', 'stage': 'rejected', 'reason': 'queue_limit'})
            return
        self._pending[utterance.identity] = utterance
        self._record({'kind': 'idle_wake', 'stage': 'submitted', 'candidate': utterance.identity,
                      'source_onset_at': utterance.onset, 'source_speech_at': utterance.speech_at,
                      'captured_through_at': at})

    def result(self, result: IdleResult) -> bool:
        utterance = self._pending.get(result.utterance.identity)
        if utterance is None or utterance is not result.utterance:
            return False
        if not result.accepted:
            del self._pending[utterance.identity]
            self._record({'kind': 'idle_wake', 'stage': 'rejected', 'reason': result.error or 'no_match'})
            return False
        utterance.announced = True
        utterance.phrase_only = result.phrase_only
        return True

    def take(self, identity: int) -> IdleUtterance | None:
        utterance = self._pending.pop(identity, None)
        return utterance if utterance is not None and utterance.announced else None

    def available(self, identity: int) -> bool:
        utterance = self._pending.get(identity)
        return not self._closed and utterance is not None and utterance.announced

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._recognizer.close()
        try:
            self._jobs.get_nowait()
        except queue.Empty:
            pass
        self._jobs.put_nowait(None)
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            utterance, audio = job
            try:
                match = self._recognizer.recognizes(audio)
                result = IdleResult(utterance, match.accepted, phrase_only=match.phrase_only)
            except Exception as error:
                result = IdleResult(utterance, False, type(error).__name__)
            if not self._closed:
                self._completed(result)
