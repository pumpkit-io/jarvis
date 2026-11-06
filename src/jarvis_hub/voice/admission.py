"""Opt-in contextual admission, with no authority to choose or cancel Tasks."""

import asyncio
import json
from pathlib import Path
import shutil
import time
from collections.abc import Callable
from typing import Protocol, cast

from jarvis_hub.voice.jev import JevAssessor
from jarvis_hub.voice.assessment import Assessment, Candidate, SegmentAction, Verdict


class ClassifierFailure(Exception):
    def __init__(self, reason: str = "unavailable") -> None:
        self.reason = reason if reason in ("rate_limited", "subscription_required", "unavailable") else "unavailable"
        super().__init__(self.reason)


class ClassifierBackend(Protocol):
    provider: str
    model: str

    async def prepare(self) -> None: ...
    async def classify(self, payload: str) -> str: ...
    async def close(self) -> None: ...


def observation(candidate: Candidate) -> str:
    segments = candidate.observed_segments
    if not 1 <= len(segments) <= 4 or any(not text.strip() for text in segments):
        raise ValueError("context_limit")
    payload = json.dumps(dict(admitted_turn=candidate.admitted_turn, played_text=candidate.played_text,
                              phase=candidate.phase, started_before_reply=candidate.started_before_reply,
                              observed_segments=segments), ensure_ascii=False, separators=(",", ":"))
    if len(payload) > 6000:
        raise ValueError("context_limit")
    return payload


def segment_actions(raw: str, count: int) -> tuple[SegmentAction, ...]:
    if len(raw.encode("utf-8")) > 2048:
        raise ValueError("invalid")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("invalid")
    items = cast(list[object], parsed)
    if len(items) != count or any(type(item) is not str or item not in ("floor", "stop", "ignore", "uncertain") for item in items):
        raise ValueError("invalid")
    return tuple(cast(list[SegmentAction], items))


class CloudAssessor:
    def __init__(self, backend: ClassifierBackend, *, notice: Callable[[str], None]) -> None:
        self._backend, self._notice = backend, notice
        self._state = "not_started"
        self._preparing: asyncio.Task[None] | None = None
        self._query: asyncio.Task[str] | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def ready(self) -> bool:
        return self._state == "ready" and not self._closed

    def _status(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        messages = {
            "loading": "Cloud admission loading; candidate transcripts and bounded context may leave this machine.",
            "ready": "Cloud admission ready.",
            "rate_limited": "Cloud admission usage-limited; existing replies continue. Restart after the provider limit resets.",
            "subscription_required": "Cloud admission needs an existing first-party subscription login. Restart after signing in.",
            "unavailable": "Cloud admission unavailable; existing replies continue. Restart to check the classifier again.",
        }
        if state in messages:
            self._notice(messages[state])

    def start(self) -> None:
        if self._state == "not_started" and not self._closed:
            self._preparing = asyncio.create_task(self._prepare())

    async def _prepare(self) -> None:
        self._status("loading")
        try:
            await self._backend.prepare()
        except ClassifierFailure as error:
            self._status(error.reason)
        except Exception:
            self._status("unavailable")
        else:
            if not self._closed:
                self._status("ready")

    async def _finish(self, query: asyncio.Task[str], *, recover: bool) -> None:
        try:
            await self._backend.close()
            await asyncio.gather(query, return_exceptions=True)
        except Exception:
            if self._state not in ("rate_limited", "subscription_required", "closed"):
                self._status("unavailable")
            await asyncio.gather(query, return_exceptions=True)
            return
        self._query = None
        if recover and not self._closed:
            await self._prepare()

    async def assess(self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0) -> Assessment:
        started = time.perf_counter()
        def result(verdict: Verdict, reason: str, actions: tuple[SegmentAction, ...] | None = None) -> Assessment:
            return Assessment(candidate.candidate_id, candidate.turn_id, verdict, reason,
                              (time.perf_counter() - started) * 1000, actions)
        token = candidate.candidate_id, candidate.turn_id
        if current() != token:
            return result("uncertain", "stale")
        if self._query is not None:
            return result("uncertain", "busy")
        if not self.ready:
            return result("uncertain", self._state)
        try:
            payload = observation(candidate)
        except (ValueError, UnicodeError):
            self._notice("Cloud admission context limit reached. Repeat as a fresh request or use typed input.")
            return result("uncertain", "context_limit")
        query = self._query = asyncio.create_task(self._backend.classify(payload))
        try:
            remaining = max(0.0, min(timeout_s, 2.0) - (time.perf_counter() - started))
            done, _ = await asyncio.wait({query}, timeout=remaining)
            stale = current() != token
            if not done:
                query.cancel()
                self._status("loading" if stale else "unavailable")
                self._cleanup = asyncio.create_task(self._finish(query, recover=stale))
                return result("uncertain", "stale" if stale else "timeout")
            raw = query.result()
            if stale:
                return result("uncertain", "stale")
            if time.perf_counter() - started >= min(timeout_s, 2.0):
                self._status("unavailable")
                return result("uncertain", "timeout")
            actions = segment_actions(raw, len(candidate.observed_segments))
            verdict: Verdict = "directed" if "floor" in actions else "stop_reply" if "stop" in actions else "uncertain" if "uncertain" in actions else "unrelated"
            return result(verdict, "assessed", actions)
        except (ValueError, UnicodeError):
            self._status("unavailable")
            self._cleanup = asyncio.create_task(self._finish(query, recover=False))
            return result("uncertain", "invalid")
        except ClassifierFailure as error:
            self._status(error.reason)
            self._cleanup = asyncio.create_task(self._finish(query, recover=False))
            return result("uncertain", error.reason)
        except asyncio.CancelledError:
            query.cancel()
            self._cleanup = asyncio.create_task(self._finish(query, recover=False))
            raise
        except Exception:
            self._status("unavailable")
            self._cleanup = asyncio.create_task(self._finish(query, recover=False))
            return result("uncertain", "unavailable")
        finally:
            if query.done() and (self._cleanup is None or self._cleanup.done()):
                self._query = None

    async def close(self) -> None:
        self._closed = True
        self._status("closed")
        if self._preparing is not None and not self._preparing.done():
            self._preparing.cancel()
            await asyncio.gather(self._preparing, return_exceptions=True)
        if self._query is not None:
            self._query.cancel()
        await self._backend.close()
        if self._cleanup is not None:
            await asyncio.gather(self._cleanup, return_exceptions=True)
        elif self._query is not None:
            await asyncio.gather(self._query, return_exceptions=True)
        self._query = None


def configured_assessor(backend: str, model: str, prompt_path: Path, *, notice: Callable[[str], None],
                        api_key: str = "", record: Callable[[dict[str, object]], None] = lambda row: None) -> CloudAssessor | JevAssessor | None:
    if backend == "jev":
        return JevAssessor(api_key, notice=notice, record=record)
    if backend == "off":
        notice("Cloud admission disabled.")
        return None
    if backend != "claude":
        raise ValueError("Unsupported admission backend; no fallback was selected.")
    cli = shutil.which("claude")
    if cli is None:
        notice("Cloud admission unavailable: install Claude Code and sign in with your subscription, then restart.")
        return None
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        notice("Cloud admission unavailable: its observer prompt could not be read. Restore it and restart.")
        return None
    from jarvis_hub.agents.classifier import ClaudeClassifier
    return CloudAssessor(ClaudeClassifier(cli, model, prompt), notice=notice)
