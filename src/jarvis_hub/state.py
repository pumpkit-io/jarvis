"""The Hub's state file in the runtime folder: what a restart must not lose, as one small versioned JSON document."""

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

VERSION = 1

TimeShift = Callable[[float], float]
T = TypeVar("T")


@dataclass(frozen=True)
class Fields:
    """The attributes of one kind of record the state file keeps. `times` are on the Hub's monotonic clock and saved
    as wall-clock times; `paths` are saved as strings."""

    names: tuple[str, ...]
    times: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()

    def dump(self, record: object, to_wall: TimeShift) -> dict[str, Any]:
        saved: dict[str, Any] = {}
        for name in self.names:
            value = getattr(record, name)
            if value is not None and name in self.times:
                value = to_wall(value)
            elif value is not None and name in self.paths:
                value = str(value)
            saved[name] = value
        return saved

    def load(self, saved: dict[str, Any], from_wall: TimeShift) -> dict[str, Any]:
        """The record's constructor arguments; a missing field raises KeyError."""
        values: dict[str, Any] = {}
        for name in self.names:
            value = saved[name]
            if value is not None and name in self.times:
                value = from_wall(float(value))
            elif value is not None and name in self.paths:
                value = Path(value)
            values[name] = value
        return values


def one_of(value: T, allowed: Collection[T]) -> T:
    """`value`, or ValueError when a saved field holds something this Hub does not know."""
    if value not in allowed:
        raise ValueError(f"{value!r} is not one of {', '.join(map(repr, allowed))}")
    return value


def numbered(record_id: object, prefix: str) -> int:
    """The number of an id such as T3 or C2; ValueError for any other shape."""
    if not isinstance(record_id, str) or not record_id.startswith(prefix) or not record_id[1:].isdigit():
        raise ValueError(f"{record_id!r} is not a {prefix} id")
    return int(record_id[1:])


class UnreadableState(Exception):
    """The state file could not be used and was moved aside; the message says where, for the User."""


class StateFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, Any] | None:
        """The saved state, or None when there is none. A corrupt file or one of another version is moved aside
        and raises UnreadableState, so the Hub starts fresh instead of failing."""
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise UnreadableState(f"Could not read {self._path}: {e}; starting with nothing restored.") from e
        try:
            data: object = json.loads(text)
        except ValueError:
            raise UnreadableState(self.set_aside("is not valid JSON")) from None
        version = cast(dict[str, Any], data).get("version") if isinstance(data, dict) else None
        if version != VERSION:
            raise UnreadableState(self.set_aside(f"has version {version!r}, not {VERSION}"))
        return cast(dict[str, Any], data)

    def save(self, data: dict[str, Any]) -> None:
        """Written to a temporary file and renamed over the old one, so a crash mid-write leaves the old state whole."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"version": VERSION, **data}, f, ensure_ascii=False, indent=1)
                # Without it a power cut after the rename can leave an empty file in place of both states.
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def set_aside(self, problem: str) -> str:
        """Moves the file out of the way, so the next save starts clean; returns the notice saying where it went."""
        aside = self._path.with_name(f"{self._path.stem}.unreadable{self._path.suffix}")
        try:
            os.replace(self._path, aside)
        except OSError as e:
            return f"The state file {self._path} {problem} and could not be moved aside ({e}); nothing was restored."
        return f"The state file {self._path} {problem}, so nothing was restored; it was moved to {aside}."
