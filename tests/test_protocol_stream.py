import asyncio
from pathlib import Path
from typing import Any

import aiohttp

from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.pairing import PairingStore
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, make_hub, until


async def test_typed_exchange_is_durable_once_and_shared(tmp_path: Path) -> None:
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        first = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        second = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Reference", "reference")
        screen = RecordingOutput()
        backend = FakeBackend(FakeClock(), [[Text("Here, sir.")]])
        hub = make_hub(backend, EventOutput(log, screen), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{door.port}/v1/events?after=0"
                async with session.ws_connect(url, headers={"Authorization": f"Bearer {first.token}"}, protocols=["jarvis.v1"]) as one, session.ws_connect(url, headers={"Authorization": f"Bearer {second.token}"}, protocols=["jarvis.v1"]) as two:
                    assert (await one.receive_json())["type"] == "replay_complete"
                    assert (await two.receive_json())["type"] == "replay_complete"
                    command = {"version": 1, "id": "say-1", "type": "say_text", "payload": {"text": "Hello"}}
                    await one.send_json(command)
                    events: list[dict[str, Any]] = []
                    result = None
                    while result is None or not any(e["type"] == "reply_end" for e in events):
                        frame = await asyncio.wait_for(one.receive_json(), 2)
                        if frame["type"] == "command_result":
                            result = frame
                        else:
                            events.append(frame)
                    assert result is not None and result["ok"]
                    assert [e["type"] for e in events] == ["heard", "reply_delta", "reply_end"]
                    assert [await asyncio.wait_for(two.receive_json(), 2) for _ in events] == events
                    assert events[0]["payload"] == {"request_id": result["result"]["request_id"], "text": "Hello", "spoken": False}
                    assert events[-1]["payload"] == {"reply_id": events[1]["payload"]["reply_id"], "request_id": result["result"]["request_id"], "text": "Here, sir.", "reason": "complete"}
                    assert all(e["client_id"] == first.client.id for e in events)
                    await one.send_json(command)
                    assert await asyncio.wait_for(one.receive_json(), 2) == result
                    assert len(log.after(0)) == 3
                    assert ("heard", "Hello") in screen.log
        finally:
            await door.close()
            await hub.close()


async def test_shared_swift_fixture_matches_actual_loopback_exchanges(tmp_path: Path) -> None:
    from tests.protocol_exchanges import FIXTURE, encoded, exchanges

    assert encoded(await exchanges(tmp_path)) == FIXTURE.read_text(encoding="utf-8"), "Run uv run python -m tests.generate_protocol_fixtures"


async def test_history_filters_destination_status_and_preserves_pruned_high_water(tmp_path: Path) -> None:
    from tests.protocol_exchanges import connect, receive

    path = tmp_path / "events.db"
    with PairingStore(tmp_path / "pairing.db") as pairing:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        other = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Other", "reference")
        with EventLog(path, wall=lambda: 0) as log:
            log.append("notice", {"text": "old"}, client_id=None)
        with EventLog(path, wall=lambda: 31 * 86400) as log:
            assert log.high_water == 1 and log.after(0) == []
            hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
            door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
            await door.start()
            try:
                async with aiohttp.ClientSession() as session:
                    async with connect(session, door, phone.token) as socket:
                        assert await receive(socket) == {"version": 1, "type": "replay_complete", "through_seq": 1,
                                                         "oldest_seq": None, "history_pruned": True}
                        log.append("status", {"silence": True}, client_id=other.client.id)
                        visible = log.append("notice", {"text": "visible"}, client_id=None)
                        assert (await receive(socket))["seq"] == visible.seq == 3
                    async with session.get(f"http://127.0.0.1:{door.port}/v1/history?limit=1", headers={"Authorization": f"Bearer {phone.token}"}) as response:
                        page = await response.json()
                    assert [e["seq"] for e in page["events"]] == [3]
                    assert page["has_more"] is False
                    async with connect(session, door, phone.token) as socket:
                        assert (await receive(socket))["seq"] == 3
                        assert (await receive(socket))["through_seq"] == 3
                    for query, status in (("after=4", 409), ("after=-1", 400), ("after=9223372036854775808", 400), ("after=true", 400)):
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/events?{query}", headers={"Authorization": f"Bearer {phone.token}", "Sec-WebSocket-Protocol": "jarvis.v1"}) as response:
                            assert response.status == status
                    for endpoint in ("events", "history"):
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/{endpoint}") as response:
                            assert response.status == 401
            finally:
                await door.close()
                await hub.close()


async def test_terminal_disconnection_and_failure_cannot_suppress_durable_remote_output(tmp_path: Path) -> None:
    from jarvis_hub.clients import Client, TERMINAL
    from jarvis_hub.hub import Line

    class BrokenScreen(RecordingOutput):
        def heard(self, text: str, *, typed: bool = True) -> None:
            raise OSError("screen failed")

        def reply(self, text: str) -> None:
            raise OSError("screen failed")

        def reply_end(self) -> None:
            raise OSError("screen failed")

    with EventLog(tmp_path / "events.db") as log:
        backend = FakeBackend(FakeClock(), [[Text("Visible." )], [Text("Still visible.")]])
        hub = make_hub(backend, EventOutput(log, BrokenScreen()), RecordingMetrics())
        remote = RecordingOutput()
        hub.attach_client(Client("reference", "reference", connected=True), output=remote)
        await hub.start()
        try:
            await hub.admit_line(Line("First", 1000, client_id="reference"))
            await until(lambda: len(remote.replies) == 1)
            hub.clients.disconnect(TERMINAL)
            await hub.admit_line(Line("Second", 1000, client_id="reference"))
            await until(lambda: len(remote.replies) == 2)
            assert remote.replies == ["Visible.", "Still visible."]
            assert [e.type for e in log.after(0)] == ["heard", "reply_delta", "reply_end"] * 2
        finally:
            await hub.close()


async def test_command_validation_is_bounded_strict_and_durably_idempotent(tmp_path: Path) -> None:
    from tests.protocol_exchanges import command, connect, receive

    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db") as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        backend = FakeBackend(FakeClock())
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await receive(socket)
                for i, text in enumerate(("", " ", "é" * 8193)):
                    bad = command(f"invalid-{i}", "say_text", text=text)
                    await socket.send_json(bad)
                    result = await receive(socket)
                    assert result["error"]["code"] == "invalid_payload"
                    await socket.send_json(bad)
                    assert await receive(socket) == result
                for raw in ('{"version":1,"id":"nan","type":"say_text","payload":{"text":NaN}}',
                            '{"version":1,"id":"surrogate","type":"say_text","payload":{"text":"\\ud800"}}', "[]"):
                    await socket.send_str(raw)
                    assert (await receive(socket))["error"]["code"] == "invalid_payload"
                await socket.send_json(command("ack-future", "acknowledge", seq=1))
                assert (await receive(socket))["error"]["code"] == "invalid_payload"
                await socket.send_json(command("unknown", "never_a_tool", text="do not echo"))
                assert (await receive(socket))["error"]["code"] == "unknown_command"
                assert log.after(0) == []
        finally:
            await door.close()
            await hub.close()


async def test_worker_appends_during_replay_have_no_live_handover_gap(tmp_path: Path) -> None:
    from tests.protocol_exchanges import connect, receive

    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db") as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        for i in range(1200):
            log.append("notice", {"text": f"Replay {i}"}, client_id=None)
        hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await door.start()
        try:
            async with aiohttp.ClientSession() as session:
                started = asyncio.Event()

                async def workers() -> None:
                    await started.wait()
                    await asyncio.gather(*(asyncio.to_thread(log.append, "notice", {"text": f"Worker {i}"}, client_id=None)
                                           for i in range(40)))

                appending = asyncio.create_task(workers())
                started.set()
                async with connect(session, door, phone.token) as socket:
                    events: list[dict[str, Any]] = []
                    replay_complete = None
                    while len(events) < 1240 or replay_complete is None:
                        frame = await receive(socket)
                        if frame["type"] == "replay_complete":
                            replay_complete = frame
                        else:
                            events.append(frame)
                    await appending
                    assert [e["seq"] for e in events] == list(range(1, 1241))
                    assert [e["payload"] for e in events] == [e.payload for e in log.after(0)]
        finally:
            await door.close()
            await hub.close()


async def test_a_slow_replay_cannot_block_another_client_or_durability(tmp_path: Path) -> None:
    from tests.protocol_exchanges import connect, receive

    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db") as log:
        slow = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Slow", "reference")
        healthy = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Healthy", "reference")
        for _ in range(64):
            log.append("notice", {"text": "x" * 262144}, client_id=None)
        hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await door.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, slow.token), connect(session, door, healthy.token, after=64) as healthy_socket:
                    assert (await receive(healthy_socket))["through_seq"] == 64
                    log.append("notice", {"text": "Other Client remains live"}, client_id=None)
                    final = await receive(healthy_socket)
                    assert final["seq"] == 65 and final["payload"]["text"] == "Other Client remains live"
                    assert log.high_water == 65
        finally:
            await door.close()
            await hub.close()


