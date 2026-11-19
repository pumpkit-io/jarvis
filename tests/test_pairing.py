from pathlib import Path
from typing import Any

import aiohttp
import pytest

from jarvis_hub.door import Door
from jarvis_hub.pairing import PairingStore
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, make_hub


async def test_pair_once_and_revoke_from_another_connection(tmp_path: Path) -> None:
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    path = tmp_path / "events.sqlite3"
    with PairingStore(path) as cli, PairingStore(path) as store:
        code = cli.create_code("https://mac.example.ts.net")
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        try:
            async with aiohttp.ClientSession() as client:
                base = f"http://127.0.0.1:{door.port}/v1"
                body = {"version": 1, "code": code, "name": "My iPhone", "kind": "iphone"}
                async with client.post(f"{base}/pair", json=body) as response:
                    assert response.status == 201
                    paired = await response.json()
                    assert paired["origin"] == "https://mac.example.ts.net"
                    assert paired["mac_name"] == hub.health()["mac_name"]
                    token = paired["device_token"]
                async with client.post(f"{base}/pair", json=body) as response:
                    assert response.status == 400
                    assert (await response.json())["error"]["code"] == "invalid_code"
                headers = {"Authorization": f"Bearer {token}"}
                async with client.get(f"{base}/health", headers=headers) as response:
                    assert response.status == 200
                assert [(c.name, c.kind) for c in cli.clients()] == [("My iPhone", "iphone")]
                assert cli.revoke("My iPhone")
                async with client.get(f"{base}/health", headers=headers) as response:
                    assert response.status == 401
        finally:
            await door.close()
            await hub.close()


async def test_expiry_replacement_and_lockout_survive_restarting_the_listener(tmp_path: Path) -> None:
    now = [1000.0]
    path = tmp_path / "events.sqlite3"
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    async with aiohttp.ClientSession() as client:
        for phase in range(3):
            with PairingStore(path, wall=lambda: now[0]) as store:
                door = Door(enabled=True, port=0, health=hub.health, pairing=store)
                await door.start()
                try:
                    base = f"http://127.0.0.1:{door.port}/v1/pair"

                    async def attempt(code: str) -> tuple[int, str, str | None]:
                        async with client.post(base, json={"version": 1, "code": code, "name": "Phone", "kind": "iphone"}) as response:
                            payload = await response.json()
                            return response.status, payload.get("error", {}).get("code", "ok"), response.headers.get("Retry-After")

                    if phase == 0:
                        old = store.create_code("https://mac.example.ts.net")
                        code = store.create_code("https://mac.example.ts.net")
                        assert await attempt(old) == (400, "invalid_code", None)
                        now[0] += 301
                        assert await attempt(code) == (400, "code_expired", None)
                        for _ in range(3):
                            assert await attempt("wrong") == (400, "invalid_code", None)
                        assert await attempt("wrong") == (429, "pairing_locked", "300")
                    elif phase == 1:
                        code = store.create_code("https://mac.example.ts.net")
                        assert await attempt(code) == (429, "pairing_locked", "300")
                        now[0] += 300
                    else:
                        code = store.create_code("https://mac.example.ts.net")
                        assert await attempt(code) == (201, "ok", None)
                finally:
                    await door.close()
    await hub.close()


async def test_concurrent_exchange_unique_names_and_revoked_name_reuse(tmp_path: Path) -> None:
    import asyncio

    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    path = tmp_path / "events.sqlite3"
    with PairingStore(path) as store, PairingStore(path) as cli:
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        try:
            async with aiohttp.ClientSession() as client:
                base = f"http://127.0.0.1:{door.port}/v1"

                async def attempt(code: str, name: str = "Phone") -> tuple[int, dict[str, object]]:
                    async with client.post(f"{base}/pair", json={"version": 1, "code": code, "name": name, "kind": "iphone"}) as response:
                        return response.status, await response.json()

                code = cli.create_code("https://override.example.ts.net")
                results = await asyncio.gather(attempt(code), attempt(code))
                assert sorted(status for status, _ in results) == [201, 400]
                first = next(body for status, body in results if status == 201)
                code = cli.create_code("https://override.example.ts.net")
                status, body = await attempt(code)
                assert status == 201
                assert body["client_name"] == "Phone (2)"
                second_token = body["device_token"]
                assert [c.name for c in cli.clients()] == ["Phone", "Phone (2)"]
                assert cli.revoke("Phone (2)")
                for token, expected in ((first["device_token"], 200), (second_token, 401)):
                    async with client.get(f"{base}/health", headers={"Authorization": f"Bearer {token}"}) as response:
                        assert response.status == expected
                assert cli.revoke("Phone")
                status, second = await attempt(cli.create_code("https://override.example.ts.net"))
                assert status == 201
                assert second["client_id"] != first["client_id"]
                for token, expected in ((first["device_token"], 401), (second["device_token"], 200), ("unknown", 401)):
                    async with client.get(f"{base}/health", headers={"Authorization": f"Bearer {token}"}) as response:
                        assert response.status == expected
        finally:
            await door.close()
            await hub.close()


