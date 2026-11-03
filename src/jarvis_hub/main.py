"""`jarvis-hub`: wiring, startup checks and the text-mode terminal adapters."""

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from jarvis_hub.agents.backend import AgentBackend, SessionSpec
from jarvis_hub.agents.sdk import SdkBackend
from jarvis_hub.config import ConfigError, load_settings
from jarvis_hub.hub import Clock, Hub, Metrics
from jarvis_hub.talker import talker_spec

PROMPT = "› "


class MonotonicClock:
    def now(self) -> float:
        return time.perf_counter()


class JsonlMetrics:
    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, entry: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **entry}
        with self._path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class TerminalOutput:
    def __init__(self, stdout: TextIO, stderr: TextIO, prompt: bool) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._prompt = prompt

    def reply(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    def reply_end(self) -> None:
        self._stdout.write("\n" + (PROMPT if self._prompt else ""))
        self._stdout.flush()

    def notice(self, text: str) -> None:
        self._stderr.write(f"[hub] {text}\n")
        self._stderr.flush()


def guard_api_key(environ: MutableMapping[str, str]) -> str | None:
    """Drop ANTHROPIC_API_KEY so no session bills the API instead of the Max subscription (ADR 0002)."""
    if environ.pop("ANTHROPIC_API_KEY", None) is None:
        return None
    return (
        "WARNING: ANTHROPIC_API_KEY is set in this shell. With it, every model call would be billed to the API, "
        "not your Max subscription. The Hub removed it from its own environment for this run; unset it in your "
        "shell profile."
    )


async def _typed_lines(stdin: TextIO, stdout: TextIO, interactive: bool) -> AsyncIterator[str]:
    # A daemon thread, so a blocked read never keeps the process alive at exit.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def read() -> None:
        for line in stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\n"))
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=read, daemon=True).start()
    while (line := await queue.get()) is not None:
        if not interactive and line.strip():
            stdout.write(f"{PROMPT}{line.strip()}\n")
        yield line


async def text_mode(
    *,
    talker: SessionSpec,
    backend: AgentBackend,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    metrics: Metrics,
    clock: Clock,
    handle_ctrl_c: bool = False,
) -> None:
    """A terminal conversation with the Talker: typed lines in, streamed replies out, until end of input."""
    interactive = stdin.isatty()
    hub = Hub(
        talker=talker,
        backend=backend,
        clock=clock,
        output=TerminalOutput(stdout, stderr, prompt=interactive),
        metrics=metrics,
    )
    await hub.start()
    if interactive:
        stdout.write(PROMPT)
        stdout.flush()
    conversation = asyncio.create_task(hub.run(_typed_lines(stdin, stdout, interactive)))

    async def ctrl_c() -> None:
        # Ctrl-C stops a reply in progress; between replies it ends the conversation.
        if not await hub.interrupt():
            conversation.cancel()

    if handle_ctrl_c:
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(ctrl_c()))
    try:
        await conversation
    except asyncio.CancelledError:
        stdout.write("\n")
    finally:
        await hub.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-hub", description="Run the Jarvis Hub.")
    parser.add_argument("--text", action="store_true", help="talk by typing, with no audio")
    args = parser.parse_args()

    warning = guard_api_key(os.environ)
    if warning:
        loud = "\033[1;41;97m" if sys.stderr.isatty() else ""
        sys.stderr.write(f"{loud}{warning}{chr(27) + '[0m' if loud else ''}\n")
    if not args.text:
        sys.exit("jarvis-hub: only text mode exists so far; run `jarvis-hub --text`.")
    cli = shutil.which("claude")
    if cli is None:
        sys.exit("jarvis-hub: the claude CLI is not on PATH; install Claude Code and log in with your Max plan.")
    try:
        settings = load_settings(Path(__file__).resolve().parents[2], os.environ)
    except ConfigError as e:
        sys.exit(f"jarvis-hub: {e}")

    asyncio.run(
        text_mode(
            talker=talker_spec(settings),
            backend=SdkBackend(cli),
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            metrics=JsonlMetrics(settings.runtime_dir / "metrics.jsonl"),
            clock=MonotonicClock(),
            handle_ctrl_c=True,
        )
    )
