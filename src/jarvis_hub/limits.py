"""The Max usage limit: which sessions it stopped, until when, and what the User is shown about it."""

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
# A role's hits stop counting this long after its hold cleared: a retry hits again at once, so a later hit is news.
# Known limit: the Hub hears of a turn only when it ends, so a first turn after the hold that runs longer than this
# is expired before `observe` sees it complete, and the reset is then never announced. Telling an in-flight turn
# apart would need a turn-start hook in every role's runner, for a turn that long being rare.
STRIKE_TTL = 3600.0
# A reset further off than this is a weekly window, so the spoken time names its day.
SAME_DAY = 18 * 3600.0

_WEEKDAYS: dict[Lang, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
}


def _italian_clock(when: datetime) -> str:
    """"alle 15:40", with Italian's own forms for one o'clock, midnight and noon."""
    if when.minute == 0 and when.hour in (0, 12):
        return "a mezzanotte" if when.hour == 0 else "a mezzogiorno"
    clock = f"{when.hour}" if when.minute == 0 else f"{when.hour}:{when.minute:02d}"
    if when.hour == 1:
        return "all'una" if when.minute == 0 else f"all'{clock}"
    return f"alle {clock}"


class LimitClock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


def _when(resets_at: float, lang: Lang, now: float) -> str:
    """"at 3:40 PM" in local time, or "on Friday at 3:40 PM" when the reset is more than SAME_DAY off."""
    when = datetime.fromtimestamp(resets_at)
    day = _WEEKDAYS[lang][when.weekday()] if resets_at - now > SAME_DAY else None
    if lang == "it":
        return f"{f'{day} ' if day else ''}{_italian_clock(when)}"
    hour = when.hour % 12 or 12
    clock = f"{hour} {'AM' if when.hour < 12 else 'PM'}" if when.minute == 0 else (
        f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"
    )
    return f"{f'on {day} ' if day else ''}at {clock}"


def reset_sentence(resets_at: float | None, lang: Lang, now: float) -> str:
    """"It resets at 3:40 PM.", or "" when the reset time is unknown. `now` is Unix seconds."""
    if resets_at is None:
        return ""
    when = _when(resets_at, lang, now)
    return f"Si riazzera {when}." if lang == "it" else f"It resets {when}."


def update_sentence(resets_at: float | None, lang: Lang, now: float) -> str:
    """The reset the User was told has moved: to `resets_at`, or to an unknown time."""
    if resets_at is None:
        return "Non è più chiaro quando si riazzera." if lang == "it" else "It's no longer clear when it resets."
    if lang == "it":
        return f"Ora dovrebbe riazzerarsi {_when(resets_at, lang, now)}."
    return f"It's now expected to reset {_when(resets_at, lang, now)}."


@dataclass(frozen=True)
class Notice:
    """What to tell the User of the limit: the reset, None when unknown, and whether it moves one they heard."""

    resets_at: float | None
    update: bool


@dataclass(frozen=True)
class SavedLimit:
    """What the state file keeps of the limit, as `UsageLimit.dump` writes it. Times are Unix seconds."""

    until: dict[str, float] = field(default_factory=lambda: {})
    guessed: frozenset[str] = frozenset()
    reported: dict[str, float] = field(default_factory=lambda: {})
    strikes: dict[str, int] = field(default_factory=lambda: {})
    cleared: dict[str, float] = field(default_factory=lambda: {})
    told: bool = False
    told_reset: float | None = None


