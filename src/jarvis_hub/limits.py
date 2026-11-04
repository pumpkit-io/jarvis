"""The Max usage limit: which sessions it stopped, until when, and what Jarvis says about it."""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from jarvis_hub.agents.backend import TurnEnd
from jarvis_hub.voice.language import Lang

# How long a limit whose reset time is unknown holds before the Hub tries again.
UNKNOWN_RESET_WAIT = 15 * 60.0
# The least a limit holds, doubled for each further hit of the same role in an episode, up to MAX_WAIT.
MIN_WAIT = 60.0
MAX_WAIT = 15 * 60.0
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


@dataclass(frozen=True)
class SavedLimit:
    """What the state file keeps of the limit: each stopped role's reset as Unix seconds, the roles whose reset is a
    guess, each role's hits since its last successful turn, and whether this episode's reset was announced."""

    until: dict[str, float] = field(default_factory=lambda: {})
    guessed: frozenset[str] = frozenset()
    strikes: dict[str, int] = field(default_factory=lambda: {})
    reset_told: bool = False


class UsageLimit:
    """Roles the limit has stopped, each until its own reset.

    An episode runs from the first hit until every role it stopped has had a successful turn, so a reset that turns
    out early (the next call is limited again) is not news. `on_hit` hears of every hit, with whether it is the
    episode's first, which is the one the User is told of; `on_clear` of every set of roles that comes back, with
    whether to announce the reset: once per episode, when no role is stopped any more.
    """

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
        # Role to its hits since its last successful turn: the episode is open while any role has one.
        self._strikes: dict[str, int] = {}
        self._reset_told = False
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

    def observe(self, role: str, end: TurnEnd) -> None:
        """A turn of `role` ended: a limited one stops the role, a completed one ends its part in the episode."""
        if end.outcome == "limited":
            self._hit(role, end.resets_at)
        elif end.outcome == "completed" and self._strikes.pop(role, None) is not None and not self._strikes:
            self._reset_told = False

    def _hit(self, role: str, resets_at: float | None) -> None:
        """Stops `role` until its reset, but never less than a wait that doubles with each hit in the episode: a
        reset time already past would otherwise retry at once, forever. A reset time not given is guessed."""
        first = not self._strikes
        strikes = self._strikes[role] = self._strikes.get(role, 0) + 1
        now = self._wall()
        floor = now + min(MIN_WAIT * 2 ** (strikes - 1), MAX_WAIT)
        self._guessed.discard(role)
        if resets_at is None:
            self._guessed.add(role)
            resets_at = now + UNKNOWN_RESET_WAIT
        self._until[role] = max(resets_at, floor)
        self._cleared.clear()
        # Restarted so a role that comes back sooner than the one it is sleeping for is not kept waiting.
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._run())
        self._on_hit(role, first)

    def dump(self) -> dict[str, Any]:
        return {
            "until": dict(self._until),
            "guessed": sorted(self._guessed),
            "strikes": dict(self._strikes),
            "reset_told": self._reset_told,
        }

    @staticmethod
    def load(saved: dict[str, Any]) -> SavedLimit:
        """What `dump` saved, checked; KeyError, ValueError or TypeError when it is not."""
        until = {str(role): float(t) for role, t in dict(saved["until"]).items()}
        roles: list[Any] = list(saved["guessed"])
        guessed = frozenset(str(role) for role in roles)
        if not guessed <= until.keys():
            raise ValueError(f"guessed roles {sorted(guessed - until.keys())} have no reset time")
        strikes = {str(role): int(n) for role, n in dict(saved["strikes"]).items()}
        if not isinstance(saved["reset_told"], bool):
            raise TypeError(f"reset_told is {saved['reset_told']!r}, not a bool")
        return SavedLimit(until, guessed, strikes, saved["reset_told"])

    def restore(self, saved: SavedLimit) -> None:
        """Stops the roles again until their saved reset, without `on_hit`: the User heard of it before. A reset
        already past clears on the next loop turn, through `on_clear`."""
        self._strikes, self._reset_told = dict(saved.strikes), saved.reset_told
        if not saved.until:
            return
        self._until, self._guessed = dict(saved.until), set(saved.guessed)
        self._cleared.clear()
        self._timer = asyncio.create_task(self._run())

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
                announce = False
                if not self._until:
                    self._cleared.set()
                    announce, self._reset_told = not self._reset_told, True
                self._on_clear(back, announce)
        finally:
            if self._timer is asyncio.current_task():
                self._timer = None
