"""`jarvis-hub`: wiring, startup checks and the text-mode terminal adapters."""

import argparse
import asyncio
import io
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import AsyncIterator, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from jarvis_hub.agents.backend import AgentBackend
from jarvis_hub.agents.sdk import SdkBackend
from jarvis_hub.config import ConfigError, Settings, load_settings
from jarvis_hub.hub import Clock, Hub
from jarvis_hub.talker import talker_spec

PROMPT = "› "

# Each of these sends model calls past the Max subscription (ADR 0002).
BILLING_SWITCHES = {
    "ANTHROPIC_API_KEY": "every model call would be billed to the API",
    "ANTHROPIC_AUTH_TOKEN": "every model call would authenticate with that token",
    "CLAUDE_CODE_USE_BEDROCK": "every model call would go to Amazon Bedrock",
    "CLAUDE_CODE_USE_VERTEX": "every model call would go to Google Vertex AI",
}


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


def _guard_billing(environ: MutableMapping[str, str], stderr: TextIO) -> None:
    """Remove every billing switch from the environment the sessions inherit, with a loud warning for each."""
    loud, reset = ("\033[1;41;97m", "\033[0m") if stderr.isatty() else ("", "")
    for name, effect in BILLING_SWITCHES.items():
        if environ.pop(name, None) is None:
            continue
        stderr.write(
            f"{loud}WARNING: {name} is set in this shell, so {effect} instead of using your Max subscription. "
            f"The Hub removed it from its own environment for this run; unset it in your shell profile.{reset}\n"
        )
    stderr.flush()


async def _typed_lines(stdin: TextIO, stdout: TextIO, interactive: bool) -> AsyncIterator[str]:
    # A daemon thread, so a blocked read never keeps the process alive at exit.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    if isinstance(stdin, io.TextIOWrapper):
        stdin.reconfigure(errors="replace")

    def read() -> None:
        try:
            for line in stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\n"))
        except OSError:
            pass  # A terminal that went away ends the conversation like the end of input.
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=read, daemon=True).start()
    while (line := await queue.get()) is not None:
        if not interactive and line.strip():
            stdout.write(f"{PROMPT}{line.strip()}\n")
        yield line


async def text_mode(
    *,
    settings: Settings,
    environ: MutableMapping[str, str],
    backend: AgentBackend,
    clock: Clock,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """A terminal conversation with the Talker: typed lines in, streamed replies out, until end of input."""
    _guard_billing(environ, stderr)
    interactive = stdin.isatty()
    hub = Hub(
        talker=talker_spec(settings),
        backend=backend,
        clock=clock,
        output=TerminalOutput(stdout, stderr, prompt=interactive),
        metrics=JsonlMetrics(settings.runtime_dir / "metrics.jsonl"),
    )
    try:
        await hub.start()
        if interactive:
            stdout.write(PROMPT)
            stdout.flush()
        await hub.run(_typed_lines(stdin, stdout, interactive))
    finally:
        await hub.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-hub", description="Run the Jarvis Hub.")
    parser.add_argument("--text", action="store_true", help="talk by typing, with no audio")
    args = parser.parse_args()

    if not args.text:
        sys.exit("jarvis-hub: only text mode exists so far; run `jarvis-hub --text`.")
    cli = shutil.which("claude")
    if cli is None:
        sys.exit("jarvis-hub: the claude CLI is not on PATH; install Claude Code and log in with your Max plan.")
    try:
        settings = load_settings(Path(__file__).resolve().parents[2], os.environ)
    except ConfigError as e:
        sys.exit(f"jarvis-hub: {e}")

    # Ctrl-C reaches the claude CLI too (same process group), so it ends the conversation rather than a reply.
    try:
        asyncio.run(
            text_mode(
                settings=settings,
                environ=os.environ,
                backend=SdkBackend(cli),
                clock=MonotonicClock(),
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.exit(130)