async def test_pairing_validates_the_protocol_and_caps_json(tmp_path: Path) -> None:
    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    with PairingStore(tmp_path / "events.sqlite3") as store:
        code = store.create_code("https://mac.example.ts.net")
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        try:
            async with aiohttp.ClientSession() as client:
                url = f"http://127.0.0.1:{door.port}/v1/pair"
                invalid_bodies: list[tuple[Any, int]] = [([], 400), ({"version": 2}, 426), ({"version": True}, 426),
                                       ({"version": 1}, 400),
                                       ({"version": 1, "code": code, "name": "bad\nname", "kind": "iphone"}, 400),
                                       ({"version": 1, "code": code, "name": "\ud800", "kind": "iphone"}, 400),
                                       ({"version": 1, "code": code, "name": "é" * 41, "kind": "iphone"}, 400),
                                       ({"version": 1, "code": code, "name": "Phone", "kind": "terminal"}, 400)]
                for body, expected in invalid_bodies:
                    async with client.post(url, json=body) as response:
                        assert response.status == expected
                        assert response.headers["Cache-Control"] == "no-store"
                async with client.post(url, data="{", headers={"Content-Type": "application/json"}) as response:
                    assert response.status == 400
                async with client.post(url, data="x" * 65537, headers={"Content-Type": "application/json"}) as response:
                    assert response.status == 413
                    assert (await response.json())["error"]["code"] == "invalid_payload"
                async with client.post(url, json={"version": 1, "code": code, "name": "Phone", "kind": "iphone"}, headers={"Host": "evil.example"}) as response:
                    assert response.status == 201
                    assert (await response.json())["origin"] == "https://mac.example.ts.net"
        finally:
            await door.close()
            await hub.close()


_CLI_SCRIPT = '''
import sys
from pathlib import Path
from jarvis_hub import main as entry
from jarvis_hub.config import load_settings
root = Path(sys.argv.pop(1))
configured_origin = sys.argv.pop(1)
def settings(*args, **kwargs):
    return load_settings(root, {"JARVIS_REMOTE_ORIGIN": configured_origin})
def forbidden(*args, **kwargs):
    raise AssertionError("pairing CLI must not start models, audio, or Claude checks")
entry.load_settings = settings
entry.Mouth = entry.KokoroSynthesizer = entry.SoundPlayer = entry.SdkBackend = forbidden
entry.shutil.which = forbidden
entry.main()
'''


