"""The seam between the Hub and its agent sessions: each turn streams as AgentEvents ending in exactly one TurnEnd."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from jarvis_hub.config import Effort

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Refused:
    reason: str


# Parses a Bash command into the words to run, or refuses it; the backend runs those words and nothing else.
BashGuard = Callable[[str], Sequence[str] | Refused]


@dataclass(frozen=True)
class HubTool:
    """A tool the Hub answers in-process, without a model call."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


def object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """A tool's input schema: an object with these properties."""
    return {"type": "object", "properties": properties, "required": list(required)}


STRING: dict[str, Any] = {"type": "string"}


@dataclass(frozen=True)
class SessionSpec:
    """One session's setup. Nothing outside it is allowed: no settings, no connectors, no unlisted tool."""

    role: str
    model: str
    effort: Effort
    thinking: bool
    system_prompt: str
    cwd: Path
    tools: Sequence[HubTool] = ()
    # Appends system_prompt to Claude Code's own prompt instead of replacing it.
    preset: bool = False
    # Built-in tools the session may use without asking, such as Read.
    read_tools: Sequence[str] = ()
    # Gives the session Bash, running only the commands this guard lets through.
    bash_guard: BashGuard | None = None
    # Model turns allowed per message.
    max_turns: int | None = None


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
