"""Local assessment contract; no authority over conversational admission or playback."""

import asyncio
import multiprocessing
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal

Verdict = Literal["directed", "unrelated", "uncertain"]


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    turn_id: str
    transcript: str
    admitted_turn: str
    played_text: str


@dataclass(frozen=True)
class Assessment:
    candidate_id: str
    turn_id: str
    verdict: Verdict
    reason: str
    elapsed_ms: float


def _serve(connection: Connection, recognize: Callable[[Candidate], str]) -> None:
    try:
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

    def __init__(self, recognize: Callable[[Candidate], str]) -> None:
        self._recognize = recognize
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._busy = False

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
                  candidate.admitted_turn, candidate.played_text)
        if sum(map(len, fields)) > 6000:
            return result("uncertain", "context_limit")
        if self._busy:
            return result("uncertain", "busy")
        self._busy = True
        try:
            if self._connection is None:
                context = multiprocessing.get_context("spawn")
                connection, child = context.Pipe()
                process = context.Process(target=_serve, args=(child, self._recognize), daemon=True)
                process.start()
                child.close()
                self._process, self._connection = process, connection
            connection = self._connection
            await asyncio.wait_for(asyncio.to_thread(connection.send, candidate), timeout=max(0.001, timeout_s))
            while not connection.poll():
                if time.perf_counter() - start >= timeout_s:
                    await self.close()
                    return result("uncertain", "timeout")
                if self._process is None or not self._process.is_alive():
                    await self.close()
                    return result("uncertain", "unavailable")
                await asyncio.sleep(0.005)
            if time.perf_counter() - start >= timeout_s:
                await self.close()
                return result("uncertain", "timeout")
            verdict, reason = connection.recv()
            if current() != (candidate.candidate_id, candidate.turn_id):
                return result("uncertain", "stale")
            if verdict not in ("directed", "unrelated", "uncertain"):
                return result("uncertain", "invalid")
            if verdict == "directed":
                return result("directed", reason)
            if verdict == "unrelated":
                return result("unrelated", reason)
            return result("uncertain", reason)
        except TimeoutError:
            await self.close()
            return result("uncertain", "timeout")
        except (EOFError, BrokenPipeError, OSError):
            await self.close()
            return result("uncertain", "unavailable")
        except asyncio.CancelledError:
            await self.close()
            raise
        finally:
            self._busy = False

    async def close(self) -> None:
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
