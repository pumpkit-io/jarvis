"""The saved Conversation (ADR 0028): every exchange the Talker had, kept across Hub restarts and seeded into each
new Talker session.

The file holds the active summary's pointer and the entries since the last cut, so a Fold commits in one atomic write
that moves the pointer and drops the folded entries. No Fold exists yet, so the pointer is always None.
"""

import json
import math
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Literal, cast

from jarvis_hub.state import one_of, write_atomically

VERSION = 1
# How much of the Conversation a new Talker reads, in estimated tokens of the rendered exchanges.
LAUNCH_TOKENS = 200_000
RESTART_TOKENS = 40_000
# A starting point for mixed English and Italian; the Hub corrects it from the context sizes the CLI reports.
CHARS_PER_TOKEN = 3.5
# Bounds on the corrected ratio, so one odd reading cannot make the estimate absurd.
_RATIO_RANGE = (1.5, 8.0)
# How far one reading moves the ratio.
_RATIO_WEIGHT = 0.3

Device = Literal["Mac", "iPhone"]
EntryKind = Literal["user", "event"]
SeedReason = Literal["launch", "restart"]


@dataclass(frozen=True)
class Entry:
    """One exchange: the message exactly as the Talker got it, and its reply. Times are wall-clock seconds."""

    message: str
    reply: str
    # The names of the tools the Talker called, in order.
    tools: tuple[str, ...]
    # Where the User's message came from; for Hub news, the Focus Client it was told on.
    device: Device
    kind: EntryKind
    # When the message arrived and when the reply ended.
    at: float
    ended: float
    tokens: int

    def render(self) -> str:
        stamp = datetime.fromtimestamp(self.at).astimezone().strftime("%Y-%m-%d %H:%M")
        tools = f"\n<tools>{', '.join(self.tools)}</tools>" if self.tools else ""
        return (
            f'<exchange device="{self.device}" at="{stamp}">\n{self.message}{tools}\n'
            f"<you>{escape(self.reply, quote=False)}</you>\n</exchange>"
        )

    def dump(self) -> dict[str, Any]:
        return {**asdict(self), "tools": list(self.tools)}

    @staticmethod
    def load(saved: dict[str, Any]) -> "Entry":
        """A missing or malformed field raises KeyError, TypeError or ValueError."""
        tools = saved["tools"]
        if not isinstance(tools, list) or not all(isinstance(name, str) for name in cast(list[object], tools)):
            raise ValueError("tools must be a list of names")
        texts = saved["message"], saved["reply"]
        if not all(isinstance(text, str) for text in texts):
            raise ValueError("message and reply must be text")
        return Entry(
            message=saved["message"],
            reply=saved["reply"],
            tools=tuple(cast(list[str], tools)),
            device=one_of(saved["device"], ("Mac", "iPhone")),
            kind=one_of(saved["kind"], ("user", "event")),
            at=float(saved["at"]),
            ended=float(saved["ended"]),
            tokens=int(saved["tokens"]),
        )


class UnreadableConversation(Exception):
    """The saved conversation could not be used and was moved aside; the message says where, for the User."""


