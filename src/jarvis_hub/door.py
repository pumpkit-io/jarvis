"""The optional loopback transport, embedded in the terminal-owned Hub lifetime."""

from collections.abc import Callable
from typing import Any

from aiohttp import web

from jarvis_hub.config import ConfigError


class Door:
    def __init__(
        self, *, enabled: bool = False, port: int = 8765, host: str = "127.0.0.1",
        health: Callable[[], dict[str, Any]], authenticate: Callable[[str], bool] = lambda token: False,
    ) -> None:
        self._enabled = enabled
        self._host = host
        self._configured_port = port
        self._health = health
        self._authenticate = authenticate
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
        app = web.Application()
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
        if scheme.lower() != "bearer" or not separator or not token or not self._authenticate(token):
            return web.json_response(
                {"version": 1, "error": {"code": "unauthorized", "message": "Device token required", "retry_after_seconds": None}},
                status=401, headers={"WWW-Authenticate": "Bearer"},
            )
        return web.json_response(self._health(), headers={"Cache-Control": "no-store"})
