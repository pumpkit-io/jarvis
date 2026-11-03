"""The Hub's state file in the runtime folder: what a restart must not lose, as one small versioned JSON document."""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

VERSION = 1


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
