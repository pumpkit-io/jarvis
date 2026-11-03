"""The Hub core: turns User input into Talker turns and streams the replies out.

Every outside dependency is injected (agent backend, clock, output, metrics) so tests and text mode share
this one code path; voice mode will add audio adapters around it.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import asdict
from html import escape
from typing import Any, Protocol

from jarvis_hub.agents.backend import (
    AgentBackend,
    AgentSession,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
)


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...


class Output(Protocol):
    def reply(self, text: str) -> None:
        """A piece of the Talker's reply, shown as soon as it arrives."""
        ...

    def reply_end(self) -> None: ...

    def notice(self, text: str) -> None:
        """A line from the Hub itself, never from the Talker."""
        ...


class Metrics(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


# Sent once at launch so the first real turn finds the prompt prefix cached; its reply is never shown.
WARM_UP = '<event source="hub">Session start. Reply with one word.</event>'

# The Phase 0 definition, kept so Hub numbers compare with the spike's: the end of a delta counts too.
_SENTENCE_END = re.compile(r"[.?!](\s|$)")


def user_message(text: str) -> str:
    # Escaped so typed text can never close the tag or forge another one.
    return f'<user typed="true">{escape(text, quote=False)}</user>'


class Hub:
    def __init__(
        self,
        *,
        talker: SessionSpec,
        backend: AgentBackend,
        clock: Clock,
        output: Output,
        metrics: Metrics,
    ) -> None:
        self._spec = talker
        self._backend = backend
        self._clock = clock
        self._output = output
        self._metrics = metrics
        self._talker: AgentSession | None = None
        self._replying = False
        self._interrupting = False

    async def start(self) -> None:
        """Open the launch's one Talker session and warm it before the User's first turn."""
        self._talker = await self._backend.open(self._spec)
        await self._turn(WARM_UP, kind="warm_up", show=False)

    async def run(self, inputs: AsyncIterator[str]) -> None:
        async for line in inputs:
            if line.strip():
                await self._turn(user_message(line.strip()), kind="user", show=True)

    async def interrupt(self) -> bool:
        """Stop the reply in progress, if any. Returns whether there was one."""
        if not self._replying or self._talker is None:
            return False
        self._interrupting = True
        await self._talker.interrupt()
        return True

    async def close(self) -> None:
        if self._talker is not None:
            await self._talker.close()
            self._talker = None

    async def _turn(self, message: str, *, kind: str, show: bool) -> None:
        assert self._talker is not None, "start() first"
        t0 = self._clock.now()
        shown, block = "", ""
        first_delta: float | None = None
        first_sentence: float | None = None
        tool_before_text = False
        after_block = False
        end = TurnEnd("failed", error="the session ended without a result")
        self._replying, self._interrupting = True, False
        try:
            async for event in self._talker.send(message):
                match event:
                    case TextDelta(text) if not self._interrupting:
                        if first_delta is None:
                            first_delta = self._clock.now()
                        if after_block and shown and not shown[-1].isspace() and not text[:1].isspace():
                            text = " " + text
                        after_block = False
                        shown += text
                        block += text
                        if first_sentence is None and _SENTENCE_END.search(block):
                            first_sentence = self._clock.now()
                        if show:
                            self._output.reply(text)
                    case TextEnd():
                        if first_sentence is None and block.strip():
                            first_sentence = self._clock.now()
                        block, after_block = "", True
                    case ToolCall():
                        if first_delta is None:
                            tool_before_text = True
                    case TurnEnd():
                        end = event
                    case _:
                        pass
        finally:
            self._replying = False
        if show:
            self._output.reply_end()
        if end.outcome == "failed":
            self._output.notice(f"The Talker failed: {end.error}")

        def ms(t: float | None) -> int | None:
            return None if t is None else round((t - t0) * 1000)

        self._metrics.record(
            {
                "kind": kind,
                "role": self._spec.role,
                "model": self._spec.model,
                "effort": self._spec.effort,
                "first_delta_ms": ms(first_delta),
                "first_sentence_ms": ms(first_sentence),
                "total_ms": ms(self._clock.now()),
                "tool_before_text": tool_before_text,
                "outcome": end.outcome,
                "usage": asdict(end.usage),
            }
        )
