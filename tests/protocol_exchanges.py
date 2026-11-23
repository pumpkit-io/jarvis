"""Actual fake-agent loopback exchanges, also used by the checked-in Swift fixture generator."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, cast

import aiohttp

from jarvis_hub.clients import TERMINAL, Client
from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.hub import Hub, Line
from jarvis_hub.pairing import PairingStore
from jarvis_hub.protocol import wire_event
from jarvis_hub.voice.player import Player
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, Gate, Limited, Tool, make_hub, until

FIXTURE = Path(__file__).resolve().parents[1] / "ios/JarvisClient/Tests/JarvisClientTests/Fixtures/client-v1.json"


SNAPSHOTS = {"status", "task_snapshot", "pending_approval"}


async def receive_conversation(socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    while True:
        frame = await receive(socket)
        if frame["type"] not in SNAPSHOTS:
            return frame


class WireCapture:
    def __init__(self) -> None:
        self.streams: dict[aiohttp.ClientWebSocketResponse, list[dict[str, Any]]] = {}

    async def read(self, socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        frame = await receive(socket)
        self.streams.setdefault(socket, []).append(frame)
        return frame

    async def until(self, socket: aiohttp.ClientWebSocketResponse, kind: str) -> list[dict[str, Any]]:
        frames = [await self.read(socket)]
        while frames[-1]["type"] != kind:
            frames.append(await self.read(socket))
        return frames

    async def receive(self, socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        while True:
            frame = await self.read(socket)
            if frame["type"] not in SNAPSHOTS:
                return frame

    def frames(self) -> list[list[dict[str, Any]]]:
        return list(self.streams.values())


async def receive(socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    return await asyncio.wait_for(socket.receive_json(), 2)


@asynccontextmanager
async def connect(session: aiohttp.ClientSession, door: Door, token: str,
                  after: int = 0) -> AsyncGenerator[aiohttp.ClientWebSocketResponse]:
    async with session.ws_connect(f"http://127.0.0.1:{door.port}/v1/events?after={after}",
                                  headers={"Authorization": f"Bearer {token}"}, protocols=["jarvis.v1"]) as socket:
        yield socket


def command(id: str, kind: str, **payload: Any) -> dict[str, Any]:
    return {"version": 1, "id": id, "type": kind, "payload": payload}


def normalize(value: Any) -> Any:
    identities: dict[str, str] = {}
    counters: dict[str, int] = {}

    def visit(item: Any, key: str = "") -> Any:
        if isinstance(item, dict):
            return {k: visit(v, k) for k, v in cast(dict[str, Any], item).items()}
        if isinstance(item, list):
            return [visit(v) for v in cast(list[Any], item)]
        if isinstance(item, str) and key in {"client_id", "focus_client_id", "request_id", "reply_id", "speech_id"} and item != TERMINAL:
            if item not in identities:
                prefix = "client" if key == "focus_client_id" else key.removesuffix("_id")
                counters[prefix] = counters.get(prefix, 0) + 1
                identities[item] = f"{prefix}-{counters[prefix]}"
            return identities[item]
        return item

    return visit(value)


async def exchanges(root: Path) -> dict[str, Any]:
    capture = WireCapture()
    root.mkdir(parents=True, exist_ok=True)
    with PairingStore(root / "pairing.db") as pairing, EventLog(root / "events.db", wall=lambda: 1000) as log:
        first = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        second = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Reference", "reference")
        screen = RecordingOutput()
        clock = FakeClock()
        finish_reply = asyncio.Event()
        backend = FakeBackend(clock, [[Text("One"), Gate(finish_reply), Text(" two.")]])
        hub = make_hub(backend, EventOutput(log, screen), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, now=clock.now)
        await hub.start()
        await door.start()
        artifact: dict[str, Any] = {"version": 1, "normalization": "Only generated client/request/reply IDs, including Focus references and speech IDs, are replaced by first-seen ordinal IDs. Fake wall time starts at 1000 seconds.", "exchanges": {}}
        cases = artifact["exchanges"]
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, first.token) as one, connect(session, door, second.token) as two:
                    initial = [await capture.receive(one), await capture.receive(two)]
                    rejected: list[dict[str, Any]] = []
                    invalid_commands: tuple[dict[str, Any], ...] = (
                        {"version": 1, "id": "malformed-command", "type": "say_text", "payload": []},
                        {"version": 2, "id": "unsupported-command", "type": "say_text", "payload": {"text": "Hello"}},
                        {"version": 1, "type": "say_text", "payload": []},
                    )
                    for invalid in invalid_commands:
                        await one.send_json(invalid)
                        response = await capture.receive(one)
                        assert response["type"] == "command_result" and response["ok"] is False
                        assert response.get("id") == invalid.get("id")
                        rejected.append({"command": invalid, "result": response})
                    cases["rejected_envelopes"] = rejected
                    say = command("say-1", "say_text", text="Hello")
                    await one.send_json(say)
                    partial: list[dict[str, Any]] = []
                    result = None
                    while result is None or not any(e["type"] == "reply_delta" for e in partial):
                        frame = await capture.receive(one)
                        if frame["type"] == "command_result":
                            result = frame
                        else:
                            partial.append(frame)
                    assert [e["type"] for e in partial] == ["heard", "reply_delta"]
                    assert [await capture.receive(two), await capture.receive(two)] == partial
                    assert result is not None and result["ok"]
                    await one.send_json(say)
                    repeated = await capture.receive(one)
                    assert repeated == result
                    await one.send_json(command("say-1", "say_text", text="Different"))
                    conflict = await capture.receive(one)
                    assert conflict["error"]["code"] == "id_conflict"
                    cases["typed_partial"] = {"initial": initial, "command": say, "events": partial, "result": result,
                                               "retry_result": repeated, "conflict_result": conflict}
                    await one.close()
                    clock.advance(1)
                    finish_reply.set()
                    await until(lambda: len([e for e in log.after(0) if e.type not in SNAPSHOTS]) == 4)
                    suffix = [await capture.receive(two), await capture.receive(two)]
                    assert [e["type"] for e in suffix] == ["reply_delta", "reply_end"]
                    async with connect(session, door, first.token, after=partial[-1]["seq"]) as resumed:
                        replay = [await capture.receive(resumed), await capture.receive(resumed), await capture.receive(resumed)]
                        assert replay[:2] == suffix
                        assert replay[-1]["type"] == "replay_complete"
                        assert partial + suffix == [wire_event(e) for e in log.after(0) if e.type not in SNAPSHOTS]
                        assert suffix[-1]["payload"]["text"] == "One two."
                        cases["reconnect_during_reply"] = {"after": partial[-1]["seq"], "frames": replay}
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/history?limit=2", headers={"Authorization": f"Bearer {first.token}"}) as response:
                            newest = await response.json()
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/history?before={newest['next_before']}&limit=100", headers={"Authorization": f"Bearer {first.token}"}) as response:
                            older = await response.json()
                        assert [e for e in older["events"] + newest["events"] if e["type"] not in SNAPSHOTS] == partial + suffix
                        assert newest["has_more"] and not older["has_more"]
                        ack_seq = log.high_water
                        await resumed.send_json(command("ack-1", "acknowledge", seq=ack_seq))
                        ack = await capture.receive(resumed)
                        await resumed.send_json(command("ack-2", "acknowledge", seq=1))
                        low_ack = await capture.receive(resumed)
                        assert ack["result"] == low_ack["result"] == {"seq": ack_seq}
                        cases["history_acknowledge"] = {"newest": newest, "older": older, "ack": ack, "lower_ack": low_ack}
                        async with connect(session, door, first.token, after=log.high_water) as replacement:
                            replacement_replay = await capture.receive(replacement)
                            await until(lambda: (client := hub.clients.get(first.client.id)) is not None and client.connected)
                            await replacement.send_json(say)
                            assert await capture.receive(replacement) == result
                            process = await asyncio.create_subprocess_exec(
                                sys.executable, "-c",
                                "from pathlib import Path; import sys; from jarvis_hub.pairing import PairingStore; "
                                "store = PairingStore(Path(sys.argv[1])); "
                                "revoked = store.revoke('Phone'); store.close(); sys.exit(0 if revoked else 1)",
                                str(root / "pairing.db"), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            _, error = await process.communicate()
                            assert process.returncode == 0, error.decode()
                            closed = await asyncio.wait_for(replacement.receive(), 2)
                            assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 1008
                            cases["replacement_revocation"] = {"replay": replacement_replay, "close_code": closed.data}
                        await until(lambda: (client := hub.clients.get(first.client.id)) is not None and not client.connected)
                        assert (other := hub.clients.get(second.client.id)) is not None and other.connected
                        assert hub.clients.focus == first.client.id
                assert screen.replies == ["One two."]
                assert len(backend.session("talker").received) == 2
        finally:
            await door.close()
            await hub.close()
        cases["typed_wire_streams"] = capture.frames()
        cases.update(await concurrent_restart(root / "restart"))
        cases.update(await snapshot_exchanges(root / "snapshots"))
        cases.update(await large_event_exchanges(root / "large-events"))
        cases.update(await reset_exchange(root / "reset"))
        cases.update(await uploaded_exchange(root / "upload"))
        cases.update(await speech_exchange(root / "speech"))
        return normalize(artifact)


def encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


class DelayedAdmission:
    """Hold the admission result after the real Hub has accepted the line."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.clients = hub.clients
        self.speech_available = hub.speech_available
        self.begin_entered = asyncio.Event()
        self.cancel_remote_talk = hub.cancel_remote_talk
        self.set_client_talking = hub.set_client_talking
        self.set_client_playing = hub.set_client_playing
        self.replay_speech = hub.replay_speech
        self.reset_context = hub.reset_context
        self.focus_entered = asyncio.Event()
        self.interrupt_client = hub.interrupt_client
        self.admitted = asyncio.Event()
        self.release = asyncio.Event()

    async def take_focus(self, client_id: str, *, allowed: Callable[[], bool] = lambda: True) -> None:
        self.focus_entered.set()
        await self.hub.take_focus(client_id, allowed=allowed)

    async def begin_remote_talk(self, client_id: str, *, allowed: Callable[[], bool] = lambda: True) -> str:
        self.begin_entered.set()
        return await self.hub.begin_remote_talk(client_id, allowed=allowed)

    def publish_client_snapshots(self, client_id: str) -> None:
        self.hub.publish_client_snapshots(client_id)

    def attach_client(self, client: Client, *, player: Player | None = None) -> None:
        self.hub.attach_client(client, player=player)

    async def admit_line(self, line: Line, *, allowed: Callable[[], bool] = lambda: True) -> str | None:
        result = await self.hub.admit_line(line, allowed=allowed)
        self.admitted.set()
        await self.release.wait()
        return result


