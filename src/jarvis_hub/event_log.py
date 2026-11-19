from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid
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
    client_id: str | None
    payload: dict[str, Any]
    replayable: bool = True
    binary: bytes | None = None
    audience: object | None = None


def _event(row: tuple[Any, ...]) -> Event:
    return Event(row[0], row[1], row[2], row[3] or None, json.loads(row[4]))


class EventLog:
    def __init__(self, path: Path, *, wall: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wall = wall
        self._lock = threading.Lock()
        self._subscriptions: dict[object, tuple[asyncio.AbstractEventLoop, Callable[[Event], None]]] = {}
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                type TEXT NOT NULL, client_id TEXT, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (
                client_id TEXT NOT NULL, id TEXT NOT NULL, type TEXT NOT NULL,
                digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                PRIMARY KEY(client_id, id));
            CREATE TABLE IF NOT EXISTS talks (
                client_id TEXT NOT NULL, id TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(client_id, id));
            CREATE TABLE IF NOT EXISTS reply_languages (reply_id TEXT PRIMARY KEY, lang TEXT NOT NULL, time REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS client_settings (client_id TEXT PRIMARY KEY, silence INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS retention (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1), pruned_through INTEGER NOT NULL);
            INSERT OR IGNORE INTO retention VALUES (1, 0);
        """)
        cutoff = wall() - 30 * 86400
        self._db.execute("UPDATE retention SET pruned_through = MAX(pruned_through, "
                         "COALESCE((SELECT MAX(seq) FROM events WHERE time < ?), 0)) WHERE singleton = 1", (cutoff,))
        self._db.execute("DELETE FROM events WHERE time < ?", (cutoff,))
        self._db.execute("DELETE FROM reply_languages WHERE time < ?", (cutoff,))
        unknown = json.dumps({"ok": False, "error": {"code": "outcome_unknown",
            "message": "Previous operation outcome is unknown; reconcile with history", "retry_after_seconds": None}})
        self._db.execute("UPDATE commands SET status = 'complete', result = ? WHERE status != 'complete'", (unknown,))
        self._db.execute("UPDATE talks SET status = 'expired' WHERE status = 'active'")
        self._db.commit()

    def append(self, type: str, payload: dict[str, Any], *, client_id: str | None = "terminal", replayable: bool = True, binary: bytes | None = None, audience: object | None = None) -> Event:
        if binary is not None and replayable:
            raise ValueError("Binary audio must be live-only")
        with self._lock:
            stamp = self._wall()
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            with self._db:
                cursor = self._db.execute(
                    "INSERT INTO events(time, type, client_id, payload) VALUES (?, ?, ?, ?)",
                    (stamp, type, client_id if client_id is not None else "", encoded if replayable else "{}"),
                )
                if not replayable:
                    self._db.execute("DELETE FROM events WHERE seq = ?", (cursor.lastrowid,))
            assert cursor.lastrowid is not None
            event = Event(cursor.lastrowid, stamp, type, client_id, json.loads(encoded), replayable, binary, audience)
            # Schedule under the allocation lock so worker threads cannot reverse delivery order.
            for loop, callback in self._subscriptions.values():
                loop.call_soon_threadsafe(callback, event)
            return event

    def _high_water(self) -> int:
        row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
        return row[0] if row else 0

    @property
    def high_water(self) -> int:
        with self._lock:
            return self._high_water()

    def after(self, seq: int) -> list[Event]:
        with self._lock:
            return [_event(row) for row in self._db.execute(
                "SELECT seq, time, type, client_id, payload FROM events WHERE seq > ? ORDER BY seq", (seq,))]

    def subscribe(self, after: int, loop: asyncio.AbstractEventLoop, callback: Callable[[Event], None],
                  client_id: str) -> tuple[object, list[Event], int, int | None, bool]:
        with self._lock:
            mark = self._high_water()
            if after > mark:
                raise ValueError("Cursor exceeds the latest sequence")
            oldest = self._db.execute("SELECT MIN(seq) FROM events").fetchone()[0]
            replay = [_event(row) for row in self._db.execute(
                "SELECT seq, time, type, client_id, payload FROM events WHERE seq > ? AND seq <= ? "
                "AND (type != 'status' OR client_id = ?) ORDER BY seq", (after, mark, client_id))]
            token = object()
            self._subscriptions[token] = (loop, callback)
            pruned_through = self._db.execute("SELECT pruned_through FROM retention WHERE singleton = 1").fetchone()[0]
            pruned = after < pruned_through
            return token, replay, mark, oldest, pruned

    def unsubscribe(self, token: object) -> None:
        with self._lock:
            self._subscriptions.pop(token, None)

    def history(self, client_id: str, before: int | None, limit: int) -> tuple[list[Event], bool]:
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, time, type, client_id, payload FROM events "
                "WHERE (? IS NULL OR seq < ?) AND (type != 'status' OR client_id = ?) "
                "ORDER BY seq DESC LIMIT ?", (before, before, client_id, limit + 1)).fetchall()
            return [_event(row) for row in reversed(rows[:limit])], len(rows) > limit

    def silence(self, client_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT silence FROM client_settings WHERE client_id = ?", (client_id,)).fetchone()
            return bool(row and row[0])

    def set_silence(self, client_id: str, silence: bool) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO client_settings VALUES (?, ?)", (client_id, silence))

    def set_reply_language(self, reply_id: str, lang: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO reply_languages VALUES (?, ?, ?)", (reply_id, lang, self._wall()))

    def retained_reply(self, reply_id: str) -> tuple[str, str] | None:
        with self._lock:
            rows = self._db.execute("SELECT payload FROM events WHERE type = 'reply_end' AND time >= ? ORDER BY seq DESC",
                                    (self._wall() - 30 * 86400,)).fetchall()
            for (encoded,) in rows:
                payload = json.loads(encoded)
                if payload.get("reply_id") == reply_id:
                    row = self._db.execute("SELECT lang FROM reply_languages WHERE reply_id = ?", (reply_id,)).fetchone()
                    return payload["text"], row[0] if row else "en"
            return None

    def reserve(self, client_id: str, command_id: str, kind: str,
                payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        encoded = json.dumps([kind, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock, self._db:
            inserted = self._db.execute("INSERT OR IGNORE INTO commands VALUES (?, ?, ?, ?, 'reserved', NULL)",
                             (client_id, command_id, kind if kind in {"say_text", "acknowledge", "set_presence", "talk_begin", "talk_cancel", "upload", "set_silence", "replay_speech"} else "unknown", digest))
            row = self._db.execute("SELECT digest, status, result FROM commands WHERE client_id = ? AND id = ?",
                                   (client_id, command_id)).fetchone()
            if row[0] != digest:
                return "conflict", None
            return "new" if inserted.rowcount else row[1], json.loads(row[2]) if row[2] is not None else None

    def begin_talk(self, client_id: str, talk_id: str) -> bool:
        with self._lock, self._db:
            inserted = self._db.execute("INSERT OR IGNORE INTO talks VALUES (?, ?, '', 'active')", (client_id, talk_id))
            return bool(inserted.rowcount)

    def finish_talk(self, client_id: str, talk_id: str, request_id: str, status: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE talks SET request_id = ?, status = ? WHERE client_id = ? AND id = ?",
                             (request_id, status, client_id, talk_id))

    def complete(self, client_id: str, command_id: str, result: dict[str, Any]) -> None:
        safe_fields = {"request_id", "talk_id", "speech_id", "reply_id", "seq", "focus_client_id", "silence", "cancelled", "stopped", "can_show_text"}
        if "result" in result and set(result["result"]) - safe_fields:
            raise ValueError("Lifetime command results may contain only identifiers and status")
        with self._lock, self._db:
            self._db.execute("UPDATE commands SET status = 'complete', result = ? WHERE client_id = ? AND id = ?",
                             (json.dumps(result, allow_nan=False), client_id, command_id))

    def close(self) -> None:
        with self._lock:
            self._subscriptions.clear()
            self._db.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventOutput:
    def __init__(self, log: EventLog | None, destination: Output) -> None:
        self.log = log
        self.destination = destination
        self._lock = threading.RLock()
        self._reply_id: str | None = None
        self._request_id: str | None = None
        self._client_id: str | None = None
        self._text = ""

    @property
    def reply_id(self) -> str | None:
        return self._reply_id

    def begin_reply(self, client_id: str | None, request_id: str | None) -> None:
        with self._lock:
            self._reply_id = uuid.uuid4().hex
            self._request_id = request_id
            self._client_id = client_id
            self._text = ""

    def _append(self, kind: str, payload: dict[str, Any], client_id: str | None) -> None:
        if self.log is not None:
            self.log.append(kind, payload, client_id=client_id)

    def snapshot(self, kind: str, payload: dict[str, Any], *, client_id: str | None = None) -> None:
        with self._lock:
            self._append(kind, payload, client_id)

    def heard(self, text: str, *, typed: bool = True, request_id: str | None = None, client_id: str = "terminal", display: bool = True) -> None:
        with self._lock:
            self._append("heard", {"request_id": request_id or uuid.uuid4().hex, "text": text, "spoken": not typed}, client_id)
            if display:
                self.destination.heard(text, typed=typed, client_id=client_id)

    def reply(self, text: str) -> None:
        with self._lock:
            if self._reply_id is None:
                self.begin_reply("terminal", None)
            self._text += text
            self._append("reply_delta", {"reply_id": self._reply_id, "request_id": self._request_id, "text": text}, self._client_id)
            self.destination.reply(text)

    def reply_end(self, *, text: str | None = None, reason: str = "complete") -> None:
        with self._lock:
            if self._reply_id is None:
                self.begin_reply("terminal", None)
            self._append("reply_end", {"reply_id": self._reply_id, "request_id": self._request_id,
                                      "text": self._text if text is None else text, "reason": reason}, self._client_id)
            self._reply_id = None
            self._request_id = self._client_id = None
            self._text = ""
            self.destination.reply_end()

    def notice(self, text: str) -> None:
        with self._lock:
            self._append("notice", {"text": text}, None)
            self.destination.notice(text)

    def show(self, markdown: str) -> None:
        with self._lock:
            self._append("show", {"reply_id": self._reply_id, "title": "Details", "text": markdown}, self._client_id)
            self.destination.show(markdown)