class UsageLimit:
    """Roles the limit has stopped, each until its own reset, and what the User was shown about it. The User sees the
    limit, not of a role: once that it was reached, again when a hit moves the reset later than they were shown or makes
    it unknown, and that it reset only once a turn of a role the limit held completes after its hold cleared, since
    only then has work really resumed. A retry that is limited again is silent. A hit after an announced reset is
    news again, since the User believes the limit is over. `on_hit` hears each hit and whether it brings news,
    `on_clear` the roles whose hold ran out, `on_reset` that the reset is to be announced."""

    def __init__(
        self,
        clock: LimitClock,
        wall: Callable[[], float],
        on_hit: Callable[[str, bool], None],
        on_clear: Callable[[frozenset[str]], None],
        on_reset: Callable[[], None],
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._on_hit = on_hit
        self._on_clear = on_clear
        self._on_reset = on_reset
        # Role to the Unix time it comes back.
        self._until: dict[str, float] = {}
        # Roles whose reset time is a guess.
        self._guessed: set[str] = set()
        # Role to the reset its last hit reported, before the wait that backs off: what the User's notice compares.
        self._reported: dict[str, float] = {}
        # Role to its hits in the episode, which is open while any role has one.
        self._strikes: dict[str, int] = {}
        # Struck role, no longer stopped, to the Unix time its hold cleared.
        self._cleared_at: dict[str, float] = {}
        # Whether the User heard the limit is on, and the reset they heard, since the last reset they heard. Stays
        # set while a hold has cleared but no held role has completed a turn yet.
        self._told = False
        self._told_reset: float | None = None
        self._cleared = asyncio.Event()
        self._cleared.set()
        self._timer: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return bool(self._until)

    @property
    def recovery_pending(self) -> bool:
        """A shown limit still awaits evidence from a successful held-role turn."""
        return self._told

    def blocks(self, role: str) -> bool:
        return role in self._until

    def resets_at(self, role: str) -> float | None:
        """When the role comes back, or None when it is not stopped or the reset time was never known."""
        return None if role in self._guessed else self._until.get(role)

    def _latest(self) -> float | None:
        """When the last held role comes back, None when any held role's reset time is unknown."""
        resets = [self.resets_at(role) for role in self._until]
        return None if None in resets else max(r for r in resets if r is not None)

    def _latest_reported(self) -> float | None:
        """Like `_latest`, but from the resets the hits reported, so a retry that backs off is not a later reset."""
        resets = [None if r in self._guessed else self._reported.get(r, self._until[r]) for r in self._until]
        return None if None in resets else max(r for r in resets if r is not None)

    def _news(self) -> bool:
        if not self._until:
            return False
        if not self._told:
            return True
        if self._told_reset is None:
            return False
        latest = self._latest_reported()
        # A reset already past when reported only says "soon": it moves nothing the User heard.
        return latest is None or latest > max(self._told_reset, self._wall())

    def tell(self) -> float | None:
        """The reset to tell the User, who now has heard it: the latest among the held roles. Only while active."""
        self._told, self._told_reset = True, self._latest()
        return self._told_reset

    def tell_news(self) -> Notice | None:
        """What the User has yet to hear of the limit, now told; None when nothing holds or they heard it all."""
        if not self._news():
            return None
        update = self._told
        return Notice(self.tell(), update)

    def observe(self, role: str, end: TurnEnd) -> None:
        """A turn of `role` ended: a limited one stops the role, a completed one ends its part in the episode. When
        that role was held and nothing is held now, the limit has really reset, if the User was told it was on. An
        episode that expired before this turn ended silently."""
        self._expire()
        if end.outcome == "limited":
            self._hit(role, end.resets_at)
        elif end.outcome == "completed" and role not in self._until:
            resumed = role in self._strikes
            self._leave_episode([role])
            if resumed and self._told and not self._until:
                self._told, self._told_reset = False, None
                self._on_reset()

    def _expire(self) -> None:
        """Ends the part in the episode of every role whose hold cleared STRIKE_TTL ago. When none is left the
        episode is over, and what the User heard of it with it."""
        now = self._wall()
        stale = [r for r in self._strikes if r not in self._until and now - self._cleared_at.get(r, 0.0) >= STRIKE_TTL]
        self._leave_episode(stale)
        if not self._until and not self._strikes:
            # Nothing came back to complete: the hold the User heard of is long over, so the next limit is new.
            self._told, self._told_reset = False, None

    def _leave_episode(self, roles: list[str]) -> None:
        for role in roles:
            self._strikes.pop(role, None)
            self._cleared_at.pop(role, None)

    def _hit(self, role: str, resets_at: float | None) -> None:
        """Stops `role` until its reset, but never less than a wait that doubles with each hit in the episode: a
        reset time already past would otherwise retry at once, forever. A reset time not given is guessed."""
        now = self._wall()
        strikes = self._strikes[role] = self._strikes.get(role, 0) + 1
        self._cleared_at.pop(role, None)
        floor = now + min(MIN_WAIT * 2 ** (strikes - 1), MAX_WAIT)
        self._guessed.discard(role)
        self._reported.pop(role, None)
        if resets_at is None:
            self._guessed.add(role)
            resets_at = now + UNKNOWN_RESET_WAIT
        else:
            self._reported[role] = resets_at
        self._until[role] = max(resets_at, floor)
        self._cleared.clear()
        # Restarted so a role that comes back sooner than the one it is sleeping for is not kept waiting.
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._run())
        self._on_hit(role, self._news())

    def dump(self) -> dict[str, Any]:
        return {
            "until": dict(self._until),
            "guessed": sorted(self._guessed),
            "reported": dict(self._reported),
            "strikes": dict(self._strikes),
            "cleared": dict(self._cleared_at),
            "told": self._told,
            "told_reset": self._told_reset,
        }

    @staticmethod
    def load(saved: dict[str, Any]) -> SavedLimit:
        """What `dump` saved, checked; KeyError, ValueError or TypeError when it is not."""
        until = {str(role): float(t) for role, t in dict(saved["until"]).items()}
        roles: list[Any] = list(saved["guessed"])
        guessed = frozenset(str(role) for role in roles)
        if not guessed <= until.keys():
            raise ValueError(f"guessed roles {sorted(guessed - until.keys())} have no reset time")
        reported = {str(role): float(t) for role, t in dict(saved.get("reported", {})).items() if role in until}
        strikes = {str(role): int(n) for role, n in dict(saved["strikes"]).items()}
        cleared = {str(role): float(t) for role, t in dict(saved.get("cleared", {})).items()}
        told, told_reset = saved.get("told", False), saved.get("told_reset")
        if not isinstance(told, bool):
            raise TypeError(f"told is {told!r}, not a bool")
        told_reset = None if told_reset is None else float(told_reset)
        return SavedLimit(until, guessed, reported, strikes, cleared, told, told_reset)

    def restore(self, saved: SavedLimit) -> None:
        """Stops the roles again until their saved reset, without `on_hit`: what the User has yet to hear is in
        `tell_news`. A reset already past clears on the next loop turn, through `on_clear`."""
        self._strikes, self._cleared_at = dict(saved.strikes), dict(saved.cleared)
        self._told, self._told_reset = saved.told, saved.told_reset
        if not saved.until:
            return
        self._until, self._guessed = dict(saved.until), set(saved.guessed)
        self._reported = dict(saved.reported)
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
                now = self._wall()
                back = frozenset(role for role, until in self._until.items() if until <= now)
                for role in back:
                    del self._until[role]
                    self._guessed.discard(role)
                    self._reported.pop(role, None)
                    if role in self._strikes:
                        self._cleared_at[role] = now
                if not self._until:
                    self._cleared.set()
                self._on_clear(back)
        finally:
            if self._timer is asyncio.current_task():
                self._timer = None