async def test_terminal_eof_refuses_new_remote_turns_and_begun_replies_always_finish(tmp_path: Path) -> None:
    from collections.abc import AsyncGenerator

    import pytest

    from jarvis_hub.hub import Line
    from tests.fakes import Fail, Gate, Tool

    for reason, script in (("complete", [Text("Done.")]), ("error", [Fail("fake failure")]),
                           ("stand_by", [Tool("stand_by")])):
        with EventLog(tmp_path / f"{reason}.db") as log:
            backend = FakeBackend(FakeClock(), [script])
            output = RecordingOutput()
            hub = make_hub(backend, EventOutput(log, output), RecordingMetrics())
            await hub.start()

            async def terminal() -> AsyncGenerator[Line]:
                yield Line("Hello", 1000)

            try:
                await hub.run(terminal())
                [heard, *_, end] = log.after(0) if reason != "error" else [e for e in log.after(0) if e.type != "notice"]
                assert heard.type == "heard" and end.type == "reply_end"
                assert end.payload["reason"] == reason
                assert end.payload["request_id"] == heard.payload["request_id"]
                with pytest.raises(ValueError, match="stopping"):
                    await hub.admit_line(Line("After EOF", 1000))
            finally:
                await hub.close()
    with EventLog(tmp_path / "cancelled.db") as log:
        gate = asyncio.Event()
        output = RecordingOutput()
        hub = make_hub(FakeBackend(FakeClock(), [[Text("Partial"), Gate(gate)]]), EventOutput(log, output), RecordingMetrics())
        await hub.start()
        await hub.admit_line(Line("Hello", 1000))
        await until(lambda: bool(output.log and any(k == "reply" for k, _ in output.log)))
        await hub.close()
        assert log.after(0)[-1].payload["reason"] == "error"
        assert log.after(0)[-1].payload["text"] == "Partial"


