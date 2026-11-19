#!/usr/bin/env python3
"""Protocol v1 reference Client. See README.md beside this file."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import random
import sqlite3
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid
import wave

import aiohttp
from aiohttp.abc import AbstractStreamWriter

Json = dict[str, Any]
MAX_PCM = 9_600_000


def emit(frame: Json) -> None:
    print(json.dumps(frame, ensure_ascii=False), flush=True)


def origin_url(origin: str) -> str:
    parsed = urlsplit(origin)
    local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local)) or not parsed.hostname:
        raise ValueError('Use the Hub HTTPS origin (HTTP is allowed only on loopback).')
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Supply an origin without credentials, path, query or fragment.')
    return origin.rstrip('/')


def save_credentials(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def pair(path: Path, origin: str, code: str, name: str) -> None:
    origin = origin_url(origin)
    async with aiohttp.ClientSession() as session:
        async with session.post(origin + '/v1/pair', json={'version': 1, 'code': code, 'name': name, 'kind': 'reference'},
                                allow_redirects=False) as response:
            body = await response.json()
            if response.status != 201:
                raise ValueError('Pairing failed: ' + body.get('error', {}).get('code', str(response.status)))
    save_credentials(path, {'origin': origin, 'client_id': body['client_id'], 'device_token': body['device_token']})
    emit({'type': 'paired', 'client_id': body['client_id']})


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, frame TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS cursor (seq INTEGER NOT NULL)')
        if self.db.execute('SELECT seq FROM cursor').fetchone() is None:
            self.db.execute('INSERT INTO cursor VALUES (0)')
        self.db.commit()

    @property
    def cursor(self) -> int:
        return int(self.db.execute('SELECT seq FROM cursor').fetchone()[0])

    def advance(self, seq: int, frame: Json | None = None) -> bool:
        with self.db:
            new = seq > self.cursor
            if frame is not None and new:
                self.db.execute('INSERT OR IGNORE INTO events VALUES (?, ?)', (seq, json.dumps(frame)))
            self.db.execute('UPDATE cursor SET seq = MAX(seq, ?)', (seq,))
        return new

    def frames(self) -> list[Json]:
        return [json.loads(row[0]) for row in self.db.execute('SELECT frame FROM events ORDER BY seq')]

    def close(self) -> None:
        self.db.close()


class ReferenceClient:
    def __init__(self, credentials: Path, output: Path, *, display: Callable[[Json], None] = emit) -> None:
        self.credentials = credentials
        self.identity: Json = json.loads(credentials.read_text())
        self.origin = origin_url(self.identity['origin'])
        cache_id = uuid.uuid5(uuid.NAMESPACE_URL, self.origin + '/' + self.identity['client_id']).hex
        self.cache = Cache(credentials.parent / (cache_id + '.sqlite3'))
        self.output = output
        self.display = display
        self.ready = False
        self.changed = asyncio.Event()
        self.socket: aiohttp.ClientWebSocketResponse | None = None
        self.pending: dict[str, tuple[Json, asyncio.Future[Json]]] = {}
        self.reader: asyncio.Task[None] | None = None
        self.failure: Exception | None = None
        self.streams: dict[str, tuple[str, int]] = {}
        self.clip_header: Json | None = None
        self.clips: list[tuple[str, str, float, Path]] = []
        self.ended_streams: set[str] = set()
        self.audio_done_replies: set[str] = set()
        self.silence = False
        self.reply_requests: dict[str, str | None] = {}
        self.session: aiohttp.ClientSession

    async def __aenter__(self) -> ReferenceClient:
        self.session = aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + self.identity['device_token']},
                                             timeout=aiohttp.ClientTimeout(total=35))
        self.reader = asyncio.create_task(self._run())
        try:
            await self.wait(lambda: self.ready)
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        await self.session.close()
        self.cache.close()

    async def wait(self, predicate: Callable[[], bool]) -> None:
        while True:
            self.changed.clear()
            if self.failure is not None:
                raise self.failure
            if predicate():
                return
            await self.changed.wait()

    def refused(self) -> None:
        self.credentials.unlink(missing_ok=True)
        raise ValueError('Pairing refused or revoked; pair again.')

    async def _run(self) -> None:
        delay = 1.0
        try:
            while True:
                try:
                    self.streams.clear()
                    self.clip_header = None
                    async with self.session.ws_connect(self.origin + f'/v1/events?after={self.cache.cursor}',
                                                       protocols=['jarvis.v1'], max_msg_size=0) as socket:
                        self.socket = socket
                        presence = uuid.uuid4().hex
                        async for message in socket:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                frame: Json = json.loads(message.data)
                                if self.clip_header is not None:
                                    await socket.close(code=1002)
                                    raise ValueError('Expected binary frame immediately after clip header.')
                                if frame.get('version') != 1:
                                    await socket.close(code=1002)
                                    raise ValueError('Unsupported event version.')
                                kind = frame['type']
                                if kind == 'replay_complete':
                                    self.cache.advance(frame['through_seq'])
                                    await socket.send_json({'version': 1, 'id': presence, 'type': 'set_presence',
                                                            'payload': {'can_show_text': True}})
                                elif kind == 'command_result':
                                    if frame.get('id') == presence:
                                        self.result(frame)
                                        for command, _future in list(self.pending.values()):
                                            await socket.send_json(command)
                                        self.ready = True
                                        delay = 1.0
                                    elif frame.get('id') in self.pending:
                                        future = self.pending[frame['id']][1]
                                        if not future.done():
                                            future.set_result(frame)
                                elif kind == 'speech_clip':
                                    self.clip_header = frame
                                else:
                                    self.apply(frame)
                            elif message.type == aiohttp.WSMsgType.BINARY:
                                try:
                                    self.save_clip(message.data)
                                except ValueError:
                                    await socket.close(code=1002)
                                    raise
                            elif message.type == aiohttp.WSMsgType.ERROR:
                                raise ConnectionError('WebSocket transport failed.')
                            self.changed.set()
                        if socket.close_code == 1008:
                            self.refused()
                except aiohttp.WSServerHandshakeError as error:
                    if error.status == 401:
                        self.refused()
                    if error.status not in (500, 502, 503, 504):
                        raise ValueError(f'WebSocket upgrade failed: HTTP {error.status}') from None
                except (aiohttp.ClientError, ConnectionError):
                    pass
                finally:
                    self.ready = False
                    self.socket = None
                    self.clip_header = None
                    self.changed.set()
                self.display({'type': 'reconnecting', 'after': self.cache.cursor})
                await asyncio.sleep(delay + random.uniform(0, delay / 4))
                delay = min(15, delay * 2)
        except Exception as error:
            self.failure = error
            self.changed.set()

    def apply(self, frame: Json) -> None:
        kind, payload = frame['type'], frame.get('payload', {})
        seq = frame.get('seq')
        if seq is None:
            return
        if kind == 'speech_begin':
            if (payload['sample_rate'], payload['channels'], payload['sample_width']) != (24000, 1, 2):
                raise ValueError('Unsupported speech format.')
            self.streams[payload['speech_id']] = (payload['reply_id'], 0)
        if kind == 'status':
            self.silence = payload.get('silence', False)
        if kind in ('speech_end', 'speech_stop'):
            self.ended_streams.add(payload['speech_id'])
            self.audio_done_replies.add(payload['reply_id'])
        if kind in ('reply_delta', 'reply_end'):
            self.reply_requests[payload['reply_id']] = payload.get('request_id')
        if self.cache.advance(seq, None if kind.startswith('speech_') else frame):
            self.display(frame)

    def save_clip(self, data: bytes) -> None:
        header = self.clip_header
        self.clip_header = None
        if header is None:
            raise ValueError('Binary audio without a clip header.')
        payload = header['payload']
        stream = self.streams.get(payload['speech_id'])
        if (stream != (payload['reply_id'], payload['index']) or not data or len(data) % 2
                or len(data) != payload['byte_length']):
            raise ValueError('Malformed or out-of-order audio clip.')
        received = time.perf_counter()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = uuid.uuid5(uuid.NAMESPACE_URL, payload['speech_id']).hex + f"-{payload['index']}.wav"
        path = self.output / filename
        with path.open('wb') as file:
            os.chmod(path, 0o600)
            with wave.open(file, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(data)
        self.streams[payload['speech_id']] = (payload['reply_id'], payload['index'] + 1)
        self.clips.append((payload['reply_id'], payload['speech_id'], received, path))
        if self.cache.advance(header['seq']):
            self.display(header)
            self.display({'type': 'saved_clip', 'path': str(path), 'reply_id': payload['reply_id'],
                          'speech_id': payload['speech_id'], 'index': payload['index']})

    @staticmethod
    def result(frame: Json) -> Json:
        if not frame.get('ok'):
            code = frame.get('error', {}).get('code', 'invalid_result')
            raise ValueError(f'Command failed: {code}' + ('; reconcile history before a new request.' if code == 'outcome_unknown' else ''))
        return frame['result']

    async def command(self, kind: str, payload: Json, *, id: str | None = None) -> Json:
        if not self.ready or self.socket is None:
            raise ValueError('Disconnected; a fresh request cannot be queued.')
        id = id or uuid.uuid4().hex
        envelope = {'version': 1, 'id': id, 'type': kind, 'payload': payload}
        future: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        if id in self.pending:
            raise ValueError('Command id already pending.')
        self.pending[id] = (envelope, future)
        try:
            try:
                await self.socket.send_json(envelope)
            except (aiohttp.ClientError, ConnectionError):
                await self.socket.close()
            await self.wait(lambda: future.done())
            return self.result(future.result())
        finally:
            self.pending.pop(id, None)

    async def reconnect(self) -> None:
        if self.socket is None:
            raise ValueError('Already disconnected.')
        self.ready = False
        await self.socket.close()
        await self.wait(lambda: self.ready)

    async def reply(self, request_id: str) -> Json:
        def ending() -> Json | None:
            return next((frame for frame in reversed(self.cache.frames())
                         if frame['type'] == 'reply_end' and frame['payload'].get('request_id') == request_id), None)
        await self.wait(lambda: ending() is not None)
        result = ending()
        assert result is not None
        return result


    async def upload(self, path: Path) -> tuple[Json, UploadTiming]:
        pcm = read_wav(path)
        talk_id, command_id = uuid.uuid4().hex, uuid.uuid4().hex
        await self.command('talk_begin', {'talk_id': talk_id})
        timing = UploadTiming(talk_id, command_id, time.perf_counter())
        headers = {'Idempotency-Key': command_id, 'Content-Type': 'application/octet-stream',
                   'X-Jarvis-Audio-Format': 'pcm_s16le_16000_mono'}
        async with asyncio.timeout(30):
            while True:
                attempt: Json = {'start': time.perf_counter(), 'body_handoff_end': None}
                timing.attempts.append(attempt)
                try:
                    async with self.session.post(self.origin + '/v1/utterances/' + talk_id,
                                                 data=TimedBody(pcm, attempt), headers=headers,
                                                 allow_redirects=False) as response:
                        if response.status == 401:
                            self.refused()
                        frame = await response.json()
                        timing.response_end = time.perf_counter()
                        result = self.result(frame)
                        if frame.get('id') != command_id or result.get('talk_id') != talk_id:
                            raise ValueError('Upload result identity mismatch.')
                        timing.request_id = result['request_id']
                        return result, timing
                except (aiohttp.ClientError, ConnectionError, OSError):
                    await asyncio.sleep(1)


def read_wav(path: Path) -> bytes:
    instructions = 'Convert first: ffmpeg -i INPUT -ar 16000 -ac 1 -c:a pcm_s16le OUTPUT.wav'
    try:
        with wave.open(str(path), 'rb') as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, 'NONE'):
                raise ValueError(instructions)
            if not 0 < wav.getnframes() <= MAX_PCM // 2:
                raise ValueError('WAV must contain more than zero and at most 300 seconds of audio.')
            pcm = wav.readframes(wav.getnframes())
            if len(pcm) != wav.getnframes() * 2:
                raise ValueError('WAV data is truncated. ' + instructions)
            return pcm
    except (wave.Error, EOFError) as error:
        raise ValueError('Unsupported WAV. ' + instructions) from error


class TimedBody(aiohttp.BytesPayload):
    def __init__(self, pcm: bytes, attempt: Json) -> None:
        super().__init__(pcm, content_type='application/octet-stream')
        self.attempt = attempt

    async def write(self, writer: AbstractStreamWriter) -> None:
        await self.write_with_length(writer, None)

    async def write_with_length(self, writer: AbstractStreamWriter, content_length: int | None) -> None:
        await super().write_with_length(writer, content_length)
        await writer.drain()
        # This is local transport handoff, not proof that the Hub received the final byte.
        self.attempt['body_handoff_end'] = time.perf_counter()


@dataclass
class UploadTiming:
    talk_id: str
    command_id: str
    release: float
    attempts: list[Json] = field(default_factory=lambda: list[Json]())
    request_id: str | None = None
    response_end: float | None = None

    def report(self, client: ReferenceClient) -> Json:
        candidates = [clip for clip in client.clips if clip[2] >= self.release
                      and client.reply_requests.get(clip[0]) == self.request_id]
        first = min(candidates, key=lambda clip: clip[2]) if candidates else None
        handed = next((attempt['body_handoff_end'] for attempt in self.attempts
                       if attempt['body_handoff_end'] is not None), None)
        receipt = first[2] if first is not None else None
        return {'type': 'upload_metrics', 'talk_id': self.talk_id, 'command_id': self.command_id,
                'request_id': self.request_id, 'reply_id': first[0] if first else None,
                'speech_id': first[1] if first else None, 'release_upload_start': self.release,
                'body_handoff_end': handed, 'first_matching_clip_received': receipt,
                'http_response_end': self.response_end, 'attempts': self.attempts,
                'upload_duration_seconds': handed - self.release if handed is not None else None,
                'release_to_first_clip_seconds': receipt - self.release if receipt is not None else None,
                'handoff_to_first_clip_seconds': receipt - handed if receipt is not None and handed is not None else None,
                'upload_boundary': 'local_post_write_and_drain_not_hub_receipt',
                'playback_boundary': 'clip_received_not_physical_playback'}


async def run(args: argparse.Namespace) -> None:
    if args.action == 'pair':
        await pair(args.credentials, args.origin, args.code or getpass.getpass('Pairing code: '), args.name)
        return
    if args.action == 'history':
        client = ReferenceClient(args.credentials, args.output)
        try:
            for frame in client.cache.frames():
                emit(frame)
        finally:
            client.cache.close()
        return
    async with ReferenceClient(args.credentials, args.output) as client:
        if args.action == 'say':
            result = await client.command('say_text', {'text': args.text})
            await client.reply(result['request_id'])
        elif args.action == 'talk':
            result, timing = await client.upload(args.wav)
            reply = await client.reply(result['request_id'])
            if not client.silence and reply['payload']['text'] and reply['payload']['reason'] == 'complete':
                await client.wait(lambda: reply['payload']['reply_id'] in client.audio_done_replies)
            emit(timing.report(client))
        elif args.action == 'listen':
            await client.wait(lambda: False)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--credentials', type=Path, default=Path.home() / '.config/jarvis-reference/credentials.json')
    result.add_argument('--output', type=Path, default=Path.home() / '.local/share/jarvis-reference/clips')
    result.add_argument('--timeout', type=float, default=120, help='Whole command deadline, seconds')
    commands = result.add_subparsers(dest='action', required=True)
    pairing = commands.add_parser('pair')
    pairing.add_argument('origin')
    pairing.add_argument('--code', help='Omit to enter secretly; shell arguments can be visible to other processes')
    pairing.add_argument('--name', default='Reference CLI')
    say = commands.add_parser('say')
    say.add_argument('text')
    talk = commands.add_parser('talk')
    talk.add_argument('wav', type=Path)
    commands.add_parser('listen')
    commands.add_parser('history', help='Print cached events without connecting or spending model usage')
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        async def bounded() -> None:
            async with asyncio.timeout(args.timeout):
                await run(args)
        asyncio.run(bounded())
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, TimeoutError, sqlite3.Error, aiohttp.ClientError) as error:
        message = 'Command timed out; reconnect to reconcile history before repeating a request.' if isinstance(error, TimeoutError) else str(error)
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