async def concurrent_restart(root: Path) -> dict[str, Any]:
    capture = WireCapture()
    root.mkdir(parents=True)
    with PairingStore(root / "pairing.db") as pairing:
        first = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        log = EventLog(root / "events.db", wall=lambda: 1000)
        backend = FakeBackend(FakeClock(), [[Text("Accepted.")], [Text("Uncertain.")]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        delayed = DelayedAdmission(hub)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=delayed, log=log, now=backend.clock.now)
        await hub.start()
        await door.start()
        cases: dict[str, Any] = {}
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, first.token) as original:
                    await capture.receive(original)
                    say = command("concurrent", "say_text", text="Accept once")
                    await original.send_json(say)
                    await asyncio.wait_for(delayed.admitted.wait(), 2)
                    await until(lambda: len([e for e in log.after(0) if e.type not in SNAPSHOTS]) == 3)
                    async with connect(session, door, first.token, after=log.high_water) as replacement:
                        assert (await capture.receive(replacement))["type"] == "replay_complete"
                        await replacement.send_json(command("concurrent", "say_text", text="Conflict while pending"))
                        conflict = await capture.receive(replacement)
                        assert conflict["error"]["code"] == "id_conflict"
                        await replacement.send_json(say)
                        delayed.release.set()
                        result = await capture.receive(replacement)
                        assert result["ok"]
                        await replacement.send_json(say)
                        repeated = await capture.receive(replacement)
                        assert repeated == result
                        cases["concurrent_retry"] = {"command": say, "conflict": conflict,
                            "result": result, "repeated": repeated, "events": [wire_event(e) for e in log.after(0) if e.type not in SNAPSHOTS]}
                        delayed.release.clear()
                        delayed.admitted.clear()
                        uncertain = command("interrupted-admission", "say_text", text="Do not duplicate after restart")
                        await replacement.send_json(uncertain)
                        await asyncio.wait_for(delayed.admitted.wait(), 2)
                        await until(lambda: len([e for e in log.after(0) if e.type not in SNAPSHOTS]) == 6)
                        restart_cursor = log.high_water
                        await door.close()
                assert len(backend.session("talker").received) == 3
        finally:
            await door.close()
            await hub.close()
            log.close()
        with EventLog(root / "events.db", wall=lambda: 1000 + 31 * 86400) as restarted_log:
            restarted_backend = FakeBackend(FakeClock())
            restarted_hub = make_hub(restarted_backend, EventOutput(restarted_log, RecordingOutput()), RecordingMetrics())
            restarted_door = Door(enabled=True, port=0, health=restarted_hub.health, pairing=pairing, hub=restarted_hub, log=restarted_log, now=restarted_backend.clock.now)
            await restarted_hub.start()
            await restarted_door.start()
            try:
                async with aiohttp.ClientSession() as session, connect(session, restarted_door, first.token, after=restart_cursor) as socket:
                    await capture.receive(socket)
                    await socket.send_json(uncertain)
                    unknown = await capture.receive(socket)
                    assert unknown["error"]["code"] == "outcome_unknown"
                    assert [e for e in restarted_log.after(0) if e.type not in SNAPSHOTS] == []
                    await socket.send_json(say)
                    assert await capture.receive(socket) == result
                    cases["restart_outcome_unknown"] = {"command": uncertain, "result": unknown}
            finally:
                await restarted_door.close()
                await restarted_hub.close()
        cases["retry_wire_streams"] = capture.frames()
        return cases