async def run_cli(root: Path, *arguments: str, configured_origin: str = "https://configured.example.ts.net") -> tuple[int, str, str]:
    import asyncio
    import sys

    process = await asyncio.create_subprocess_exec(sys.executable, "-c", _CLI_SCRIPT, str(root), configured_origin, *arguments,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


async def test_cli_processes_share_codes_and_revocations_with_the_running_door(tmp_path: Path) -> None:
    import re

    async def cli(*arguments: str) -> tuple[int, str]:
        status, stdout, stderr = await run_cli(tmp_path, *arguments)
        assert not stderr, stderr
        return status, stdout

    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    path = tmp_path / ".jarvis" / "events.sqlite3"
    with PairingStore(path) as store:
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        try:
            status, output = await cli("--speak", "pair", "--origin", "https://override.example.ts.net/")
            assert status == 0
            match = re.search(r"Pairing code: (\d{6})", output)
            assert match is not None
            code = match.group(1)
            assert "Origin: https://override.example.ts.net\n" in output
            assert len(output.splitlines()) > 15
            async with aiohttp.ClientSession() as client:
                base = f"http://127.0.0.1:{door.port}/v1"
                async with client.post(f"{base}/pair", json={"version": 1, "code": code, "name": "Phone", "kind": "iphone"}) as response:
                    assert response.status == 201
                    paired = await response.json()
                    assert paired["origin"] == "https://override.example.ts.net"
                    token = paired["device_token"]
                status, listed = await cli("clients")
                assert status == 0
                assert re.fullmatch(r"Phone\tiphone\t\d{4}-.*Z\n", listed)
                assert token not in listed and code not in listed
                assert token.encode() not in path.read_bytes()
                assert code.encode() not in path.read_bytes()
                assert token not in repr(store.authenticate(token))
                assert await cli("revoke", "Phone") == (0, "Revoked Phone.\n")
                async with client.get(f"{base}/health", headers={"Authorization": f"Bearer {token}"}) as response:
                    assert response.status == 401
                assert await cli("clients") == (0, "No paired devices.\n")
        finally:
            await door.close()
            await hub.close()


async def test_pair_cli_requires_a_safe_explicit_origin(tmp_path: Path) -> None:
    import re

    for origin in (None, "", "http://mac.example.ts.net", "https://pair-test-user:pair-test-secret@mac.example.ts.net",
                   "https://mac.example.ts.net/path", "https://mac.example.ts.net?x=1", "https://mac.example.ts.net#x",
                   "https://mac.example.ts.net:bad"):
        arguments = ("pair",) if origin is None else ("pair", "--origin", origin)
        status, stdout, stderr = await run_cli(tmp_path, *arguments, configured_origin="")
        assert status != 0
        assert stdout == ""
        assert stderr.startswith("jarvis-hub: ")
        assert "HTTPS origin" in stderr
        if not origin:
            assert "Set JARVIS_REMOTE_ORIGIN" in stderr
            assert "pair --origin" in stderr
        assert "Traceback" not in stderr
        assert "pair-test-user" not in stderr
        assert "pair-test-secret" not in stderr
        assert "Pairing code:" not in stderr
        assert "device_token" not in stderr
        assert re.search(r"\b\d{6}\b", stderr) is None


async def test_pairing_rejects_non_utf8_charsets_without_consuming_the_code(tmp_path: Path) -> None:
    import json

    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    with PairingStore(tmp_path / "events.sqlite3") as store:
        code = store.create_code("https://mac.example.ts.net")
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        try:
            async with aiohttp.ClientSession() as client:
                url = f"http://127.0.0.1:{door.port}/v1/pair"
                body = {"version": 1, "code": code, "name": "Phone", "kind": "iphone"}
                for charset in ("nonexistent-pair-test-secret", "utf-16", "iso-8859-1"):
                    async with client.post(url, data=json.dumps(body).encode("utf-8"),
                                           headers={"Content-Type": f"application/json; charset={charset}"}) as response:
                        assert response.status == 400
                        assert response.headers["Cache-Control"] == "no-store"
                        text = await response.text()
                        assert code not in text
                        assert charset not in text
                        assert json.loads(text) == {"version": 1, "error": {
                            "code": "invalid_payload", "message": "JSON must use UTF-8", "retry_after_seconds": None}}
                async with client.post(url, data=json.dumps(body).encode("utf-8"),
                                       headers={"Content-Type": "application/json; charset=UTF-8"}) as response:
                    assert response.status == 201
                    assert response.headers["Cache-Control"] == "no-store"
        finally:
            await door.close()
            await hub.close()


async def test_health_refuses_non_ascii_bearer_tokens_over_raw_http(tmp_path: Path) -> None:
    import asyncio
    import json

    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    with PairingStore(tmp_path / "events.sqlite3") as store:
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        assert door.port is not None
        try:
            for token in (b"\xff.x", b"x.\xff"):
                reader, writer = await asyncio.open_connection("127.0.0.1", door.port)
                try:
                    writer.write(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                                 + token + b"\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), timeout=2)
                    headers, body = response.split(b"\r\n\r\n", 1)
                    assert headers.split(b"\r\n", 1)[0] == b"HTTP/1.1 401 Unauthorized"
                    assert b"WWW-Authenticate: Bearer" in headers
                    assert json.loads(body) == {"version": 1, "error": {
                        "code": "unauthorized", "message": "Device token required", "retry_after_seconds": None}}
                finally:
                    writer.close()
                    await writer.wait_closed()
        finally:
            await door.close()
            await hub.close()


@pytest.mark.parametrize("malformed", ["large_integer", "nested_arrays", "NaN", "Infinity", "-Infinity"])
async def test_pairing_refuses_invalid_json_without_consuming_the_code(tmp_path: Path, malformed: str) -> None:
    import json

    hub = make_hub(FakeBackend(FakeClock()), RecordingOutput(), RecordingMetrics())
    with PairingStore(tmp_path / "events.sqlite3") as store:
        code = store.create_code("https://mac.example.ts.net")
        door = Door(enabled=True, port=0, health=hub.health, pairing=store)
        await door.start()
        try:
            async with aiohttp.ClientSession() as client:
                url = f"http://127.0.0.1:{door.port}/v1/pair"
                valid = {"version": 1, "code": code, "name": "Phone", "kind": "iphone"}
                if malformed == "large_integer":
                    body = '{"version":' + "9" * 5000 + "}"
                elif malformed == "nested_arrays":
                    body = "[" * 10_000 + "]" * 10_000
                else:
                    body = json.dumps(valid)[:-1] + ', "extra":' + malformed + "}"
                async with client.post(url, data=body.encode("utf-8"),
                                       headers={"Content-Type": "application/json"}) as response:
                    assert response.status == 400
                    assert response.headers["Cache-Control"] == "no-store"
                    text = await response.text()
                    assert code not in text
                    assert json.loads(text) == {"version": 1, "error": {
                        "code": "invalid_payload", "message": "Expected a JSON object", "retry_after_seconds": None}}
                async with client.post(url, json=valid) as response:
                    assert response.status == 201
        finally:
            await door.close()
            await hub.close()
