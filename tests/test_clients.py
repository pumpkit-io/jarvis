from collections.abc import AsyncIterator
from pathlib import Path

from jarvis_hub.hub import Line
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, make_hub


async def test_two_clients_share_one_conversation_in_arrival_order() -> None:
    from jarvis_hub.clients import Client

    backend = FakeBackend(FakeClock(), [[Text("From the desk.")], [Text("From the phone.")]])
    output = RecordingOutput()
    hub = make_hub(backend, output, RecordingMetrics())
    hub.clients.register(Client("iphone-1", "iphone", connected=True, can_show_text=True, can_speak=True))

    async def inputs() -> AsyncIterator[Line]:
        yield Line("At my desk", 1.0)
        yield Line("On my phone", 2.0, client_id="iphone-1")

    await hub.start()
    try:
        await hub.run(inputs())
        assert hub.clients.focus == "iphone-1"
        assert output.replies == ["From the desk.", "From the phone."]
        assert len(backend.all_sessions("talker")) == 1
        assert backend.session("talker").received[-2:] == [
            '<user lang="en" typed="true">At my desk</user>',
            '<user lang="en" typed="true">On my phone</user>',
        ]
    finally:
        await hub.close()


async def test_focus_changes_explicitly_and_survives_restart_while_client_is_away(tmp_path: Path) -> None:
    from jarvis_hub.clients import Client

    state = tmp_path / "state.json"
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics(), state_path=state)
    await hub.start()
    try:
        terminal = hub.clients.get("terminal")
        assert terminal is not None
        assert (terminal.kind, terminal.connected, terminal.can_show_text, terminal.can_speak) == ("terminal", True, True, False)
        phone = Client("iphone-1", "iphone", connected=True, can_show_text=True, can_speak=True)
        hub.clients.register(phone)
        assert hub.clients.focus == "terminal"
        hub.clients.take_focus("iphone-1")
        assert hub.clients.focus == "iphone-1"
        hub.clients.disconnect("iphone-1")
        assert hub.clients.focus == "iphone-1"
        disconnected = hub.clients.get("iphone-1")
        assert disconnected is not None and not disconnected.connected
        hub.clients.register(phone)
        hub.clients.register(Client("reference-1", "reference", connected=True))
        assert hub.clients.focus == "iphone-1"
    finally:
        await hub.close()

    restarted = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics(), state_path=state)
    await restarted.start()
    try:
        assert restarted.clients.focus == "iphone-1"
        assert restarted.clients.get("iphone-1") is None
        restarted.clients.take_focus("terminal")
        assert restarted.clients.focus == "terminal"
    finally:
        await restarted.close()


async def test_each_authorization_records_its_input_client_without_reusing_turn_provenance() -> None:
    from jarvis_hub.clients import Client
    from tests.fakes import Tool

    grant = Tool("authorize", {"scope": "session", "description": "Work unattended this session"})
    backend = FakeBackend(FakeClock(), [[grant], [grant], [grant]])
    metrics = RecordingMetrics()
    hub = make_hub(backend, RecordingOutput(), metrics)
    hub.clients.register(Client("iphone-1", "iphone", connected=True))

    async def inputs() -> AsyncIterator[Line]:
        yield Line("Work unattended this session", 1.0, client_id="iphone-1")
        yield Line("Work unattended this session", 2.0)
        yield Line("Work unattended this session", 3.0, client_id="iphone-1")

    await hub.start()
    try:
        await hub.run(inputs())
        records = [entry for entry in metrics.records if entry.get("kind") == "authorization"]
        assert [entry["client_id"] for entry in records] == ["iphone-1", "terminal", "iphone-1"]
        assert [entry["authorization"] for entry in records] == ["U1", "U2", "U3"]
        assert len({entry["source"] for entry in records}) == 3
    finally:
        await hub.close()