async def snapshot_exchanges(root: Path) -> dict[str, Any]:
    from dataclasses import replace

    root.mkdir(parents=True)
    capture = WireCapture()
    warm = asyncio.Event()
    question = "The launch thread is ready, sir. Shall I publish it now, exactly as shown?"
    clock = FakeClock()
    backend = FakeBackend(clock, [
        [Tool("quiet_mode", {"on": True}), Tool("delegate", {"request": "Prepare launch", "context": "private context"}), Text("Preparing.")],
        [Tool("present_approval", {"approval_id": "A1"}), Text(question)],
        [Tool("cancel", {"task_id": "T1"}), Text("Cancelled.")],
        [Limited(60)], [Text("Recovered.")],
    ], warm_up=[Gate(warm)], orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "May I publish?", "needs_user": True,
        "approval": {"action": "subagent_action", "grants": ["x_post"], "post": "812", "post_action": "post_now"}})]])
    with PairingStore(root / "pairing.db") as pairing, EventLog(root / "events.db", wall=clock.now) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        commands: list[dict[str, Any]] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, phone.token) as socket:
                    await capture.until(socket, "replay_complete")
                    await capture.until(socket, "pending_approval")
                    warm.set()
                    assert (await capture.until(socket, "status"))[-1]["payload"]["ready"]
                    for visible in (True, False, True):
                        control = command(f"presence-{len(commands)}", "set_presence", can_show_text=visible)
                        commands.append(control)
                        await socket.send_json(control)
                        assert (await capture.until(socket, "command_result"))[-1]["result"] == {"can_show_text": visible}
                    launch = command("launch", "say_text", text="Prepare launch")
                    commands.append(launch)
                    await socket.send_json(launch)
                    pending = await capture.until(socket, "pending_approval")
                    assert pending[-1]["payload"]["approvals"][0]["question"] == question
                    assert "private context" not in str(pending)
                    terminal = hub.clients.get(TERMINAL)
                    client = hub.clients.get(phone.client.id)
                    assert terminal is not None and client is not None
                    hub.attach_client(replace(terminal, can_speak=True))
                    assert (await capture.until(socket, "status"))[-1]["payload"]["mac_voice_on"]
                    hub.attach_client(replace(client, silence=True))
                    assert (await capture.until(socket, "status"))[-1]["payload"]["silence"]
                async with connect(session, door, phone.token) as resumed:
                    await capture.until(resumed, "replay_complete")
                    fresh = await capture.until(resumed, "pending_approval")
                    assert fresh[-1]["payload"]["approvals"][0]["question"] == question
                    assert [frame["type"] for frame in fresh][-3:] == ["status", "task_snapshot", "pending_approval"]
                    cancel = command("cancel", "say_text", text="Cancel this task")
                    commands.append(cancel)
                    await resumed.send_json(cancel)
                    assert (await capture.until(resumed, "pending_approval"))[-1]["payload"] == {"approvals": []}
                    await capture.until(resumed, "reply_end")
                    limited = command("limited", "say_text", text="Try something else")
                    commands.append(limited)
                    await resumed.send_json(limited)
                    frames = await capture.until(resumed, "status")
                    assert frames[-1]["payload"]["usage_limit"] == {"limited": True, "resets_at": "1970-01-01T00:17:40Z"}
                    clock.advance(61)
                    retry = command("recovered", "say_text", text="Try again")
                    commands.append(retry)
                    await resumed.send_json(retry)
                    frames = await capture.until(resumed, "status")
                    while frames[-1]["payload"]["usage_limit"]["limited"]:
                        frames += await capture.until(resumed, "status")
                    assert frames[-1]["payload"]["ready"]
            return {"snapshots_and_presence": {"commands": commands, "streams": capture.frames()}}
        finally:
            await door.close()
            await hub.close()


