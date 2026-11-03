"""The Max usage limit: which sessions it stopped, until when, and what Jarvis says about it."""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from jarvis_hub.voice.language import Lang

# How long a limit whose reset time is unknown holds before the Hub tries again.
UNKNOWN_RESET_WAIT = 15 * 60.0
# A reset further off than this is a weekly window, so the spoken time names its day.
SAME_DAY = 18 * 3600.0

_WEEKDAYS: dict[Lang, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
}


class LimitClock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


def reset_sentence(resets_at: float | None, lang: Lang, now: float) -> str:
    """"It resets at 3:40 PM, sir." in local time, or "" when the reset time is unknown. `now` is Unix seconds."""
    if resets_at is None:
        return ""
    when = datetime.fromtimestamp(resets_at)
    day = _WEEKDAYS[lang][when.weekday()] if resets_at - now > SAME_DAY else None
    if lang == "it":
        clock = f"{when.hour}" if when.minute == 0 else f"{when.hour}:{when.minute:02d}"
        return f"Si riazzera {f'{day} ' if day else ''}alle {clock}."
    hour = when.hour % 12 or 12
    clock = f"{hour} {'AM' if when.hour < 12 else 'PM'}" if when.minute == 0 else (
        f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"
    )
    return f"It resets {f'on {day} ' if day else ''}at {clock}."


class UsageLimit:
    """Roles the limit has stopped, each until its own reset. `on_hit` hears of every hit with whether it opened a
    new limit; `on_clear` of every set of roles that comes back, with whether no role is stopped any more."""

    def __init__(
        self,
        clock: LimitClock,
        wall: Callable[[], float],
        on_hit: Callable[[str, bool], None],
        on_clear: Callable[[frozenset[str], bool], None],
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._on_hit = on_hit
        self._on_clear = on_clear
        # Role to the Unix time it comes back.
        self._until: dict[str, float] = {}
        # Roles whose reset time is a guess.
        self._guessed: set[str] = set()
        self._cleared = asyncio.Event()
        self._cleared.set()
        self._timer: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return bool(self._until)

    def blocks(self, role: str) -> bool:
        return role in self._until

    def resets_at(self, role: str) -> float | None:
        """When the role comes back, or None when it is not stopped or the reset time was never known."""
        return None if role in self._guessed else self._until.get(role)

    def hit(self, role: str, resets_at: float | None) -> None:
        """The limit stopped `role`. A reset time not given is guessed, and said to be unknown."""
        opened = not self._until
        self._guessed.discard(role)
        if resets_at is None:
            self._guessed.add(role)
        self._until[role] = resets_at if resets_at is not None else self._wall() + UNKNOWN_RESET_WAIT
        self._cleared.clear()
        # Restarted so a role that comes back sooner than the one it is sleeping for is not kept waiting.
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._run())
        self._on_hit(role, opened)

    async def wait_clear(self, stop: asyncio.Event) -> None:
        """Returns once no role is stopped, or `stop` is set."""
        if not self._until:
            return
        cleared = asyncio.create_task(self._cleared.wait())
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([cleared, stopping], return_when=asyncio.FIRST_COMPLETED)
        finally:
            cleared.cancel()
            stopping.cancel()

    async def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer
            self._timer = None

    async def _run(self) -> None:
        try:
            while self._until:
                soonest = min(self._until.values())
                if (wait := soonest - self._wall()) > 0:
                    await self._clock.sleep(wait)
                    continue
                back = frozenset(role for role, until in self._until.items() if until <= self._wall())
                for role in back:
                    del self._until[role]
                    self._guessed.discard(role)
                everyone = not self._until
                if everyone:
                    self._cleared.set()
                self._on_clear(back, everyone)
        finally:
            if self._timer is asyncio.current_task():
                self._timer = None
