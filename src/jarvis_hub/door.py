"""The optional loopback transport, embedded in the terminal-owned Hub lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, cast

from aiohttp import WSMsgType, web
import numpy as np

from jarvis_hub.voice.player import Clip, Player, RemotePlayer

from jarvis_hub.clients import Client, Clients
from jarvis_hub.event_log import Event, EventLog
from jarvis_hub.protocol import failure, json_object, sequence, wire_event
from jarvis_hub.uploads import MAX_AUDIO_BYTES, BufferTranscriber, Talk, TalkError, UploadedTalks, rejected

from jarvis_hub.config import ConfigError
from jarvis_hub.pairing import PairedClient, PairingError, PairingStore

if TYPE_CHECKING:
    from jarvis_hub.hub import Line


class Conversation(Protocol):
    clients: Clients
    speech_available: bool

    async def take_focus(self, client_id: str, *, allowed: Callable[[], bool] = lambda: True) -> None: ...

    async def begin_remote_talk(self, client_id: str, *, allowed: Callable[[], bool] = lambda: True) -> str: ...

    def cancel_remote_talk(self, client_id: str, request_id: str) -> None: ...

    def replay_speech(self, client_id: str, reply_id: str, text: str, lang: str) -> str: ...

    def set_client_talking(self, client_id: str, talking: bool) -> None: ...

    def attach_client(self, client: Client, *, player: Player | None = None) -> None: ...

    def set_client_playing(self, client_id: str, playing: bool) -> None: ...

    async def interrupt_client(self, client_id: str) -> None: ...

    def publish_client_snapshots(self, client_id: str) -> None: ...

    async def admit_line(self, line: Line, *, allowed: Callable[[], bool] = lambda: True) -> str | None: ...


@dataclass(eq=False)
class _Connection:
    client: PairedClient
    socket: web.WebSocketResponse
    queue: asyncio.Queue[tuple[dict[str, Any] | bytes, ...]] = field(default_factory=lambda: asyncio.Queue(maxsize=1024))
    subscription: object | None = None
    writer: asyncio.Task[None] | None = None
    closing: bool = False
    player: RemotePlayer | None = None


class _SpeechTransport:
    def __init__(self, publish: Callable[[str, dict[str, Any], bytes | None], None], playing: Callable[[bool], None],
                 silenced: Callable[[], bool]) -> None:
        self._publish, self._playing, self._silenced = publish, playing, silenced
        self._replies: dict[str, str] = {}

    def publish(self, kind: str, payload: dict[str, Any], binary: bytes | None = None) -> None:
        speech_id = payload["speech_id"]
        if speech_id in self._replies:
            self._publish(kind, {"reply_id": self._replies[speech_id], **payload}, binary)

    def begin(self, speech_id: str, reply_id: str) -> None:
        self._replies = {speech_id: reply_id}
        self.publish("speech_begin", {"speech_id": speech_id, "sample_rate": 24000, "channels": 1, "sample_width": 2})

    def send_clip(self, speech_id: str, index: int, clip: Clip) -> None:
        if clip.sample_rate != 24000:
            raise ValueError("Remote speech requires 24000 Hz mono PCM")
        pcm = (np.clip(clip.samples, -1, 1) * 32767).astype("<i2").tobytes()
        self.publish("speech_clip", {"speech_id": speech_id, "index": index, "text": clip.text, "byte_length": len(pcm)}, pcm)
        self._playing(True)

    def end(self, speech_id: str, last_clip_index: int) -> None:
        self.publish("speech_end", {"speech_id": speech_id, "last_clip_index": last_clip_index})

    def stop(self, speech_id: str) -> None:
        self.publish("speech_stop", {"speech_id": speech_id, "reason": "silence" if self._silenced() else "interrupted"})
        self._playing(False)


class Door:
    def __init__(
        self, *, enabled: bool = False, port: int = 8765, host: str = "127.0.0.1",
        health: Callable[[], dict[str, Any]], pairing: PairingStore | None = None, authenticate: Callable[[str], bool] = lambda token: False,
        hub: Conversation | None = None, log: EventLog | None = None, revocation_interval: float = 0.25,
        on_replay: Callable[[str], None] | None = None, now: Callable[[], float] = time.perf_counter,
        transcription: BufferTranscriber | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._enabled = enabled
        self._host = host
        self._configured_port = port
        self._health = health
        self._authenticate = authenticate
        self._pairing = pairing
        self._runner: web.AppRunner | None = None
        self._hub = hub
        self._log = log
        self._on_replay: Callable[[str], None] = on_replay or (hub.publish_client_snapshots if hub is not None else lambda client_id: None)
        self._now = now
        self._connections: dict[str, _Connection] = {}
        self._operations: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self._revocation_interval = revocation_interval
        self._watcher: asyncio.Task[None] | None = None
        self._retiring: set[asyncio.Task[None]] = set()
        self._closing = False
        self._talks = UploadedTalks(hub, log, transcription, now, sleep,
            lambda client_id: not self._closing and pairing is not None and bool(pairing.active_ids({client_id}))) if hub is not None and log is not None else None

    @property
    def port(self) -> int | None:
        if self._runner is None or not self._runner.addresses:
            return None
        return int(self._runner.addresses[0][1])

    async def start(self) -> None:
        if not self._enabled or self._runner is not None:
            return
        if self._host != "127.0.0.1":
            raise ConfigError("Remote door refused: bind address must be loopback 127.0.0.1")
        app = web.Application(client_max_size=65536)
        app.router.add_post("/v1/pair", self._pair)
        app.router.add_post("/v1/utterances/{talk_id}", self._utterance)
        app.router.add_get("/v1/health", self._get_health)
        app.router.add_get("/v1/events", self._events)
        app.router.add_get("/v1/history", self._history)
        runner = web.AppRunner(app, access_log=None)
        self._runner = runner
        try:
            await runner.setup()
            await web.TCPSite(runner, self._host, self._configured_port).start()
            self._closing = False
            self._watcher = asyncio.create_task(self._watch_revocations())
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        if self._talks is not None:
            await self._talks.close()
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        if self._operations:
            for operation in self._operations.values():
                operation.cancel()
            await asyncio.gather(*self._operations.values(), return_exceptions=True)
        await asyncio.gather(*(self._retire(c, 1001) for c in tuple(self._connections.values())))
        if self._retiring:
            await asyncio.gather(*self._retiring, return_exceptions=True)
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()

    async def _get_health(self, request: web.Request) -> web.Response:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token or not await self._authorized(token):
            return web.json_response(
                {"version": 1, "error": {"code": "unauthorized", "message": "Device token required", "retry_after_seconds": None}},
                status=401, headers={"WWW-Authenticate": "Bearer"},
            )
        return web.json_response(self._health(), headers={"Cache-Control": "no-store"})

    async def _authorized(self, token: str) -> bool:
        if self._pairing is not None:
            return await asyncio.to_thread(self._pairing.authenticate, token) is not None
        return self._authenticate(token)

    async def _pair(self, request: web.Request) -> web.Response:
        headers = {"Cache-Control": "no-store"}
        try:
            if request.content_type != "application/json":
                raise PairingError("invalid_payload", "Content-Type must be application/json")
            if request.charset is not None and request.charset.lower() not in ("utf-8", "utf8"):
                raise PairingError("invalid_payload", "JSON must use UTF-8")
            try:
                body = json_object(await request.read())
            except (ValueError, RecursionError):
                raise PairingError("invalid_payload", "Expected a JSON object") from None
            if type(body.get("version")) is not int or body["version"] != 1:
                return web.json_response({"version": 1, "supported_versions": [1], "error": {
                    "code": "unsupported_version", "message": "Use protocol version 1", "retry_after_seconds": None}},
                    status=426, headers=headers)
            if any(not isinstance(body.get(key), str) for key in ("code", "name", "kind")):
                raise PairingError("invalid_payload", "code, name and kind must be strings")
            if self._pairing is None:
                raise PairingError("invalid_code", "Create a pairing code on the Mac first")
            result = await asyncio.to_thread(self._pairing.exchange, body["code"], body["name"], body["kind"])
            return web.json_response({"version": 1, "client_id": result.client.id,
                "device_token": result.token, "client_name": result.client.name, "origin": result.origin, "mac_name": self._health()["mac_name"]},
                status=201, headers=headers)
        except web.HTTPRequestEntityTooLarge:
            error = PairingError("invalid_payload", "JSON body exceeds 64 KiB", status=413)
        except PairingError as caught:
            error = caught
        except sqlite3.OperationalError:
            error = PairingError("temporarily_unavailable", "Pairing store is busy; try again", status=503, retry_after=2)
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
        return web.json_response({"version": 1, "error": {"code": error.code, "message": str(error),
            "retry_after_seconds": error.retry_after}}, status=error.status, headers=headers)

    async def _identity(self, request: web.Request) -> PairedClient | None:
        scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not token or self._pairing is None:
            return None
        return await asyncio.to_thread(self._pairing.authenticate, token)

    def _current(self, connection: _Connection) -> bool:
        return self._connections.get(connection.client.id) is connection and not connection.closing and not self._closing

    def _active(self, connection: _Connection) -> bool:
        return self._current(connection) and self._pairing is not None and bool(self._pairing.active_ids({connection.client.id}))

    def _enqueue(self, connection: _Connection, *frames: dict[str, Any] | bytes) -> None:
        if not self._current(connection):
            return
        try:
            connection.queue.put_nowait(frames)
        except asyncio.QueueFull:
            self._retire_soon(connection, 1013)

    def _retire_soon(self, connection: _Connection, code: int) -> None:
        if connection.closing:
            return
        connection.closing = True
        task = asyncio.create_task(self._retire(connection, code))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _retire(self, connection: _Connection, code: int) -> None:
        connection.closing = True
        if connection.subscription is not None and self._log is not None:
            self._log.unsubscribe(connection.subscription)
            connection.subscription = None
        if self._connections.get(connection.client.id) is connection:
            del self._connections[connection.client.id]
            if self._talks is not None:
                self._talks.disconnect(connection.client.id)
            if self._hub is not None:
                self._hub.clients.disconnect(connection.client.id)
        writer = connection.writer
        if writer is not None and writer is not asyncio.current_task():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        while not connection.queue.empty():
            connection.queue.get_nowait()
            connection.queue.task_done()
        await connection.socket.close(code=code)

    async def _watch_revocations(self) -> None:
        while True:
            await asyncio.sleep(self._revocation_interval)
            connections = tuple(self._connections.values())
            if not connections or self._pairing is None:
                continue
            active = await asyncio.to_thread(self._pairing.active_ids, {c.client.id for c in connections})
            for connection in connections:
                if connection.client.id not in active:
                    self._retire_soon(connection, 1008)

    async def _write(self, connection: _Connection) -> None:
        try:
            while self._current(connection):
                frames = await connection.queue.get()
                try:
                    if not self._active(connection):
                        self._retire_soon(connection, 1008)
                        return
                    # A speech header and its binary frame must be one writer item.
                    for frame in frames:
                        async with asyncio.timeout(5):
                            if isinstance(frame, bytes):
                                await connection.socket.send_bytes(frame)
                            else:
                                await connection.socket.send_json(frame)
                finally:
                    frames = ()
                    frame = None
                    connection.queue.task_done()
        except (OSError, ConnectionError, TimeoutError, RuntimeError):
            self._retire_soon(connection, 1013)
        finally:
            frames = ()
            frame = None

    @staticmethod
    def _upload_response(command_id: str, result: dict[str, Any]) -> web.Response:
        code = result.get("error", {}).get("code")
        status = {"unauthorized": 401, "invalid_payload": 400, "unsupported_audio": 415,
                  "audio_too_large": 413, "upload_rate_limited": 429, "empty_transcript": 422,
                  "transcription_failed": 503}.get(code, 409 if code else 200)
        headers = {"Cache-Control": "no-store"}
        retry = result.get("error", {}).get("retry_after_seconds")
        if retry is not None:
            headers["Retry-After"] = str(retry)
        envelope: dict[str, Any] = {"version": 1, "type": "command_result"}
        if Door._valid_id(command_id):
            envelope["id"] = command_id
        envelope.update(result)
        return web.json_response(envelope, status=status, headers=headers)

    async def _utterance(self, request: web.Request) -> web.Response:
        command_id = request.headers.get("Idempotency-Key", "")
        client = await self._identity(request)
        if client is None:
            return self._upload_response(command_id, rejected("unauthorized", "Device token required"))
        talk_id = request.match_info["talk_id"]
        if not self._valid_id(command_id) or not self._valid_id(talk_id):
            return self._upload_response(command_id, rejected("invalid_payload", "Talk and command identifiers are required"))
        if (request.content_type != "application/octet-stream"
                or request.headers.get("X-Jarvis-Audio-Format") != "pcm_s16le_16000_mono"):
            return self._upload_response(command_id, rejected("unsupported_audio", "Use PCM16 little-endian, 16000 Hz mono"))
        body = bytearray()
        try:
            if request.content_length is not None and request.content_length > MAX_AUDIO_BYTES:
                return self._upload_response(command_id, rejected("audio_too_large", "A Talk is limited to 300 seconds"))
            if self._talks is not None:
                self._talks.receiving(client.id, talk_id)
            async with asyncio.timeout(30):
                async for chunk in request.content.iter_chunked(65536):
                    if len(body) + len(chunk) > MAX_AUDIO_BYTES:
                        return self._upload_response(command_id, rejected("audio_too_large", "A Talk is limited to 300 seconds"))
                    body.extend(chunk)
            if not body or len(body) % 2:
                return self._upload_response(command_id, rejected("invalid_payload", "PCM must have an even, nonzero byte length"))
            if self._log is None or self._talks is None or self._closing:
                return self._upload_response(command_id, rejected("transcription_failed", "Conversation is unavailable"))
            chunk = b""
            payload = {"talk_id": talk_id, "format": "pcm_s16le_16000_mono", "sha256": hashlib.sha256(body).hexdigest()}
            status, result = self._log.reserve(client.id, command_id, "upload", payload)
            key = client.id, command_id
            if status != "new" or result is not None:
                body.clear()
            if status == "conflict":
                result = rejected("id_conflict", "Command id was already used with different content")
            elif result is None:
                task = self._operations.get(key)
                if task is None and status == "new":
                    try:
                        talk = self._talks.claim(client.id, talk_id)
                    except TalkError as error:
                        result = rejected(error.code, str(error), error.retry_after)
                        self._log.complete(client.id, command_id, result)
                    else:
                        task = asyncio.create_task(self._upload(talk, command_id, body))
                        talk.operation = task
                        self._operations[key] = task
                        task.add_done_callback(lambda done: self._operations.pop(key, None))
                        body = bytearray()
                if result is None:
                    if task is None:
                        result = rejected("outcome_unknown", "Previous operation outcome is unknown; reconcile with history")
                        self._log.complete(client.id, command_id, result)
                    else:
                        result = await asyncio.shield(task)
            assert result is not None
            return self._upload_response(command_id, result)
        except (OSError, ConnectionError, TimeoutError):
            return self._upload_response(command_id, rejected("invalid_payload", "Incomplete upload; retry the same body and id"))
        finally:
            body.clear()

    async def _upload(self, talk: Talk, command_id: str, body: bytearray) -> dict[str, Any]:
        assert self._talks is not None and self._log is not None
        result = await self._talks.upload(talk, body)
        self._log.complete(talk.client_id, command_id, result)
        return result

    async def _history(self, request: web.Request) -> web.Response:
        client = await self._identity(request)
        if client is None:
            return web.json_response(failure("unauthorized", "Device token required"), status=401)
        if self._log is None:
            return web.json_response(failure("temporarily_unavailable", "Conversation is unavailable"), status=503)
        try:
            before = self._cursor(request.query["before"]) if "before" in request.query else None
            limit = int(request.query.get("limit", "100"))
            if not 1 <= limit <= 500:
                raise ValueError()
        except ValueError:
            return web.json_response(failure("invalid_payload", "Invalid history cursor or page size"), status=400)
        events, more = self._log.history(client.id, before, limit)
        return web.json_response({"version": 1, "events": [wire_event(e) for e in events],
                                  "next_before": events[0].seq if events else None, "has_more": more},
                                 headers={"Cache-Control": "no-store"})

    @staticmethod
    def _cursor(value: str) -> int:
        if not value.isascii() or not value.isdecimal():
            raise ValueError("Invalid cursor")
        return sequence(int(value))

    async def _events(self, request: web.Request) -> web.StreamResponse:
        client = await self._identity(request)
        if client is None:
            return web.json_response(failure("unauthorized", "Device token required"), status=401)
        if "jarvis.v1" not in request.headers.get("Sec-WebSocket-Protocol", "").replace(" ", "").split(","):
            return web.json_response({**failure("unsupported_version", "Use protocol version 1"), "supported_versions": [1]}, status=426)
        if self._log is None or self._hub is None or self._closing:
            return web.json_response(failure("temporarily_unavailable", "Conversation is unavailable"), status=503)
        try:
            after = self._cursor(request.query.get("after", "0"))
        except ValueError:
            return web.json_response(failure("invalid_cursor", "Invalid sequence cursor"), status=400)
        socket = web.WebSocketResponse(protocols=["jarvis.v1"], max_msg_size=65536, timeout=1, heartbeat=18, receive_timeout=60)
        connection = _Connection(client, socket)

        ready = False
        buffered: list[Event] = []

        def live(event: Event) -> None:
            if event.audience is not None and event.audience is not connection:
                return
            eligible = (event.client_id in (None, client.id) if not event.replayable else
                        event.type != "status" or event.client_id == client.id)
            if eligible:
                if ready:
                    self._enqueue(connection, wire_event(event), *(() if event.binary is None else (event.binary,)))
                else:
                    buffered.append(event)

        try:
            token, replay, mark, oldest, pruned = self._log.subscribe(after, asyncio.get_running_loop(), live, client.id)
        except ValueError:
            return web.json_response(failure("invalid_cursor", "Cursor exceeds the latest sequence"), status=409)
        connection.subscription = token
        try:
            await socket.prepare(request)
            previous = self._connections.get(client.id)
            if previous is not None and self._talks is not None:
                self._talks.disconnect(client.id)
            self._connections[client.id] = connection
            def publish(kind: str, payload: dict[str, Any], binary: bytes | None) -> None:
                if self._active(connection):
                    assert self._log is not None
                    self._log.append(kind, payload, client_id=client.id, replayable=False, binary=binary, audience=connection)

            def playing(active: bool) -> None:
                if self._current(connection) and self._hub is not None:
                    self._hub.set_client_playing(client.id, active)

            def silenced() -> bool:
                client_state = self._hub.clients.get(client.id) if self._hub is not None else None
                return client_state is not None and client_state.silence

            connection.player = RemotePlayer(_SpeechTransport(publish, playing, silenced))
            self._hub.attach_client(Client(client.id, "iphone" if client.kind == "iphone" else "reference", connected=True, can_show_text=False, can_speak=self._hub.speech_available,
                                           silence=self._log.silence(client.id)), player=connection.player)
            # Replay is queued before scheduled append notifications can run on this loop.
            complete: dict[str, Any] = {"version": 1, "type": "replay_complete", "through_seq": mark, "oldest_seq": oldest}
            if pruned:
                complete["history_pruned"] = True
            self._enqueue(connection, *(wire_event(event) for event in replay), complete)
            for event in buffered:
                self._enqueue(connection, wire_event(event), *(() if event.binary is None else (event.binary,)))
            ready = True
            self._on_replay(client.id)
            connection.writer = asyncio.create_task(self._write(connection))
            if previous is not None:
                await self._retire(previous, 1000)
            if not self._active(connection):
                await self._retire(connection, 1008)
                return socket
            async for message in socket:
                if not self._active(connection):
                    await self._retire(connection, 1008)
                    break
                if message.type == WSMsgType.TEXT:
                    try:
                        body = json_object(message.data.encode("utf-8"))
                        result = await self._command(connection, body)
                    except (ValueError, RecursionError, UnicodeError):
                        result = self._command_error("invalid_payload", "Expected a valid command envelope")
                    self._enqueue(connection, result)
                elif message.type == WSMsgType.BINARY:
                    await self._retire(connection, 1002)
                    break
        finally:
            await self._retire(connection, 1000)
        return socket

    async def _command(self, connection: _Connection, body: dict[str, Any]) -> dict[str, Any]:
        if type(body.get("version")) is not int or body["version"] != 1:
            return {**self._command_error("unsupported_version", "Use protocol version 1", body),
                    "supported_versions": [1]}
        command_id, kind, payload = body.get("id"), body.get("type"), body.get("payload")
        if (not self._valid_id(command_id) or not isinstance(kind, str) or not kind or len(kind.encode("utf-8")) > 128
                or not isinstance(payload, dict)):
            return self._command_error("invalid_payload", "Expected a valid command envelope", body)
        assert isinstance(command_id, str) and self._log is not None
        payload = cast(dict[str, Any], payload)
        status, result = self._log.reserve(connection.client.id, command_id, kind, payload)
        if status == "conflict":
            result = {"ok": False, "error": failure("id_conflict", "Command id was already used with different content")["error"]}
        elif result is None:
            key = (connection.client.id, command_id)
            task = self._operations.get(key)
            if task is None and status == "new":
                task = asyncio.create_task(self._execute(connection, command_id, kind, payload))
                self._operations[key] = task
                task.add_done_callback(lambda done: self._operations.pop(key, None))
            if task is None:
                result = {"ok": False, "error": failure("outcome_unknown", "Previous operation outcome is unknown; reconcile with history")["error"]}
                self._log.complete(connection.client.id, command_id, result)
            else:
                try:
                    result = await asyncio.shield(task)
                except Exception:
                    result = {"ok": False, "error": failure("outcome_unknown", "Operation outcome is unknown; reconcile with history")["error"]}
        return {"version": 1, "type": "command_result", "id": command_id, **result}

    def _command_error(self, code: str, message: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        result = {**failure(code, message), "type": "command_result", "ok": False}
        if body is not None and self._valid_id(body.get("id")):
            result["id"] = body["id"]
        return result

    @staticmethod
    def _valid_id(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            return False
        return 0 < len(encoded) <= 256 and all(ord(c) >= 32 for c in value)

    async def _execute(self, connection: _Connection, command_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        from jarvis_hub.hub import Line

        assert self._hub is not None and self._log is not None and self._pairing is not None
        result: dict[str, Any]
        try:
            if not self._active(connection):
                raise ValueError("Client is disconnected")
            if kind in ("playback_report", "interrupt"):
                speech_id = payload.get("speech_id")
                index = payload.get("clip_index" if kind == "playback_report" else "last_clip_index")
                player = connection.player
                if not isinstance(speech_id, str) or type(index) is not int or player is None:
                    raise ValueError("Unknown speech")
                if kind == "interrupt":
                    if set(payload) != {"speech_id", "last_clip_index"} or not player.heard_through(speech_id, index):
                        raise ValueError("Unknown speech")
                    await self._hub.interrupt_client(connection.client.id)
                    result = {"ok": True, "result": {"speech_id": speech_id, "stopped": True}}
                else:
                    state = payload.get("state")
                    if set(payload) != {"speech_id", "clip_index", "state"} or state not in ("started", "finished", "stopped"):
                        raise ValueError("Invalid report")
                    if state == "stopped":
                        if not player.accepts(speech_id, index):
                            raise ValueError("Unknown speech")
                        await self._hub.interrupt_client(connection.client.id)
                    elif not player.report(speech_id, index, state):
                        raise ValueError("Unknown speech")
                    self._hub.set_client_playing(connection.client.id, player.pending() > 0)
                    result = {"ok": True, "result": {}}
            elif kind == "say_text":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16384:
                    raise ValueError("text must be nonempty and at most 16384 UTF-8 bytes")
                request_id = await self._hub.admit_line(
                    Line(text, self._now(), client_id=connection.client.id),
                    allowed=lambda: self._active(connection))
                result = {"ok": True, "result": {"request_id": request_id}}
            elif kind in ("talk_begin", "talk_cancel"):
                talk_id = payload.get("talk_id")
                if set(payload) != {"talk_id"} or not self._valid_id(talk_id) or self._talks is None:
                    raise ValueError("A Talk identifier is required")
                assert isinstance(talk_id, str)
                result = (await self._talks.begin(connection.client.id, talk_id, allowed=lambda: self._active(connection)) if kind == "talk_begin"
                          else self._talks.cancel(connection.client.id, talk_id))
            elif kind == "take_focus":
                if payload:
                    raise ValueError("take_focus requires an empty payload")
                await self._hub.take_focus(connection.client.id, allowed=lambda: self._active(connection))
                result = {"ok": True, "result": {"focus_client_id": connection.client.id}}
            elif kind == "set_presence":
                visible = payload.get("can_show_text")
                if type(visible) is not bool or set(payload) != {"can_show_text"}:
                    raise ValueError("can_show_text must be a boolean")
                client = self._hub.clients.get(connection.client.id)
                if client is None:
                    raise ValueError("Client is disconnected")
                self._hub.attach_client(replace(client, can_show_text=visible))
                result = {"ok": True, "result": {"can_show_text": visible}}
            elif kind == "set_silence":
                silence = payload.get("silence")
                if type(silence) is not bool or set(payload) != {"silence"}:
                    raise ValueError("silence must be a boolean")
                self._hub.clients.set_silence(connection.client.id, silence)
                result = {"ok": True, "result": {"silence": silence}}
            elif kind == "replay_speech":
                reply_id = payload.get("reply_id")
                if set(payload) != {"reply_id"} or not self._valid_id(reply_id):
                    raise ValueError("A reply identifier is required")
                assert isinstance(reply_id, str)
                client = self._hub.clients.get(connection.client.id)
                if client is not None and client.silence:
                    result = rejected("silenced", "Voice is off for this Client")
                elif (reply := self._log.retained_reply(reply_id)) is None:
                    result = rejected("reply_not_found", "That reply is no longer available")
                else:
                    speech_id = self._hub.replay_speech(connection.client.id, reply_id, *reply)
                    result = {"ok": True, "result": {"speech_id": speech_id, "reply_id": reply_id}}
            elif kind == "acknowledge":
                seq = sequence(payload.get("seq"))
                if seq > self._log.high_water:
                    raise ValueError("Sequence exceeds the latest issued event")
                result = {"ok": True, "result": {"seq": self._pairing.acknowledge(connection.client.id, seq)}}
            else:
                result = {"ok": False, "error": failure("unknown_command", "Unknown command")["error"]}
        except TalkError as error:
            result = rejected(error.code, str(error), error.retry_after)
        except ValueError:
            result = {"ok": False, "error": failure("invalid_payload", "Command payload is invalid or Hub is stopping")["error"]}
        except (Exception, asyncio.CancelledError):
            result = {"ok": False, "error": failure("outcome_unknown", "Operation outcome is unknown; reconcile with history")["error"]}
        self._log.complete(connection.client.id, command_id, result)
        return result