async def large_event_exchanges(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    capture = WireCapture()
    clock = FakeClock()
    text = "L" * 70_000
    backend = FakeBackend(clock, [[Tool("show", {"markdown": text}), Text(text)]])
    with PairingStore(root / "pairing.db") as pairing, EventLog(root / "events.db", wall=clock.now) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Large reply phone", "iphone")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await until(lambda: hub.health()["ready"])
        await door.start()
        say = command("large-events", "say_text", text="Show the complete result")
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, phone.token) as socket:
                    await capture.until(socket, "pending_approval")
                    await socket.send_json(say)
                    await capture.until(socket, "reply_end")
                    if not any(frame["type"] == "command_result" for frame in capture.frames()[0]):
                        await capture.until(socket, "command_result")
                live = capture.frames()[0]
                first_large = next(frame for frame in live if frame["type"] == "show")
                after = first_large["seq"] - 1
                async with connect(session, door, phone.token, after=after) as resumed:
                    await capture.until(resumed, "replay_complete")
                    await capture.until(resumed, "pending_approval")
            return {"large_event_replay": {"command": say, "after": after, "live": live, "replay": capture.frames()[1]}}
        finally:
            await door.close()
            await hub.close()


async def reset_exchange(root: Path) -> dict[str, Any]:
    """Reset context and its retry: the result is its status alone, and no event reaches either screen."""
    root.mkdir(parents=True, exist_ok=True)
    capture = WireCapture()
    clock = FakeClock()
    backend = FakeBackend(clock)
    with PairingStore(root / "pairing.db") as pairing, EventLog(root / "events.db", wall=clock.now) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Reset phone", "iphone")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics(),
                       conversation_path=root / "conversation.json")
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, now=clock.now)
        await hub.start()
        await door.start()
        reset = command("reset-1", "reset_context")
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await capture.receive(socket)
                await socket.send_json(reset)
                result = await capture.receive(socket)
                await socket.send_json(reset)
                repeated = await capture.receive(socket)
                await socket.send_json(command("reset-2", "reset_context", everything=True))
                refused = await capture.receive(socket)
                assert result["result"] == {"status": "reset"} and repeated == result
                assert refused["error"]["code"] == "invalid_payload"
                assert [e for e in log.after(0) if e.type not in SNAPSHOTS] == []
            return {"reset_context": {"command": reset, "result": result, "retry_result": repeated,
                                      "refused_payload": refused}}
        finally:
            await door.close()
            await hub.close()


