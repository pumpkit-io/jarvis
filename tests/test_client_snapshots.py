"""Current Client controls and Tasks through a real loopback Door and fake Hub sessions."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.pairing import PairingStore
from tests.fakes import FakeBackend, FakeClock, Gate, RecordingMetrics, RecordingOutput, Text, Tool, make_hub
from tests.protocol_exchanges import command, connect, receive


async def through(socket: aiohttp.ClientWebSocketResponse, kind: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while not frames or frames[-1]["type"] != kind:
        frames.append(await receive(socket))
    return frames


async def current(socket: aiohttp.ClientWebSocketResponse) -> list[dict[str, Any]]:
    return await through(socket, "replay_complete") + await through(socket, "pending_approval")


async def test_task_open_report_and_end_publish_complete_safe_snapshots(tmp_path: Path) -> None:
    finish = asyncio.Event()
    backend = FakeBackend(FakeClock(), [[Tool("delegate", {"request": "Review login", "context": "private context"}), Text("Working.")], [Text("Done.")]],
                          orchestrator=[[Gate(finish), Tool("report", {"task_id": "T1", "spoken": "Reviewed.", "needs_user": False})]])
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, on_replay=hub.publish_client_snapshots)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                initial = await current(socket)
                assert initial[-2]["type"] == "task_snapshot" and initial[-2]["payload"] == {"tasks": []}
                await socket.send_json(command("foreground", "set_presence", can_show_text=True))
                await through(socket, "command_result")
                await socket.send_json(command("task", "say_text", text="Please review login"))
                opened = await through(socket, "task_snapshot")
                assert opened[-1]["payload"] == {"tasks": [{"id": "T1", "title": "Review login", "state": "queued", "last_report": None, "approval_question": None}]}
                finish.set()
                frames = await through(socket, "task_snapshot")
                while frames[-1]["payload"]["tasks"]:
                    frames += await through(socket, "task_snapshot")
                assert frames[-1]["payload"] == {"tasks": []}
                assert "private context" not in str(initial + opened + frames)
                assert all(set(task) == {"id", "title", "state", "last_report", "approval_question"} for frame in opened + frames if frame["type"] == "task_snapshot" for task in frame["payload"]["tasks"])
        finally:
            await door.close()
            await hub.close()


@pytest.mark.parametrize("clear", ["cancel", "expire"])
async def test_pending_approval_uses_completed_talker_question_and_clears(tmp_path: Path, clear: str) -> None:
    from tests.fakes import until

    finish_question = asyncio.Event()
    question = "The launch thread is ready, sir. Shall I publish it now, exactly as shown?"
    backend = FakeBackend(FakeClock(), [
        [Tool("delegate", {"request": "Prepare launch", "context": "private deployment context"}), Text("Preparing.")],
        [Tool("present_approval", {"approval_id": "A1"}), Text(question), Gate(finish_question)],
        [Tool("cancel", {"task_id": "T1"}), Text("Cancelled.")],
    ], orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "May I publish?", "needs_user": True,
        "approval": {"action": "subagent_action", "grants": ["x_post"], "post": "812", "post_action": "post_now"}})]])
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await current(socket)
                await socket.send_json(command("foreground", "set_presence", can_show_text=True))
                await through(socket, "command_result")
                await socket.send_json(command("launch", "say_text", text="Prepare launch"))
                frames = await through(socket, "reply_delta")
                while frames[-1]["payload"]["text"] != question:
                    frames += await through(socket, "reply_delta")
                assert all(not f["payload"]["approvals"] for f in frames if f["type"] == "pending_approval")
                finish_question.set()
                committed = await through(socket, "pending_approval")
                assert committed[-1]["payload"] == {"approvals": [{"id": "A1", "task_id": "T1", "question": question, "expires_at": "1970-01-02T00:16:40Z"}]}
                task = next(f for f in committed if f["type"] == "task_snapshot")["payload"]["tasks"][0]
                assert task["approval_question"] == question and task["last_report"] == "May I publish?"
                assert set(committed[-1]["payload"]["approvals"][0]) == {"id", "task_id", "question", "expires_at"}
                await until(lambda: len(backend.session("talker").received) == 3)
                if clear == "cancel":
                    await socket.send_json(command("cancel", "say_text", text="Cancel this task"))
                else:
                    backend.clock.advance(86400)
                cleared = await through(socket, "pending_approval")
                assert cleared[-1]["payload"] == {"approvals": []}
                cleared_tasks = next(f for f in cleared if f["type"] == "task_snapshot")["payload"]["tasks"]
                if clear == "cancel":
                    assert cleared_tasks == []
                else:
                    assert cleared_tasks[0]["approval_question"] is None
                assert "private deployment context" not in str(frames + committed + cleared)
        finally:
            await door.close()
            await hub.close()


async def test_status_changes_and_reconnect_refresh_are_destination_specific(tmp_path: Path) -> None:
    from dataclasses import replace

    from jarvis_hub.clients import TERMINAL
    from tests.fakes import Limited, until

    warm = asyncio.Event()
    clock = FakeClock()
    backend = FakeBackend(clock, [[Tool("quiet_mode", {"on": True}), Text("Quiet.")], [Limited(60)], [Text("Recovered.")]], warm_up=[Gate(warm)])
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=clock.now) as log:
        first = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        second = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Other", "reference")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with connect(session, door, first.token) as one:
                    initial = await current(one)
                    status = next(f for f in initial if f["type"] == "status")
                    assert status["payload"] == {"ready": False, "usage_limit": {"limited": False, "resets_at": None}, "quiet": False, "focus_client_id": TERMINAL, "mac_voice_on": False, "silence": False}
                    warm.set()
                    assert (await through(one, "status"))[-1]["payload"]["ready"] is True
                    async with connect(session, door, second.token) as two:
                        await current(two)
                        phone = hub.clients.get(first.client.id)
                        terminal = hub.clients.get(TERMINAL)
                        assert phone is not None and terminal is not None
                        hub.attach_client(replace(phone, silence=True))
                        frames = await through(one, "status")
                        assert frames[-1]["client_id"] == first.client.id and frames[-1]["payload"]["silence"] is True
                        hub.attach_client(replace(terminal, can_speak=True))
                        frames = await through(two, "status")
                        assert frames[-1]["client_id"] == second.client.id and frames[-1]["payload"]["silence"] is False
                        assert frames[-1]["payload"]["mac_voice_on"] is True
                        await one.send_json(command("quiet", "say_text", text="Quiet please"))
                        quiet = await through(one, "status")
                        while not quiet[-1]["payload"]["quiet"]:
                            quiet += await through(one, "status")
                        assert quiet[-1]["payload"]["focus_client_id"] == first.client.id
                        await one.send_json(command("limit", "say_text", text="Another request"))
                        limited = await through(one, "status")
                        while not limited[-1]["payload"]["usage_limit"]["limited"]:
                            limited += await through(one, "status")
                        assert limited[-1]["payload"]["ready"] is False
                        assert limited[-1]["payload"]["usage_limit"] == {"limited": True, "resets_at": "1970-01-01T00:17:40Z"}
                        clock.advance(61)
                        await one.send_json(command("recover", "say_text", text="Try again"))
                        recovered = await through(one, "status")
                        while recovered[-1]["payload"]["usage_limit"]["limited"]:
                            recovered += await through(one, "status")
                        assert recovered[-1]["payload"]["ready"] is True
                        cursor = recovered[-1]["seq"]
                        await until(lambda: bool(hub.health()["ready"]))
                    async with connect(session, door, first.token, after=0) as resumed:
                        replay = await through(resumed, "replay_complete")
                        fresh = await through(resumed, "pending_approval")
                        assert [f["type"] for f in fresh][-3:] == ["status", "task_snapshot", "pending_approval"]
                        assert all(f["seq"] > replay[-1]["through_seq"] for f in fresh)
                        latest = next(f for f in fresh if f["type"] == "status")
                        assert latest["seq"] > cursor and latest["payload"]["silence"] is True
                        assert latest["payload"]["focus_client_id"] == first.client.id
                        assert latest["payload"]["usage_limit"]["limited"] is False
                        assert all(f["client_id"] == first.client.id for f in replay + fresh if f["type"] == "status")
                        async with session.get(f"http://127.0.0.1:{door.port}/v1/history?limit=500", headers={"Authorization": f"Bearer {second.token}"}) as response:
                            history = await response.json()
                        assert all(f["client_id"] == second.client.id and f["payload"]["silence"] is False for f in history["events"] if f["type"] == "status")
        finally:
            await door.close()
            await hub.close()


@pytest.mark.parametrize("replace_socket", [False, True])
async def test_background_socket_holds_news_until_foreground_without_moving_focus(tmp_path: Path, replace_socket: bool) -> None:
    from tests.fakes import Set, until

    report, reported = asyncio.Event(), asyncio.Event()
    backend = FakeBackend(FakeClock(), [
        [Tool("delegate", {"request": "Review login", "context": ""}), Text("Working.")],
        [Text("One held update, sir.")],
    ], orchestrator=[[Gate(report), Tool("report", {"task_id": "T1", "spoken": "Reviewed."}), Set(reported)]])
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        output = RecordingOutput()
        hub = make_hub(backend, EventOutput(log, output), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await current(socket)
                await socket.send_json(command("task", "say_text", text="Review login"))
                await through(socket, "reply_end")
                report.set()
                await reported.wait()
                await socket.send_json(command("background", "set_presence", can_show_text=False))
                frames = await through(socket, "command_result")
                while frames[-1]["id"] != "background":
                    frames += await through(socket, "command_result")
                assert frames[-1]["result"] == {"can_show_text": False}
                assert output.replies == ["Working."]
                assert hub.clients.focus == phone.client.id
                async with AsyncExitStack() as connections:
                    foreground = socket
                    if replace_socket:
                        foreground = await connections.enter_async_context(connect(session, door, phone.token, after=log.high_water))
                        await current(foreground)
                        assert output.replies == ["Working."]
                        assert hub.clients.focus == phone.client.id
                    await foreground.send_json(command("foreground", "set_presence", can_show_text=True))
                    shown = await through(foreground, "reply_end")
                    assert shown[-1]["payload"]["text"] == "One held update, sir."
                    await until(lambda: len(output.replies) == 2)
                    assert output.replies == ["Working.", "One held update, sir."]
                    assert hub.clients.focus == phone.client.id
                    assert backend.session("talker").received[-1].startswith('<event source="hub" away="1">')
        finally:
            await door.close()
            await hub.close()


async def test_background_presence_does_not_retire_an_active_announcement(tmp_path: Path) -> None:
    from dataclasses import replace

    from jarvis_hub.voice.speech import Speech
    from tests.fakes import FakeTts, RecordingPlayer, until

    arrival, remaining = asyncio.Event(), asyncio.Event()
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer(hold=True)
    backend = FakeBackend(clock, [[Tool("delegate", {"request": "Review login", "context": ""})],
        [Text("First part. "), Gate(remaining), Text("Second part.")]],
        orchestrator=[[Gate(arrival), Tool("report", {"task_id": "T1", "spoken": "Reviewed."})]])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(backend, EventOutput(log, output), metrics, speech=speech)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        speech.start()
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await current(socket)
                client = hub.clients.get(phone.client.id)
                assert client is not None
                hub.attach_client(replace(client, can_speak=True), player=remote)
                await socket.send_json(command("foreground", "set_presence", can_show_text=True))
                await through(socket, "command_result")
                await socket.send_json(command("task", "say_text", text="Review login"))
                await through(socket, "reply_end")
                arrival.set()
                await until(lambda: remote.texts == ["First part."])
                stops = remote.events.count("stop")
                await socket.send_json(command("background", "set_presence", can_show_text=False))
                result = await through(socket, "command_result")
                while result[-1]["id"] != "background":
                    result += await through(socket, "command_result")
                assert result[-1]["ok"] is True and remote.pending() > 0
                assert remote.events.count("stop") == stops
                remaining.set()
                await until(lambda: remote.texts == ["First part.", "Second part."])
                assert remote.events.count("stop") == stops
                remote.finish()
                await speech.idle()
                assert remote.played() == "First part. Second part."
                assert local.texts == []
                assert hub.clients.focus == phone.client.id
        finally:
            await door.close()
            await hub.close()
            await speech.close()


async def test_an_incomplete_question_is_not_published_as_pending(tmp_path: Path) -> None:
    from tests.fakes import Fail

    backend = FakeBackend(FakeClock(), [
        [Tool("delegate", {"request": "Prepare launch", "context": ""})],
        [Tool("present_approval", {"approval_id": "A1"}), Text("Shall I publish"), Fail("fake failure")],
    ], orchestrator=[[Tool("report", {"task_id": "T1", "spoken": "May I publish?", "needs_user": True,
        "approval": {"action": "subagent_action", "grants": ["x_post"], "post": "812", "post_action": "post_now"}})]])
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(backend, EventOutput(log, RecordingOutput()), RecordingMetrics())
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await current(socket)
                await socket.send_json(command("foreground", "set_presence", can_show_text=True))
                await through(socket, "command_result")
                await socket.send_json(command("launch", "say_text", text="Prepare launch"))
                frames = await through(socket, "reply_end")
                while frames[-1]["payload"]["reason"] != "error":
                    frames += await through(socket, "reply_end")
                async with connect(session, door, phone.token, after=log.high_water) as resumed:
                    refreshed = await current(resumed)
                    assert refreshed[-1]["payload"] == {"approvals": []}
                    assert all(not f["payload"]["approvals"] for f in frames if f["type"] == "pending_approval")
        finally:
            await door.close()
            await hub.close()


@pytest.mark.parametrize("foreground_between_chunks", [False, True])
async def test_background_before_first_sentence_cannot_resume_reply_tail(tmp_path: Path, foreground_between_chunks: bool) -> None:
    from dataclasses import replace

    from jarvis_hub.hub import Line
    from jarvis_hub.voice.speech import Speech
    from tests.fakes import FakeTts, RecordingPlayer, Set, until

    thinking, answer, opening, tail = (asyncio.Event() for _ in range(4))
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    local, remote = RecordingPlayer(), RecordingPlayer()
    backend = FakeBackend(clock, [
        [Set(thinking), Gate(answer), Text("The deployment failed. "), Set(opening), Gate(tail), Text("Check the logs.")],
        [Text("A complete later reply.")],
    ])
    speech = Speech(FakeTts(), local, clock, metrics=metrics, notice=output.notice)
    with PairingStore(tmp_path / "pairing.db") as pairing, EventLog(tmp_path / "events.db", wall=lambda: 1000) as log:
        phone = pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        hub = make_hub(backend, EventOutput(log, output), metrics, speech=speech)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log)
        speech.start()
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                await current(socket)
                client = hub.clients.get(phone.client.id)
                assert client is not None
                hub.attach_client(replace(client, can_speak=True), player=remote)
                await socket.send_json(command("foreground", "set_presence", can_show_text=True))
                await through(socket, "command_result")
                await hub.admit_line(Line("Hello", clock.now(), typed=False, client_id=phone.client.id))
                await thinking.wait()
                await socket.send_json(command("background", "set_presence", can_show_text=False))
                assert (await through(socket, "command_result"))[-1]["ok"]
                answer.set()
                await opening.wait()
                await speech.idle()
                if foreground_between_chunks:
                    await socket.send_json(command("foreground-again", "set_presence", can_show_text=True))
                    assert (await through(socket, "command_result"))[-1]["ok"]
                tail.set()
                await until(lambda: output.replies == ["The deployment failed. Check the logs."])
                await speech.idle()
                assert remote.texts == local.texts == []
                if not foreground_between_chunks:
                    await socket.send_json(command("foreground-again", "set_presence", can_show_text=True))
                    assert (await through(socket, "command_result"))[-1]["ok"]
                await hub.admit_line(Line("Another request", clock.now(), typed=False, client_id=phone.client.id))
                await until(lambda: len(output.replies) == 2)
                await speech.idle()
                assert remote.texts == ["A complete later reply."]
                assert local.texts == []
        finally:
            await door.close()
            await hub.close()
            await speech.close()
