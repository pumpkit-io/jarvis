"""The live context the Hub adds to each User message for that turn only: the User's local time and zone, and the
Claude plan's usage when this Talker session has not seen it yet."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis_hub.agents.backend import PlanUsage, UsageWindow

# The longest IANA name is about 30 characters; anything far longer is not one.
_MAX_ZONE = 64
# How often the Hub reads the plan's usage, and how old a reading may be before the Talker stops getting it.
USAGE_EVERY = 5 * 60.0
USAGE_STALE = 30 * 60.0
# A session that saw the usage sees it again once a window moved this many points, or reset.
USAGE_STEP = 5.0


def zone(name: str | None) -> ZoneInfo | None:
    """The IANA time zone `name` names, or None for anything else, such as a forged or garbled header."""
    if not name or len(name) > _MAX_ZONE or name.startswith("/") or ".." in name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # OSError: a name that is a folder of zones, such as "America", or one the database cannot read.
        return None


def mac_zone() -> str | None:
    """The Mac's system time zone, read afresh each time so it follows a Mac that changed zone while the Hub runs.
    Python's own local time is fixed when the process starts."""
    if zone(name := os.environ.get("TZ", "").removeprefix(":")):
        return name
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    _, found, name = target.partition("zoneinfo/")
    return name if found and zone(name) else None


@dataclass(frozen=True)
class _Reading:
    usage: PlanUsage
    at: float


class LiveContext:
    """Where the User is in time, from each Client's zone as it last reported it, else the Mac's own; and the plan's
    usage as last read, told to each Talker session once and again only when it moved."""

    def __init__(self, mac_zone: Callable[[], str | None] = mac_zone) -> None:
        self._mac_zone = mac_zone
        self._zones: dict[str, str] = {}
        self._reading: _Reading | None = None
        # The session that was last told the usage, and what it was told.
        self._told: tuple[object, PlanUsage] | None = None

    def note_zone(self, client_id: str, name: str | None) -> None:
        """The zone a Client reported with a request; anything but an IANA name changes nothing."""
        if name and zone(name):
            self._zones[client_id] = name

    def note_usage(self, usage: PlanUsage | None, at: float) -> None:
        """A usage reading at Unix time `at`; a failed one keeps the last reading until it goes stale."""
        if usage is not None and (usage.five_hour or usage.weekly):
            self._reading = _Reading(usage, at)

    def lines(self, client_id: str, now: float, session: object) -> str:
        """What a User message from `client_id` to the Talker `session` opens with, at Unix time `now`."""
        name = self._zones.get(client_id) or self._mac_zone()
        place: tzinfo | None = zone(name)
        local = datetime.fromtimestamp(now, place) if place else datetime.fromtimestamp(now).astimezone()
        offset = local.strftime("%z")
        label = name if place else local.tzname()
        lines = [f"<now>{local.strftime('%Y-%m-%d %H:%M %A')}, {label} (UTC{offset[:3]}:{offset[3:]})</now>"]
        reading = self._reading
        if reading is not None and now - reading.at < USAGE_STALE and self._news(reading.usage, session):
            self._told = session, reading.usage
            lines.append(_usage_line(reading.usage, local))
        return "\n".join(lines)

    def _news(self, usage: PlanUsage, session: object) -> bool:
        if self._told is None or self._told[0] is not session:
            return True
        told = self._told[1]
        return _moved(told.five_hour, usage.five_hour) or _moved(told.weekly, usage.weekly)


def _moved(told: UsageWindow | None, now: UsageWindow | None) -> bool:
    if told is None or now is None:
        return told is not now
    # A reset time that changes by more than a minute is a new window.
    resets_moved = (told.resets_at is None) != (now.resets_at is None) or (
        told.resets_at is not None and now.resets_at is not None and abs(told.resets_at - now.resets_at) > 60
    )
    return abs(told.percent - now.percent) >= USAGE_STEP or resets_moved


def _usage_line(usage: PlanUsage, local: datetime) -> str:
    windows = (("five_hour", usage.five_hour), ("weekly", usage.weekly))
    attributes = "".join(f' {name}="{_window(window, local)}"' for name, window in windows if window is not None)
    return f"<claude_usage{attributes}/>"


def _window(window: UsageWindow, local: datetime) -> str:
    used = f"{round(window.percent)}% used"
    if window.resets_at is None:
        return used
    resets = datetime.fromtimestamp(window.resets_at, local.tzinfo)
    when = resets.strftime("%H:%M" if resets.date() == local.date() else "%A %H:%M")
    return f"{used}, resets {when}"