async def uploaded_exchange(root: Path) -> dict[str, Any]:
    from jarvis_hub.voice.player import Samples

    class Transcriber:
        async def transcribe_buffer(self, audio: Samples) -> str:
            return 'Good evening from the phone'

    capture = WireCapture()
    root.mkdir(parents=True)
    with PairingStore(root / 'pairing.db') as pairing, EventLog(root / 'events.db', wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
        finish = asyncio.Event()
        backend = FakeBackend(FakeClock(), [[Gate(finish), Text('Good evening, sir.')]])
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, transcription=Transcriber())
        await hub.start()
        await until(lambda: hub.health()['ready'])
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await capture.until(socket, 'pending_approval')
                begin = command('talk-begin', 'talk_begin', talk_id='talk-1')
                await socket.send_json(begin)
                await capture.until(socket, 'command_result')
                async with session.post(f'http://127.0.0.1:{door.port}/v1/utterances/talk-1', data=b'\x00\x00', headers={
                    'Authorization': f'Bearer {phone.token}', 'Idempotency-Key': 'upload-1',
                    'Content-Type': 'application/octet-stream', 'X-Jarvis-Audio-Format': 'pcm_s16le_16000_mono',
                }) as response:
                    assert response.status == 200
                    receipt = await response.json()
                await capture.until(socket, 'heard')
                finish.set()
                await capture.until(socket, 'reply_end')
            return {'uploaded_talk': {'begin': begin, 'upload_result': receipt, 'live': capture.frames()[0]}}
        finally:
            await door.close()
            await hub.close()


