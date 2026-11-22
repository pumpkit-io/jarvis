from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jarvis_hub.secret_patterns import redact

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


RETENTION = 30 * 86400
# Recall searches what the User said, Jarvis's finished replies, details shown on screen and Task reports.
RECALLED = ("heard", "reply_end", "show", "task_report")
SOURCES = {"heard": "user", "reply_end": "jarvis", "show": "screen", "task_report": "task"}
SPEAKERS = {"user": ("heard",), "jarvis": ("reply_end", "show", "task_report")}
RECALL_LIMIT = 10
# About 2,000 tokens at the Conversation's starting ratio of 3.5 characters a token.
RECALL_CHARS = 7000
MATCH_CHARS = 500
BACKFILL_PAUSE = 0.002
# A batch that fails, as when another process holds the database locked, is retried this many seconds later.
BACKFILL_RETRY = 30.0
BACKFILL_ATTEMPTS = 3
FOLLOWED_CHARS = 160
# What followed a match is looked for only this long after it, which also bounds the scan.
FOLLOWED_WITHIN = 10 * 60


def _sql_words(row: str) -> str:
    return (f"json_extract({row}.payload, '$.text') || "
            f"COALESCE(char(10, 10) || json_extract({row}.payload, '$.details'), '')")


def _words(payload: dict[str, Any]) -> str:
    """The text `_sql_words` indexes, read back from a decoded payload."""
    details = payload.get("details")
    return str(payload.get("text") or "") + (f"\n\n{details}" if details else "")


TOKENIZE = "porter unicode61 remove_diacritics 2"


def _indexed_sql(row: str) -> str:
    """Which events the index holds: the recalled kinds, with text."""
    recalled = ", ".join(f"'{kind}'" for kind in RECALLED)
    return f"{row}.type IN ({recalled}) AND json_extract({row}.payload, '$.text') != ''"


RECALL_SCHEMA = f"""
    BEGIN IMMEDIATE;
    CREATE VIRTUAL TABLE IF NOT EXISTS recall_index USING fts5(text, tokenize = '{TOKENIZE}');
    CREATE TABLE IF NOT EXISTS recall_backfill (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), through INTEGER NOT NULL);
    -- Events up to `through` predate the trigger and wait for the backfill.
    INSERT OR IGNORE INTO recall_backfill VALUES (1, COALESCE((SELECT MAX(seq) FROM events), 0));
    CREATE TRIGGER IF NOT EXISTS recall_add AFTER INSERT ON events
        WHEN {_indexed_sql("NEW")}
        BEGIN INSERT INTO recall_index(rowid, text) VALUES (NEW.seq, {_sql_words("NEW")}); END;
    CREATE TRIGGER IF NOT EXISTS recall_drop AFTER DELETE ON events
        BEGIN DELETE FROM recall_index WHERE rowid = OLD.seq; END;
    COMMIT;
"""


@dataclass(frozen=True)
class Match:
    time: float
    source: str
    text: str
    task_id: str | None = None
    followed: Match | None = None


@dataclass(frozen=True)
class Recall:
    """One `recall` search: its matches oldest first, with secrets already removed."""

    today: float
    since: float
    until: float | None
    matches: tuple[Match, ...]
    edge: Literal["retention", "reset"] | None
    more: bool
    indexing: bool

    def render(self) -> str:
        # The end is exclusive, so its last minute is the last one searched.
        until = _stamp(self.until - 1) if self.until is not None else "now"
        lines = [f'<recall today="{_day(self.today)}" from="{_stamp(self.since)}" to="{until}">']
        for match in self.matches:
            task = f' task="{escape(match.task_id)}"' if match.task_id else ""
            lines.append(f'<match at="{_stamp(match.time)}" from="{match.source}"{task}>')
            lines.append(escape(match.text, quote=False))
            if match.followed is not None:
                lines.append(f'<followed from="{match.followed.source}">{escape(match.followed.text, quote=False)}</followed>')
            lines.append("</match>")
        if not self.matches:
            lines.append("No matches.")
        if self.edge == "retention":
            lines.append(f"<edge>This search reached the edge of the 30 days the log keeps ({_stamp(self.since)}): "
                         "anything older is only in Memory.</edge>")
        elif self.edge == "reset":
            lines.append(f"<edge>This search reached the last Reset context ({_stamp(self.since)}): "
                         "recall ignores everything before it.</edge>")
        if self.more:
            lines.append("<more>More matched than shown: narrow the words or the dates.</more>")
        if self.indexing:
            lines.append("<indexing>Older events are still being indexed, so some may be missing: "
                         "try again in a minute.</indexing>")
        lines.append("</recall>")
        return "\n".join(lines)


