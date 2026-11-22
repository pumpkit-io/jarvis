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


def write_atomically(path: Path, data: dict[str, Any], *, indent: int | None = 1) -> None:
    """Written to a temporary file and renamed over the old one, so a crash mid-write leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    try:
        # A lone surrogate from a Client's JSON becomes a \uXXXX escape, which reads back as the same string.
        with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent, allow_nan=False)
            # Without it a power cut after the rename can leave an empty file in place of both versions.
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class UnreadableFile(Exception):
    """A saved file could not be used; the message tells the User what became of it."""


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON number")


class SavedFile:
    """A versioned JSON file in the runtime folder. One that cannot be used is moved aside, never saved over."""

    def __init__(self, path: Path, version: int, *, name: str, fallback: str, indent: int | None = 1) -> None:
        self._path = path
        self._version = version
        self._indent = indent
        # How the notices name the file and say what the Hub does without it.
        self._name = name
        self._fallback = fallback
        self._blocked = False

    def load(self) -> dict[str, Any] | None:
        """The saved data, or None when there is none; raises UnreadableFile after moving an unusable file aside."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise UnreadableFile(self.set_aside(f"could not be read ({e})")) from e
        try:
            data: object = json.loads(raw.decode(), parse_constant=_no_constant)
        except UnicodeDecodeError:
            raise UnreadableFile(self.set_aside("is not UTF-8 text")) from None
        except (ValueError, RecursionError):
            raise UnreadableFile(self.set_aside("is not valid JSON")) from None
        version = cast(dict[str, Any], data).get("version") if isinstance(data, dict) else None
        if version != self._version:
            raise UnreadableFile(self.set_aside(f"has version {version!r}, not {self._version}"))
        return cast(dict[str, Any], data)

    def save(self, data: dict[str, Any]) -> None:
        if not self._blocked:
            write_atomically(self._path, {"version": self._version, **data}, indent=self._indent)

    def set_aside(self, problem: str) -> str:
        """Moves the file out of the way, so the next save starts clean; returns the notice saying where it went."""
        aside = self._path.with_name(f"{self._path.stem}.unreadable{self._path.suffix}")
        n = 1
        while os.path.lexists(aside):
            n += 1
            aside = self._path.with_name(f"{self._path.stem}.unreadable-{n}{self._path.suffix}")
        try:
            os.replace(self._path, aside)
        except OSError as e:
            self._blocked = True
            return (f"{self._name} {self._path} {problem} and could not be moved aside ({e}); "
                    f"{self._fallback}, and nothing will be saved over it.")
        return f"{self._name} {self._path} {problem}, so {self._fallback}; it was moved to {aside}."


class StateFile(SavedFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path, VERSION, name="The state file", fallback="nothing was restored")
