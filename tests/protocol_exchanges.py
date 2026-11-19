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
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, Gate, make_hub, until

FIXTURE = Path(__file__).resolve().parents[1] / "ios/JarvisClient/Tests/JarvisClientTests/Fixtures/client-v1.json"


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
        if isinstance(item, str) and key in {"client_id", "request_id", "reply_id"} and item != TERMINAL:
            if item not in identities:
                prefix = key.removesuffix("_id")
                counters[prefix] = counters.get(prefix, 0) + 1
                identities[item] = f"{prefix}-{counters[prefix]}"
            return identities[item]
        return item

    return visit(value)


async def exchanges(root: Path) -> dict[str, Any]:
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
        artifact: dict[str, Any] = {"version": 1, "normalization": "Only generated client/request/reply IDs are replaced by first-seen ordinal IDs. Fixed fake wall time is 1000 seconds.", "exchanges": {}}
        cases = artifact["exchanges"]
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, first.token) as one, connect(session, door, second.token) as two:
                    initial = [await receive(one), await receive(two)]
                    say = command("say-1", "say_text", text="Hello")
                    await one.send_json(say)
                    partial: list[dict[str, Any]] = []
                    result = None
                    while result is None or not any(e["type"] == "reply_delta" for e in partial):
                        frame = await receive(one)
                        if frame["type"] == "command_result":
                            result = frame
                        else:
                            partial.append(frame)
                    assert [e["type"] for e in partial] == ["heard", "reply_delta"]
                    assert [await receive(two), await receive(two)] == partial
                    assert result is not None and result["ok"]
                    await one.send_json(say)
                    repeated = await receive(one)
                    assert repeated == result
                    await one.send_json(command("say-1", "say_text", text="Different"))
                    conflict = await receive(one)
                    assert conflict["error"]["code"] == "id_conflict"
                    cases["typed_partial"] = {"initial": initial, "command": say, "events": partial, "result": result,
                                               "retry_result": repeated, "conflict_result": conflict}
                    await one.close()
                    clock.advance(1)
                    finish_reply.set()
                    await until(lambda: len(log.after(0)) == 4)
                    suffix = [await receive(two), await receive(two)]
                    assert [e["type"] for e in suffix] == ["reply_delta", "reply_end"]
                    async with connect(session, door, first.token, after=partial[-1]["seq"]) as resumed:
                        replay = [await receive(resumed), await receive(resumed), await receive(resumed)]
                        assert replay[:2] == suffix
                        assert replay[-1]["type"] == "replay_complete"
                        assert partial + suffix == [wire_event(e) for e in log.after(0)]
                        assert suffix[-1]["payload"]["text"] == "One two."
                        cases["reconnect_during_reply"] = {"after": partial[-1]["seq"], "frames": replay}
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/history?limit=2", headers={"Authorization": f"Bearer {first.token}"}) as response:
                            newest = await response.json()
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/history?before={newest['next_before']}&limit=2", headers={"Authorization": f"Bearer {first.token}"}) as response:
                            older = await response.json()
                        assert older["events"] + newest["events"] == partial + suffix
                        assert newest["has_more"] and not older["has_more"]
                        await resumed.send_json(command("ack-1", "acknowledge", seq=4))
                        ack = await receive(resumed)
                        await resumed.send_json(command("ack-2", "acknowledge", seq=1))
                        low_ack = await receive(resumed)
                        assert ack["result"] == low_ack["result"] == {"seq": 4}
                        cases["history_acknowledge"] = {"newest": newest, "older": older, "ack": ack, "lower_ack": low_ack}
                        async with connect(session, door, first.token, after=4) as replacement:
                            replacement_replay = await receive(replacement)
                            await until(lambda: (client := hub.clients.get(first.client.id)) is not None and client.connected)
                            await replacement.send_json(say)
                            assert await receive(replacement) == result
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
        cases.update(await concurrent_restart(root / "restart"))
        return normalize(artifact)


def encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


class DelayedAdmission:
    """Hold the admission result after the real Hub has accepted the line."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.clients = hub.clients
        self.admitted = asyncio.Event()
        self.release = asyncio.Event()

    def attach_client(self, client: Client) -> None:
        self.hub.attach_client(client)

    async def admit_line(self, line: Line, *, allowed: Callable[[], bool] = lambda: True) -> str | None:
        result = await self.hub.admit_line(line, allowed=allowed)
        self.admitted.set()
        await self.release.wait()
        return result


async def concurrent_restart(root: Path) -> dict[str, Any]:
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
                    await receive(original)
                    say = command("concurrent", "say_text", text="Accept once")
                    await original.send_json(say)
                    await asyncio.wait_for(delayed.admitted.wait(), 2)
                    await until(lambda: len(log.after(0)) == 3)
                    async with connect(session, door, first.token, after=3) as replacement:
                        assert (await receive(replacement))["type"] == "replay_complete"
                        await replacement.send_json(command("concurrent", "say_text", text="Conflict while pending"))
                        conflict = await receive(replacement)
                        assert conflict["error"]["code"] == "id_conflict"
                        await replacement.send_json(say)
                        delayed.release.set()
                        result = await receive(replacement)
                        assert result["ok"]
                        await replacement.send_json(say)
                        repeated = await receive(replacement)
                        assert repeated == result
                        cases["concurrent_retry"] = {"command": say, "conflict": conflict,
                            "result": result, "repeated": repeated, "events": [wire_event(e) for e in log.after(0)]}
                        delayed.release.clear()
                        delayed.admitted.clear()
                        uncertain = command("interrupted-admission", "say_text", text="Do not duplicate after restart")
                        await replacement.send_json(uncertain)
                        await asyncio.wait_for(delayed.admitted.wait(), 2)
                        await until(lambda: len(log.after(0)) == 6)
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
                async with aiohttp.ClientSession() as session, connect(session, restarted_door, first.token, after=6) as socket:
                    await receive(socket)
                    await socket.send_json(uncertain)
                    unknown = await receive(socket)
                    assert unknown["error"]["code"] == "outcome_unknown"
                    assert restarted_log.high_water == 6 and restarted_log.after(0) == []
                    await socket.send_json(say)
                    assert await receive(socket) == result
                    cases["restart_outcome_unknown"] = {"command": uncertain, "result": unknown}
            finally:
                await restarted_door.close()
                await restarted_hub.close()
        return cases
