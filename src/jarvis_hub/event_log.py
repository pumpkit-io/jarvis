from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis_hub.hub import Output


@dataclass(frozen=True)
class Event:
    seq: int
    time: float
    type: str
    client_id: str
    payload: dict[str, Any]


class EventLog:
    def __init__(self, path: Path, *, wall: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wall = wall
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, "
            "type TEXT NOT NULL, client_id TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self._db.execute("DELETE FROM events WHERE time < ?", (wall() - 30 * 86400,))
        self._db.commit()

    def append(self, type: str, payload: dict[str, Any], *, client_id: str = "terminal") -> Event:
        stamp = self._wall()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO events(time, type, client_id, payload) VALUES (?, ?, ?, ?)",
                (stamp, type, client_id, encoded),
            )
        assert cursor.lastrowid is not None
        return Event(cursor.lastrowid, stamp, type, client_id, json.loads(encoded))

    def after(self, seq: int) -> list[Event]:
        rows = self._db.execute(
            "SELECT seq, time, type, client_id, payload FROM events WHERE seq > ? ORDER BY seq", (seq,)
        )
        return [Event(row[0], row[1], row[2], row[3], json.loads(row[4])) for row in rows]

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventOutput:
    def __init__(self, log: EventLog, destination: Output) -> None:
        self._log = log
        self._destination = destination

    def heard(self, text: str, *, typed: bool = True) -> None:
        self._log.append("heard", {"text": text, "typed": typed})
        self._destination.heard(text, typed=typed)

    def reply(self, text: str) -> None:
        self._log.append("reply_delta", {"text": text})
        self._destination.reply(text)

    def reply_end(self) -> None:
        self._log.append("reply_end", {})
        self._destination.reply_end()

    def notice(self, text: str) -> None:
        self._log.append("notice", {"text": text})
        self._destination.notice(text)

    def show(self, markdown: str) -> None:
        self._log.append("show", {"markdown": markdown})
        self._destination.show(markdown)