async def speech_exchange(root: Path) -> dict[str, Any]:
    import base64
    import numpy as np
    from jarvis_hub.voice.speech import Speech
    from jarvis_hub.voice.player import Samples
    from tests.fakes import FakeTts, RecordingPlayer

    class PcmTts(FakeTts):
        def synthesize(self, text: str, voice: str) -> Samples:
            self.hold.wait(5)
            return np.array([-1, -.5, 0, .5, 1, 2], dtype=np.float32)

    root.mkdir(parents=True, exist_ok=True)
    with PairingStore(root / 'pairing.db') as pairing, EventLog(root / 'events.db', wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code('https://mac.example.ts.net'), 'Phone', 'iphone')
        clock, metrics, tts = FakeClock(), RecordingMetrics(), PcmTts()
        output = EventOutput(log, RecordingOutput())
        speech = Speech(tts, RecordingPlayer(), clock, metrics=metrics, notice=output.notice)
        hub = make_hub(FakeBackend(clock, [[Text('First sentence. Second sentence.')]]), output, metrics, speech=speech)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        speech.start()
        await speech.idle()
        await hub.start()
        await door.start()
        frames: list[dict[str, Any]] = []
        replay: list[dict[str, Any]] = []
        reports: list[dict[str, Any]] = []
        controls: list[dict[str, Any]] = []
        requested: list[dict[str, Any]] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, phone.token) as socket:
                    while (await receive(socket))['type'] != 'replay_complete':
                        pass
                    await socket.send_json(command('presence', 'set_presence', can_show_text=True))
                    while (await receive(socket)).get('id') != 'presence':
                        pass
                    tts.hold.clear()
                    await hub.admit_line(Line('Hello', clock.now(), typed=False, client_id=phone.client.id))
                    while True:
                        frame = await receive(socket)
                        if frame['type'] not in SNAPSHOTS:
                            frames.append(frame)
                        if frame['type'] == 'reply_end':
                            break
                    tts.hold.set()
                    while True:
                        frame = await receive(socket)
                        if frame['type'] not in SNAPSHOTS:
                            frames.append(frame)
                        if frame['type'] == 'speech_clip':
                            binary = await socket.receive()
                            assert binary.type == aiohttp.WSMsgType.BINARY
                            frames.append({'binary_base64': base64.b64encode(binary.data).decode('ascii')})
                        if frame['type'] == 'speech_end':
                            speech_id = frame['payload']['speech_id']
                            break
                    assert hub.clients.remote_activity
                    for index, state in [(0, 'started'), (0, 'finished'), (1, 'finished')]:
                        report = command(f'report-{index}-{state}', 'playback_report', speech_id=speech_id, clip_index=index, state=state)
                        await socket.send_json(report)
                        while (response := await receive(socket)).get('id') != report['id']:
                            pass
                        reports.append({'command': report, 'result': response})
                    assert not hub.clients.remote_activity
                    reply_id = next(frame['payload']['reply_id'] for frame in frames if frame.get('type') == 'speech_begin')
                    for control in [command('silence-on', 'set_silence', silence=True),
                                    command('silenced-replay', 'replay_speech', reply_id=reply_id),
                                    command('silence-off', 'set_silence', silence=False),
                                    command('missing-reply', 'replay_speech', reply_id='missing')]:
                        await socket.send_json(control)
                        while (response := await receive(socket)).get('id') != control['id']:
                            pass
                        controls.append({'command': control, 'result': response})
                    await socket.send_json(command('requested-replay', 'replay_speech', reply_id=reply_id))
                    while True:
                        frame = await receive(socket)
                        if frame['type'] not in SNAPSHOTS:
                            requested.append(frame)
                        if frame['type'] == 'speech_clip':
                            binary = await socket.receive()
                            assert binary.type == aiohttp.WSMsgType.BINARY
                            requested.append({'binary_base64': base64.b64encode(binary.data).decode('ascii')})
                        if frame['type'] == 'speech_end':
                            break
                async with connect(session, door, phone.token) as socket:
                    while True:
                        frame = await receive(socket)
                        replay.append(frame)
                        if frame['type'] == 'replay_complete':
                            break
            return {'speech_live': frames, 'speech_reports': reports, 'speech_replay': replay,
                    'speech_controls': controls, 'speech_requested_replay': requested}
        finally:
            tts.hold.set()
            await door.close()
            await hub.close()
            await speech.close()