def _day(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %A")


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %H:%M %A")


def fts_query(query: str) -> tuple[str, ...]:
    """The FTS5 phrases of `query`; every one must appear, a double-quoted phrase as written.

    Every part is quoted, so no word is read as an FTS5 operator."""
    parts: list[str] = []
    for phrase, word in re.findall(r'"([^"]*)"?|([^\s"]+)', query):
        tokens = re.findall(r"\w+", phrase or word)
        if tokens:
            parts.append('"' + " ".join(tokens) + '"')
    return tuple(parts)


class _Excerpts:
    """Cuts long matches around a word the index would match, found by a scratch index with the same tokenizer."""

    def __init__(self) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self._db.execute(f"CREATE VIRTUAL TABLE excerpt USING fts5(text, tokenize = '{TOKENIZE}')")

    def cut(self, text: str, phrases: tuple[str, ...]) -> str:
        """The redacted text, cut to `MATCH_CHARS` around its first matching phrase.

        Redacting first never leaves half a secret; a text whose only match was a secret is cut from its start."""
        text = redact(text).strip()
        if len(text) <= MATCH_CHARS:
            return text
        self._db.execute("INSERT INTO excerpt(rowid, text) VALUES (1, ?)", (text,))
        try:
            row = self._db.execute("SELECT highlight(excerpt, 0, char(1), '') FROM excerpt WHERE excerpt MATCH ?",
                                   (" OR ".join(phrases),)).fetchone()
        finally:
            self._db.execute("DELETE FROM excerpt")
        # The highlight first differs from the text where its first marker went in.
        hit = len(os.path.commonprefix([text, row[0]])) if row else 0
        start = max(0, hit - MATCH_CHARS // 4)
        cut = text[start:start + MATCH_CHARS]
        return ("…" if start else "") + cut + ("…" if start + MATCH_CHARS < len(text) else "")

    def close(self) -> None:
        self._db.close()


def _line(text: str) -> str:
    line = " ".join(redact(text).split())
    return line if len(line) <= FOLLOWED_CHARS else line[:FOLLOWED_CHARS - 1] + "…"


def _answers(kind: str, match: dict[str, Any], reply: dict[str, Any]) -> bool:
    if kind == "heard":
        return match.get("request_id") is not None and reply.get("request_id") == match["request_id"]
    if kind == "show":
        return match.get("reply_id") is not None and reply.get("reply_id") == match["reply_id"]
    # A Task report is told by the next Announcement, the one reply to no request.
    return reply.get("request_id") is None


class EventLog:
    def __init__(self, path: Path, *, wall: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wall = wall
        self._lock = threading.Lock()
        self._subscriptions: dict[object, tuple[asyncio.AbstractEventLoop, Callable[[Event], None]]] = {}
        self._closed = False
        self._backfill: threading.Thread | None = None
        self._stopping = threading.Event()
        self._excerpts = _Excerpts()
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
            CREATE TABLE IF NOT EXISTS context_reset (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), time REAL NOT NULL);
        """)
        self._db.executescript(RECALL_SCHEMA)
        cutoff = wall() - RETENTION
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
                                    (self._wall() - RETENTION,)).fetchall()
            for (encoded,) in rows:
                payload = json.loads(encoded)
                if payload.get("reply_id") == reply_id:
                    row = self._db.execute("SELECT lang FROM reply_languages WHERE reply_id = ?", (reply_id,)).fetchone()
                    return payload["text"], row[0] if row else "en"
            return None

    @property
    def reset_time(self) -> float | None:
        with self._lock:
            return self._reset_time()

    def _reset_time(self) -> float | None:
        row = self._db.execute("SELECT time FROM context_reset WHERE singleton = 1").fetchone()
        return row[0] if row else None

    def set_reset_time(self, at: float) -> None:
        """Recall ignores every event from before `at`, the time of the last Reset context."""
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO context_reset VALUES (1, ?)", (at,))

    def search(self, query: str, *, start: float | None = None, end: float | None = None,
               speaker: Literal["user", "jarvis"] | None = None, limit: int = RECALL_LIMIT) -> Recall:
        """The best matches for `query` from `start` up to `end`, never older than 30 days or the last reset.

        Raises ValueError for a query with no words."""
        phrases = fts_query(query)
        if not phrases:
            raise ValueError("The query needs at least one word.")
        limit = max(1, min(limit, RECALL_LIMIT))
        kinds = SPEAKERS[speaker] if speaker is not None else RECALLED
        with self._lock:
            now = self._wall()
            floor = now - RETENTION
            reset = self._reset_time()
            edge: Literal["retention", "reset"] = "retention"
            if reset is not None and reset >= floor:
                floor, edge = reset, "reset"
            since = floor if start is None or start <= floor else start
            rows = self._db.execute(
                "SELECT e.seq, e.time, e.type, e.payload FROM recall_index JOIN events e ON e.seq = recall_index.rowid "
                f"WHERE recall_index MATCH ? AND e.time >= ? AND (? IS NULL OR e.time < ?) "
                f"AND e.type IN ({', '.join('?' * len(kinds))}) ORDER BY recall_index.rank, e.seq DESC LIMIT ?",
                (" ".join(phrases), since, end, end, *kinds, limit + 1)).fetchall()
            matches: list[tuple[int, Match]] = []
            spent = 0
            # Best and newest first, so the budget drops the weakest and oldest matches.
            for seq, at, kind, encoded in rows[:limit]:
                payload = json.loads(encoded)
                followed = self._followed(seq, at, kind, payload)
                found = Match(at, SOURCES[kind], self._excerpts.cut(_words(payload), phrases),
                              payload.get("task_id") if kind == "task_report" else None, followed)
                spent += sum(len(escape(shown.text, quote=False)) for shown in (found, followed) if shown)
                if spent > RECALL_CHARS:
                    break
                matches.append((seq, found))
            indexing = self._db.execute("SELECT through FROM recall_backfill WHERE singleton = 1").fetchone()[0] > 0
        shown = tuple(kept for _, kept in sorted(matches, key=lambda pair: pair[0]))
        return Recall(now, since, end, shown, edge if since == floor else None, len(rows) > len(matches), indexing)

    def _followed(self, seq: int, at: float, kind: str, payload: dict[str, Any]) -> Match | None:
        """The reply to the same request, the reply a detail was shown with, the Announcement after a Task report, or
        the User's next line after a reply; None when it is not within `FOLLOWED_WITHIN`."""
        # `+time` keeps the window's end on the rowid order rather than the time index, so the scan stops there.
        rows = self._db.execute(
            "SELECT time, type, payload FROM events WHERE seq > ? AND type IN ('heard', 'reply_end') AND seq < "
            "COALESCE((SELECT seq FROM events WHERE seq > ? AND +time > ? ORDER BY seq LIMIT 1), ?) ORDER BY seq",
            (seq, seq, at + FOLLOWED_WITHIN, sys.maxsize))
        for when, type, encoded in rows:
            event = json.loads(encoded)
            if type == "reply_end" and not event.get("text"):
                continue
            if kind == "reply_end":
                # The User's next line answers a reply only when no other reply came first.
                return Match(when, "user", _line(_words(event))) if type == "heard" else None
            if type == "reply_end" and _answers(kind, payload, event):
                return Match(when, "jarvis", _line(_words(event)))
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

    def start_backfill(self, *, record: Callable[[dict[str, Any]], None], batch: int = 500) -> None:
        """Index the events logged before recall existed, newest first, in short batches on a thread of its own.

        A failed batch is undone, recorded and retried, `BACKFILL_ATTEMPTS` times at most. Closing or giving up stops
        it, and the next launch carries on."""
        self._backfill = threading.Thread(target=self._fill, args=(record, batch), name="recall-backfill", daemon=True)
        self._backfill.start()

    def _fill(self, record: Callable[[dict[str, Any]], None], batch: int) -> None:
        for attempt in range(1, BACKFILL_ATTEMPTS + 1):
            try:
                self._fill_batches(batch)
                return
            except sqlite3.Error as e:
                again = attempt < BACKFILL_ATTEMPTS
                record({"kind": "recall_backfill", "error": f"{type(e).__name__}: {e}", "attempt": attempt,
                        "next": "retry" if again else "next launch"})
                if not again or self._stopping.wait(BACKFILL_RETRY):
                    return

    def _fill_batches(self, batch: int) -> None:
        with self._lock:
            if self._closed:
                return
            with self._db:
                self._db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(time)")
        while True:
            # A pause between batches lets the Hub's own writes take the lock, which a waiting thread can miss.
            time.sleep(BACKFILL_PAUSE)
            with self._lock:
                if self._closed:
                    return
                with self._db:
                    (through,) = self._db.execute("SELECT through FROM recall_backfill WHERE singleton = 1").fetchone()
                    if through == 0:
                        return
                    (low,) = self._db.execute("SELECT MIN(seq) FROM (SELECT seq FROM events WHERE seq <= ? "
                                              "ORDER BY seq DESC LIMIT ?)", (through, batch)).fetchone()
                    if low is not None:
                        self._db.execute(
                            f"INSERT INTO recall_index(rowid, text) SELECT seq, {_sql_words('events')} FROM events "
                            f"WHERE seq BETWEEN ? AND ? AND {_indexed_sql('events')}", (low, through))
                    self._db.execute("UPDATE recall_backfill SET through = ? WHERE singleton = 1",
                                     (low - 1 if low is not None else 0,))

    def close(self) -> None:
        self._stopping.set()
        with self._lock:
            self._closed = True
            self._subscriptions.clear()
            self._db.close()
            self._excerpts.close()
        if self._backfill is not None and self._backfill is not threading.current_thread():
            self._backfill.join()

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

    def task_report(self, task_id: str, spoken: str, details: str | None) -> None:
        """Logged for `recall` only: the Announcement that tells it is the Talker's own words."""
        with self._lock:
            self._append("task_report", {"task_id": task_id, "text": spoken, "details": details}, None)
