"""The optional loopback transport, embedded in the terminal-owned Hub lifetime."""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from typing import Any, cast

from aiohttp import web

from jarvis_hub.config import ConfigError
from jarvis_hub.pairing import PairingError, PairingStore


class Door:
    def __init__(
        self, *, enabled: bool = False, port: int = 8765, host: str = "127.0.0.1",
        health: Callable[[], dict[str, Any]], pairing: PairingStore | None = None, authenticate: Callable[[str], bool] = lambda token: False,
    ) -> None:
        self._enabled = enabled
        self._host = host
        self._configured_port = port
        self._health = health
        self._authenticate = authenticate
        self._pairing = pairing
        self._runner: web.AppRunner | None = None

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
        app.router.add_get("/v1/health", self._get_health)
        runner = web.AppRunner(app, access_log=None)
        self._runner = runner
        try:
            await runner.setup()
            await web.TCPSite(runner, self._host, self._configured_port).start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
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
                body = json.loads((await request.read()).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise PairingError("invalid_payload", "Expected a JSON object") from None
            if not isinstance(body, dict):
                raise PairingError("invalid_payload", "Expected a JSON object")
            body = cast(dict[str, Any], body)
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