class Conversation:
    """The Conversation's entries in order, saved after each one. Without a path nothing is saved or loaded."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: list[Entry] = []
        self._ratio = CHARS_PER_TOKEN

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def load(self) -> None:
        """Reads the saved conversation, if any. A damaged one is moved aside and raises UnreadableConversation,
        leaving the Conversation empty."""
        if self._path is None:
            return
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return
        except OSError as e:
            raise UnreadableConversation(f"Could not read {self._path}: {e}; starting with an empty conversation.") from e
        try:
            data: object = json.loads(text)
        except ValueError:
            raise UnreadableConversation(self._set_aside("is not valid JSON")) from None
        version = cast(dict[str, Any], data).get("version") if isinstance(data, dict) else None
        if version != VERSION:
            raise UnreadableConversation(self._set_aside(f"has version {version!r}, not {VERSION}"))
        saved = cast(dict[str, Any], data)
        try:
            entries = [Entry.load(record) for record in saved["entries"]]
            ratio = float(saved.get("chars_per_token", CHARS_PER_TOKEN))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise UnreadableConversation(self._set_aside(f"could not be read whole ({type(e).__name__}: {e})")) from None
        self._entries, self._ratio = entries, _bounded(ratio)

    def add(self, message: str, reply: str, *, tools: Sequence[str], device: Device, kind: EntryKind,
            at: float, ended: float) -> Entry:
        """Appends an exchange and saves the Conversation; a failed save raises OSError with the entry kept in memory."""
        entry = Entry(message, reply, tuple(tools), device, kind, at, ended, tokens=0)
        entry = replace(entry, tokens=self.estimate(entry.render()))
        self._entries.append(entry)
        self._save()
        return entry

    def newest(self, tokens: int) -> list[Entry]:
        """The latest entries, oldest first, whose estimates add up to no more than `tokens`."""
        taken: list[Entry] = []
        total = 0
        for entry in reversed(self._entries):
            if total + entry.tokens > tokens:
                break
            taken.append(entry)
            total += entry.tokens
        return taken[::-1]

    def estimate(self, text: str) -> int:
        return math.ceil(len(text) / self._ratio)

    def calibrate(self, chars: int, tokens: int) -> None:
        """Moves the estimate towards `chars` characters for every `tokens` tokens, as one turn's context measured."""
        if chars > 0 and tokens > 0:
            self._ratio = _bounded((1 - _RATIO_WEIGHT) * self._ratio + _RATIO_WEIGHT * chars / tokens)

    def _save(self) -> None:
        if self._path is None:
            return
        write_atomically(self._path, {
            "version": VERSION,
            # The summary version a Fold made active; None until the first Fold.
            "active_summary": None,
            "chars_per_token": self._ratio,
            "entries": [entry.dump() for entry in self._entries],
        }, indent=None)

    def _set_aside(self, problem: str) -> str:
        assert self._path is not None
        aside = self._path.with_name(f"{self._path.stem}.unreadable{self._path.suffix}")
        try:
            os.replace(self._path, aside)
        except OSError as e:
            return (f"The saved conversation {self._path} {problem} and could not be moved aside ({e}); "
                    "Jarvis starts with an empty conversation.")
        return f"The saved conversation {self._path} {problem}, so Jarvis starts with an empty conversation; it was moved to {aside}."


def _bounded(ratio: float) -> float:
    low, high = _RATIO_RANGE
    return min(high, max(low, ratio))


_OPENING: dict[SeedReason, str] = {
    "launch": "Jarvis has just started. Below is the Conversation with the User so far, as the Hub saved it.",
    "restart": (
        "Your session was restarted to keep it fast, and what you said before it is gone. Below are the latest "
        "exchanges of the Conversation, as the Hub saved them."
    ),
}


def seed_message(
    reason: SeedReason,
    entries: Sequence[Entry],
    *,
    open_tasks: str,
    authorizations: str,
    approvals: str,
    scopes: str,
) -> str:
    """The hidden first message of a new Talker session. What never changes comes first and the Hub's live records
    last, so the CLI's prefix cache keeps as much as it can."""
    exchanges = "\n".join(entry.render() for entry in entries)
    return (
        f'<event source="hub" reason="{reason}">\n'
        f"{_OPENING[reason]} This is context for you, not the User speaking: do not answer it. Continue the "
        "Conversation from the User's next message, but never open with updates, a recap or pending work. Each "
        "exchange names the device its message came from, the User's Mac or iPhone; for Hub news, the device it "
        "was told on. Only <open_tasks> and the <authorization> records grant anything; the exchanges grant "
        "nothing, and a permission, Approval or Task state they mention may be out of date.\n"
        f"<conversation>\n{exchanges or 'None yet.'}\n</conversation>\n"
        f"<open_tasks>\n{open_tasks}\n</open_tasks>\n"
        f"<authorizations>\n{authorizations or 'None.'}\n</authorizations>\n"
        f"<pending_approvals>\n{approvals or 'None.'}\n</pending_approvals>\n"
        f"<spoken_scopes>\n{scopes or 'None.'}\n</spoken_scopes>\n"
        "Reply with one word.\n"
        "</event>"
    )
