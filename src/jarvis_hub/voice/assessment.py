"""Local assessment contract; no authority over conversational admission or playback."""

import asyncio
import multiprocessing
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal

Relation = Literal["stop_reply", "amend", "new_request", "unrelated", "uncertain"]
Verdict = Literal["directed", "stop_reply", "amend", "new_request", "unrelated", "uncertain"]
RELATIONS: tuple[Verdict, ...] = ("stop_reply", "amend", "new_request", "unrelated", "uncertain")


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    turn_id: str
    transcript: str
    admitted_turn: str
    played_text: str
    phase: str | None = None
    started_before_reply: bool | None = None
    observed_segments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assessment:
    candidate_id: str
    turn_id: str
    verdict: Verdict
    reason: str
    elapsed_ms: float


def _serve(connection: Connection, recognize: Callable[[Candidate], str], prepare: Callable[[], None] | None) -> None:
    try:
        if prepare is not None:
            prepare()
        connection.send(("ready", "ready"))
        while True:
            candidate = connection.recv()
            try:
                verdict = recognize(candidate)
                connection.send((verdict, "assessed"))
            except Exception:
                connection.send(("uncertain", "unavailable"))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class AssessmentWorker:
    """One bounded in-flight candidate; stalled native inference can be terminated with its process."""

    def __init__(
        self, recognize: Callable[[Candidate], str], *,
        verdicts: tuple[Verdict, ...] = ("directed", "unrelated", "uncertain"),
        prepare: Callable[[], None] | None = None,
    ) -> None:
        self._recognize = recognize
        self._verdicts = verdicts
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._busy = False
        self._prepare = prepare
        self._ready = False
        self._warming: asyncio.Task[None] | None = None
        self._readiness_failed = False
        self._background_started = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def warming(self) -> bool:
        return self._warming is not None and not self._warming.done()

    def start(self, *, readiness_s: float = 30.0) -> None:
        if self._process is None and self._warming is None and not self._readiness_failed:
            self._background_started = True
            self._warming = asyncio.create_task(self._warm(readiness_s))

    def _spawn(self) -> None:
        context = multiprocessing.get_context("spawn")
        connection, child = context.Pipe()
        process = context.Process(target=_serve, args=(child, self._recognize, self._prepare), daemon=True)
        process.start()
        child.close()
        self._process, self._connection = process, connection

    async def _wait_ready(self, timeout_s: float) -> None:
        async with asyncio.timeout(timeout_s):
            while self._connection is not None and not self._connection.poll():
                if self._process is None or not self._process.is_alive():
                    raise EOFError
                await asyncio.sleep(0.005)
            if self._connection is None or self._connection.recv() != ("ready", "ready"):
                raise EOFError
            self._ready = True

    async def _warm(self, readiness_s: float) -> None:
        try:
            self._spawn()
            await self._wait_ready(readiness_s)
        except (TimeoutError, EOFError, BrokenPipeError, OSError):
            self._readiness_failed = True
            await self._stop()

    async def assess(
        self, candidate: Candidate, *, current: Callable[[], tuple[str, str]], timeout_s: float = 2.0
    ) -> Assessment:
        start = time.perf_counter()

        def result(verdict: Verdict, reason: str) -> Assessment:
            return Assessment(candidate.candidate_id, candidate.turn_id, verdict, reason,
                              (time.perf_counter() - start) * 1000)

        if current() != (candidate.candidate_id, candidate.turn_id):
            return result("uncertain", "stale")
        fields = (candidate.candidate_id, candidate.turn_id, candidate.transcript,
                  candidate.admitted_turn, candidate.played_text, candidate.phase or "")
        if sum(map(len, fields)) > 6000:
            return result("uncertain", "context_limit")
        if self.warming:
            return result("uncertain", "warming")
        if self._readiness_failed:
            return result("uncertain", "readiness_failed")
        if self._busy:
            return result("uncertain", "busy")
        self._busy = True
        try:
            if self._connection is None:
                self._spawn()
            if not self._ready:
                await self._wait_ready(timeout_s)
            connection = self._connection
            assert connection is not None
            await asyncio.wait_for(asyncio.to_thread(connection.send, candidate), timeout=max(0.001, timeout_s))
            while not connection.poll():
                if time.perf_counter() - start >= timeout_s:
                    await self.close()
                    self._readiness_failed = self._background_started
                    return result("uncertain", "timeout")
                if self._process is None or not self._process.is_alive():
                    await self.close()
                    self._readiness_failed = self._background_started
                    return result("uncertain", "unavailable")
                await asyncio.sleep(0.005)
            if time.perf_counter() - start >= timeout_s:
                await self.close()
                self._readiness_failed = self._background_started
                return result("uncertain", "timeout")
            verdict, reason = connection.recv()
            if current() != (candidate.candidate_id, candidate.turn_id):
                return result("uncertain", "stale")
            if verdict not in self._verdicts:
                return result("uncertain", "invalid")
            return result(verdict, reason)
        except TimeoutError:
            await self.close()
            self._readiness_failed = self._background_started
            return result("uncertain", "timeout")
        except (EOFError, BrokenPipeError, OSError):
            await self.close()
            self._readiness_failed = self._background_started
            return result("uncertain", "unavailable")
        except asyncio.CancelledError:
            await self.close()
            raise
        finally:
            self._busy = False

    async def close(self) -> None:
        warming, self._warming = self._warming, None
        if warming is not None and not warming.done():
            warming.cancel()
            await asyncio.gather(warming, return_exceptions=True)
        await self._stop()

    async def _stop(self) -> None:
        self._ready = False
        process, connection = self._process, self._connection
        self._process, self._connection = None, None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 0.2)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 0.2)
            process.close()
