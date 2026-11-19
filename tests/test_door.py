import asyncio
import socket

import aiohttp

from jarvis_hub.door import Door
from tests.fakes import FakeBackend, FakeClock, Limited, RecordingMetrics, RecordingOutput, make_hub, until


async def test_authenticated_health_reports_the_fake_hubs_state_and_closes_its_listener() -> None:
    ready = asyncio.Event()
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics(), on_model_ready=ready.set)
    door = Door(enabled=True, port=0, health=hub.health, authenticate=lambda token: token == "fake-token")
    await door.start()
    assert door.port is not None
    port = door.port
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/v1/health", headers={"Authorization": "Bearer fake-token"}) as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["version"] == 1
                assert payload["ready"] is False
                assert payload["mac_name"]
                assert payload["usage_limit"] == {"limited": False, "resets_at": None}
            await hub.start()
            await asyncio.wait_for(ready.wait(), timeout=2)
            async with client.get(f"http://127.0.0.1:{port}/v1/health", headers={"Authorization": "Bearer fake-token"}) as response:
                assert (await response.json())["ready"] is True
            await hub.close()
            async with client.get(f"http://127.0.0.1:{port}/v1/health", headers={"Authorization": "Bearer fake-token"}) as response:
                assert (await response.json())["ready"] is False
    finally:
        await door.close()
        await hub.close()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


async def test_disabled_door_opens_no_port() -> None:
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    door = Door(port=port, health=hub.health)
    await door.start()
    try:
        assert door.port is None
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        await door.close()
        await hub.close()


async def test_enabled_door_denies_health_without_pairing() -> None:
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    door = Door(enabled=True, port=0, health=hub.health)
    await door.start()
    try:
        async with aiohttp.ClientSession() as client:
            for headers in ({}, {"Authorization": "Bearer fake-token"}):
                async with client.get(f"http://127.0.0.1:{door.port}/v1/health", headers=headers) as response:
                    assert response.status == 401
                    assert await response.json() == {
                        "version": 1,
                        "error": {"code": "unauthorized", "message": "Device token required", "retry_after_seconds": None},
                    }
    finally:
        await door.close()
        await hub.close()


async def test_nonloopback_bind_is_refused() -> None:
    import pytest

    from jarvis_hub.config import ConfigError

    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    try:
        for host in ("0.0.0.0", "192.168.1.1", "::", "localhost", ""):
            door = Door(enabled=True, port=0, host=host, health=hub.health)
            with pytest.raises(ConfigError, match="loopback"):
                await door.start()
            assert door.port is None
            await door.close()
    finally:
        await hub.close()


async def test_health_reports_a_usage_limit_without_internal_details() -> None:
    output = RecordingOutput()
    backend = FakeBackend(FakeClock(), warm_up=[Limited(600)])
    hub = make_hub(backend, output, RecordingMetrics())
    door = Door(enabled=True, port=0, health=hub.health, authenticate=lambda token: token == "fake-token")
    try:
        await door.start()
        await hub.start()
        await until(lambda: any("usage limit" in text for kind, text in output.log if kind == "notice"))
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{door.port}/v1/health", headers={"Authorization": "Bearer fake-token"}) as response:
                payload = await response.json()
                assert payload["ready"] is False
                assert payload["usage_limit"] == {"limited": True, "resets_at": "1970-01-01T00:26:40Z"}
                assert set(payload) == {"version", "ready", "mac_name", "usage_limit"}
    finally:
        await door.close()
        await hub.close()
