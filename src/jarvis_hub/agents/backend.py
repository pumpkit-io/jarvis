"""The seam between the Hub and the agent sessions it runs (Talker now; Orchestrator and Subagents later).

A backend opens sessions from a SessionSpec. A session takes one message at a time and streams that turn
as AgentEvents, always ending with exactly one TurnEnd.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class HubTool:
    """A tool the Hub answers in-process, without a model call."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass(frozen=True)
class SessionSpec:
    role: str
    model: str
    effort: str
    thinking: bool
    system_prompt: str
    cwd: Path
    tools: Sequence[HubTool] = ()


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class TextEnd:
    """A text block closed. Blocks either side of a tool call arrive with no space between them."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class TurnEnd:
    """`interrupted` comes from the session's terminal reason, never from its error flag."""

    outcome: Literal["completed", "interrupted", "failed"]
    usage: Usage = Usage()
    error: str | None = None


AgentEvent = TextDelta | TextEnd | ToolCall | TurnEnd


class AgentSession(Protocol):
    def send(self, message: str) -> AsyncIterator[AgentEvent]:
        """Send one user-role message and stream the turn it starts."""
        ...

    async def interrupt(self) -> None:
        """Stop the current turn. Deltas may still arrive before its TurnEnd."""
        ...

    async def close(self) -> None: ...


class AgentBackend(Protocol):
    async def open(self, spec: SessionSpec) -> AgentSession: ...
