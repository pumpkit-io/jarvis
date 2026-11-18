from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PairedClient:
    id: str
    kind: str
    name: str
    created_at: float


@dataclass(frozen=True)
class PairingResult:
    client: PairedClient
    token: str = field(repr=False)
    origin: str


class PairingError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def https_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Pairing needs an HTTPS origin, for example https://mac.example.ts.net") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
            or any(c in value for c in ("?", "#", "\\")) or parsed.netloc.endswith(":")
            or (port is not None and port < 1)):
        raise ValueError("Pairing needs an HTTPS origin without credentials, path, query or fragment")
    return value.rstrip("/")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


class PairingStore:
    def __init__(self, path: Path, *, wall: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wall = wall
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=2, isolation_level=None, check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                token_hash BLOB NOT NULL, created_at REAL NOT NULL,
                revoked_at REAL, last_ack_seq INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS active_client_name ON clients(name) WHERE revoked_at IS NULL;
            CREATE TABLE IF NOT EXISTS pairing (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1), code_hash BLOB NOT NULL,
                expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, origin TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pairing_failures (attempted_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pairing_guard (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1), locked_until REAL NOT NULL);
            INSERT OR IGNORE INTO pairing_guard VALUES (1, 0);
        """)

    def create_code(self, origin: str) -> str:
        origin = https_origin(origin)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                previous = self._db.execute("SELECT code_hash FROM pairing WHERE singleton = 1").fetchone()
                while True:
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    digest = _digest(code)
                    if previous is None or not secrets.compare_digest(previous[0], digest):
                        break
                self._db.execute("INSERT OR REPLACE INTO pairing VALUES (1, ?, ?, 0, ?)",
                                 (digest, self._wall() + 300, origin))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return code

    def exchange(self, code: str, name: str, kind: str) -> PairingResult:
        name = name.strip()
        if not name or any(unicodedata.category(c).startswith("C") for c in name) or len(name.encode("utf-8")) > 80:
            raise PairingError("invalid_payload", "name must be 1 to 80 UTF-8 bytes without control characters")
        if kind not in ("iphone", "reference"):
            raise PairingError("invalid_payload", "kind must be iphone or reference")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = self._exchange(code, name, kind)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        if isinstance(result, PairingError):
            raise result
        return result

    def _exchange(self, code: str, name: str, kind: str) -> PairingResult | PairingError:
        now = self._wall()
        self._db.execute("DELETE FROM pairing_failures WHERE attempted_at <= ?", (now - 300,))
        locked_until = self._db.execute("SELECT locked_until FROM pairing_guard WHERE singleton = 1").fetchone()[0]
        if locked_until > now:
            return PairingError("pairing_locked", "Pairing is temporarily locked", status=429,
                                retry_after=math.ceil(locked_until - now))
        pairing = self._db.execute("SELECT code_hash, expires_at, consumed, origin FROM pairing WHERE singleton = 1").fetchone()
        matches = secrets.compare_digest(_digest(code), pairing[0] if pairing else bytes(32))
        if not pairing or not matches or pairing[2] or pairing[1] <= now:
            self._db.execute("INSERT INTO pairing_failures VALUES (?)", (now,))
            failures = self._db.execute("SELECT COUNT(*) FROM pairing_failures").fetchone()[0]
            if failures >= 5:
                self._db.execute("UPDATE pairing_guard SET locked_until = ? WHERE singleton = 1", (now + 300,))
                return PairingError("pairing_locked", "Pairing is temporarily locked", status=429, retry_after=300)
            if pairing and matches and not pairing[2] and pairing[1] <= now:
                return PairingError("code_expired", "That code has expired. Run ./jarvis pair again.")
            return PairingError("invalid_code", "That code isn't right. Check it and try again.")
        requested_name = name
        suffix = 1
        while self._db.execute("SELECT 1 FROM clients WHERE name = ? AND revoked_at IS NULL", (name,)).fetchone():
            suffix += 1
            ending = f" ({suffix})"
            stem = requested_name
            while len((stem + ending).encode("utf-8")) > 80:
                stem = stem[:-1]
            name = stem + ending
        client = PairedClient(secrets.token_hex(16), kind, name, now)
        token = f"{client.id}.{secrets.token_urlsafe(32)}"
        self._db.execute("INSERT INTO clients(id, kind, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                         (client.id, kind, name, _digest(token), now))
        self._db.execute("UPDATE pairing SET consumed = 1 WHERE singleton = 1")
        return PairingResult(client, token, pairing[3])

    def authenticate(self, token: str) -> PairedClient | None:
        client_id = token.partition(".")[0]
        with self._lock:
            row = self._db.execute("SELECT id, kind, name, created_at, token_hash FROM clients WHERE id = ? AND revoked_at IS NULL",
                                   (client_id,)).fetchone()
        matches = secrets.compare_digest(_digest(token), row[4] if row else bytes(32))
        return PairedClient(*row[:4]) if matches and row else None

    def active_ids(self, client_ids: set[str]) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT id FROM clients WHERE revoked_at IS NULL").fetchall()
        return {row[0] for row in rows} & client_ids

    def clients(self) -> list[PairedClient]:
        with self._lock:
            rows = self._db.execute("SELECT id, kind, name, created_at FROM clients WHERE revoked_at IS NULL ORDER BY created_at, id").fetchall()
        return [PairedClient(*row) for row in rows]

    def revoke(self, name: str) -> bool:
        with self._lock:
            return self._db.execute("UPDATE clients SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                                    (self._wall(), name)).rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> PairingStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