async def test_live_only_sequence_gaps_are_not_reported_as_pruned_history(tmp_path: Path) -> None:
    from tests.protocol_exchanges import connect, receive

    with PairingStore(tmp_path / "pairing.db") as pairing:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        path = tmp_path / "events.db"
        with EventLog(path) as log:
            event = log.append("future_live_event", {"ephemeral": "not retained"}, client_id=phone.client.id, replayable=False)
            assert event.seq == 1 and log.after(0) == []
        with EventLog(path) as log:
            hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
            door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
            await door.start()
            try:
                async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                    assert await receive(socket) == {"version": 1, "type": "replay_complete", "through_seq": 1, "oldest_seq": None}
                    live = log.append("future_live_event", {"ephemeral": "also not retained"}, client_id=phone.client.id, replayable=False)
                    assert (await receive(socket))["seq"] == live.seq == 2
                    assert log.after(0) == []
            finally:
                await door.close()
                await hub.close()


async def test_typed_wire_identity_does_not_claim_the_mac_spoken_floor(tmp_path: Path) -> None:
    from jarvis_hub.voice.voice_mode import VoiceMode
    from jarvis_hub.voice.state import Timing
    from tests.fakes import FakeTts, RecordingPlayer, ScriptedAudio
    from tests.protocol_exchanges import command, connect, receive

    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db") as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        clock = FakeClock()
        output = RecordingOutput()
        metrics = RecordingMetrics()
        tts, player = FakeTts(), RecordingPlayer()
        voice = VoiceMode(audio=ScriptedAudio(clock), tts=tts, player=player, clock=clock, timing=Timing(follow_up_s=10, announce_follow_up_s=10, listen_s=6, chime_s=0.5), metrics=metrics, notice=output.notice)
        backend = FakeBackend(clock, [[Text("Text only.")]])
        hub = make_hub(backend, EventOutput(log, output), metrics, floor=voice, speech=voice.speech)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await receive(socket)
                await socket.send_json(command("typed-voice-mode", "say_text", text="From my phone"))
                frames: list[dict[str, Any]] = []
                while not any(f["type"] == "reply_end" for f in frames) or not any(f["type"] == "command_result" for f in frames):
                    frames.append(await receive(socket))
                [result] = [f for f in frames if f["type"] == "command_result"]
                assert result["ok"]
                [end] = [f for f in frames if f["type"] == "reply_end"]
                assert end["payload"]["request_id"] == result["result"]["request_id"]
                assert end["payload"]["text"] == "Text only."
                assert tts.calls == [] and player.clips == []
        finally:
            await door.close()
            await hub.close()
            await voice.close()


