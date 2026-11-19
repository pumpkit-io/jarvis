from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from jarvis_hub.event_log import EventLog
from jarvis_hub.hub import Line
from jarvis_hub.protocol import failure
from jarvis_hub.voice.player import Samples

MAX_AUDIO_BYTES = 9_600_000


class BufferTranscriber(Protocol):
    async def transcribe_buffer(self, audio: Samples) -> str: ...


class TalkHub(Protocol):
    async def begin_remote_talk(self, client_id: str, *, allowed: Callable[[], bool] = lambda: True) -> str: ...
    def cancel_remote_talk(self, client_id: str, request_id: str) -> None: ...
    def set_client_talking(self, client_id: str, talking: bool) -> None: ...
    async def admit_line(self, line: Line, *, allowed: Callable[[], bool] = lambda: True) -> str | None: ...


class TalkError(Exception):
    def __init__(self, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


def rejected(code: str, message: str, retry_after: int | None = None) -> dict[str, Any]:
    error = failure(code, message)["error"]
    error["retry_after_seconds"] = retry_after
    return {"ok": False, "error": error}


@dataclass(eq=False)
class Talk:
    client_id: str
    id: str
    request_id: str
    deadline: float
    state: str = 'recording'
    receiving: bool = False
    timer: asyncio.Task[None] | None = None
    operation: asyncio.Task[dict[str, Any]] | None = None


class UploadedTalks:
    """Own receipt validity independently of the HTTP response and socket generations."""

    def __init__(self, hub: TalkHub, log: EventLog, transcription: BufferTranscriber | None,
                 now: Callable[[], float], sleep: Callable[[float], Awaitable[None]], active: Callable[[str], bool]) -> None:
        self._hub, self._log, self._transcription = hub, log, transcription
        self._now, self._sleep, self._active = now, sleep, active
        self._talks: dict[tuple[str, str], Talk] = {}
        self._current: dict[str, Talk] = {}
        self._rates: dict[str, deque[float]] = {}
        self._closing = False

    async def begin(self, client_id: str, talk_id: str, *, allowed: Callable[[], bool] = lambda: True) -> dict[str, Any]:
        current = self._current.get(client_id)
        if current is not None:
            if self._valid(current):
                raise TalkError('talk_in_progress', 'A Talk is already active')
            self._finish(current, 'expired')
        if not self._log.begin_talk(client_id, talk_id):
            raise TalkError('talk_not_active', 'Talk identity has already been used')
        talk = Talk(client_id, talk_id, '', self._now() + 330)
        self._talks[client_id, talk_id] = self._current[client_id] = talk
        try:
            talk.request_id = await self._hub.begin_remote_talk(client_id, allowed=allowed)
        except BaseException:
            self._finish(talk, 'cancelled')
            raise
        talk.timer = asyncio.create_task(self._expire(talk))
        return {'ok': True, 'result': {'talk_id': talk_id}}

    def cancel(self, client_id: str, talk_id: str) -> dict[str, Any]:
        talk = self._talks.get((client_id, talk_id))
        cancelled = talk is not None and talk.state in ('recording', 'transcribing')
        if cancelled:
            assert talk is not None
            self._finish(talk, 'cancelled')
            if talk.operation is not None:
                talk.operation.cancel()
        return {'ok': True, 'result': {'talk_id': talk_id, 'cancelled': cancelled}}

    def disconnect(self, client_id: str) -> None:
        talk = self._current.get(client_id)
        if talk is not None:
            talk.deadline = min(talk.deadline, self._now() + 30)
            if talk.timer is not None:
                talk.timer.cancel()
            talk.timer = asyncio.create_task(self._expire(talk))

    async def _expire(self, talk: Talk) -> None:
        await self._sleep(max(0, talk.deadline - self._now()))
        if talk.state in ('recording', 'transcribing'):
            self._finish(talk, 'expired')
            if talk.operation is not None:
                talk.operation.cancel()

    def _valid(self, talk: Talk) -> bool:
        return (not self._closing and talk.state in ('recording', 'transcribing')
                and self._now() < talk.deadline and self._active(talk.client_id))

    def receiving(self, client_id: str, talk_id: str) -> None:
        talk = self._talks.get((client_id, talk_id))
        if talk is not None and talk.state == 'recording' and not talk.receiving:
            talk.receiving = True
            talk.deadline = min(talk.deadline, self._now() + 30)
            if talk.timer is not None:
                talk.timer.cancel()
            talk.timer = asyncio.create_task(self._expire(talk))

    def claim(self, client_id: str, talk_id: str) -> Talk:
        talk = self._talks.get((client_id, talk_id))
        if talk is None or not self._valid(talk) or talk.state != 'recording':
            raise TalkError('talk_not_active', 'Talk is cancelled, expired or already uploaded')
        rate = self._rates.setdefault(client_id, deque())
        while rate and rate[0] <= self._now() - 60:
            rate.popleft()
        if len(rate) >= 10:
            self._finish(talk, 'failed')
            raise TalkError('upload_rate_limited', 'At most 10 new uploads per minute', retry_after=max(1, int(60 - (self._now() - rate[0])) + 1))
        rate.append(self._now())
        talk.state = 'transcribing'
        talk.deadline = min(talk.deadline, self._now() + 30)
        if talk.timer is not None:
            talk.timer.cancel()
        talk.timer = asyncio.create_task(self._expire(talk))
        return talk

    def _finish(self, talk: Talk, state: str) -> None:
        talk.state = state
        self._talks.pop((talk.client_id, talk.id), None)
        self._log.finish_talk(talk.client_id, talk.id, talk.request_id, state)
        if talk.timer is not None and talk.timer is not asyncio.current_task():
            talk.timer.cancel()
        if self._current.get(talk.client_id) is talk:
            self._current.pop(talk.client_id, None)
            if state == 'admitted':
                self._hub.set_client_talking(talk.client_id, False)
            else:
                self._hub.cancel_remote_talk(talk.client_id, talk.request_id)

    async def upload(self, talk: Talk, body: bytearray) -> dict[str, Any]:
        audio: Samples | None = None
        try:
            if self._transcription is None:
                return rejected('transcription_failed', 'Transcription is unavailable')
            audio = np.frombuffer(body, dtype='<i2').astype(np.float32)
            audio /= np.float32(32768)
            body.clear()
            text = await self._transcription.transcribe_buffer(audio)
            audio = None
            if not self._valid(talk):
                return rejected('talk_not_active', 'Talk is cancelled or expired')
            if not text.strip():
                return rejected('empty_transcript', 'No speech was transcribed')
            request_id = await self._hub.admit_line(
                Line(text, self._now(), typed=False, client_id=talk.client_id,
                     request_id=talk.request_id, segments=(text,)), allowed=lambda: self._valid(talk))
            if request_id is None:
                return rejected('talk_not_active', 'A newer request superseded this Talk')
            self._finish(talk, 'admitted')
            return {'ok': True, 'result': {'talk_id': talk.id, 'request_id': request_id}}
        except asyncio.CancelledError:
            return rejected('talk_not_active', 'Talk is cancelled or expired')
        except Exception:
            return rejected('transcription_failed', 'Transcription could not be completed')
        finally:
            audio = None
            body.clear()
            talk.operation = None
            if talk.state != 'admitted':
                self._finish(talk, talk.state if talk.state in ('expired', 'cancelled') else 'failed')

    async def close(self) -> None:
        self._closing = True
        timers = [talk.timer for talk in self._talks.values() if talk.timer is not None]
        for talk in tuple(self._current.values()):
            self._finish(talk, 'cancelled')
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