async def test_worker_notices_remain_ordered_while_display_clients_attach(tmp_path: Path) -> None:
    from jarvis_hub.clients import Client

    with EventLog(tmp_path / "events.db") as log:
        terminal = RecordingOutput()
        output = EventOutput(log, terminal)
        hub = make_hub(FakeBackend(FakeClock()), output, RecordingMetrics())
        screens: list[RecordingOutput] = []

        async def attach() -> None:
            for index in range(40):
                screen = RecordingOutput()
                screens.append(screen)
                hub.attach_client(Client(f"reference-{index}", "reference", connected=True), output=screen)
                await asyncio.sleep(0)

        async def worker(index: int) -> None:
            for item in range(20):
                await asyncio.to_thread(output.notice, f"Worker {index}: {item}")

        try:
            await asyncio.gather(attach(), *(worker(i) for i in range(4)))
            ordered = [event.payload["text"] for event in log.after(0)]
            assert terminal.notices == ordered and len(ordered) == 80
            assert screens[0].notices == ordered
            for screen in screens:
                assert screen.notices == [text for text in ordered if text in screen.notices]
        finally:
            await hub.close()


async def test_malformed_wire_commands_keep_a_safe_correlated_result_envelope(tmp_path: Path) -> None:
    from tests.protocol_exchanges import connect, receive

    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db") as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(FakeBackend(FakeClock()), EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await receive(socket)
                cases: list[tuple[dict[str, Any] | str, str | None, str]] = [
                    ({"version": 1, "id": "known-id", "type": "say_text", "payload": []}, "known-id", "invalid_payload"),
                    ({"version": 2, "id": "version-id", "type": "say_text", "payload": {"text": "private-marker"}}, "version-id", "unsupported_version"),
                    ({"version": 1, "id": "missing-payload", "type": "say_text"}, "missing-payload", "invalid_payload"),
                    ({"version": 1, "type": "say_text", "payload": []}, None, "invalid_payload"),
                    ({"version": 1, "id": 42, "type": "say_text", "payload": []}, None, "invalid_payload"),
                    ('{"id":"undecodable",', None, "invalid_payload"),
                ]
                for body, expected_id, code in cases:
                    if isinstance(body, str):
                        await socket.send_str(body)
                    else:
                        await socket.send_json(body)
                    response = await receive(socket)
                    assert response["type"] == "command_result" and response["ok"] is False
                    assert response["error"]["code"] == code
                    assert response.get("id") == expected_id
                    assert "private-marker" not in str(response)
                assert log.after(0) == []
        finally:
            await door.close()
            await hub.close()
