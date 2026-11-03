"""`jarvis-hub`: wiring, startup checks and the text-mode terminal adapters."""

import argparse
import asyncio
import contextlib
import io
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown

from jarvis_hub.agents.backend import AgentBackend
from jarvis_hub.agents.sdk import SdkBackend
from jarvis_hub.config import ConfigError, Settings, load_settings
from jarvis_hub.hub import Clock, Hub, Line
from jarvis_hub.orchestrator import orchestrator_spec
from jarvis_hub.talker import talker_spec
from jarvis_hub.voice import language
from jarvis_hub.voice.player import Player, SoundPlayer
from jarvis_hub.voice.speech import Speech, SpeechClock
from jarvis_hub.voice.tts import KokoroSynthesizer, Synthesizer

PROMPT = "› "

# Each of these sends model calls past the Max subscription (ADR 0002).
BILLING_SWITCHES = {
    "ANTHROPIC_API_KEY": "every model call would be billed to the API",
    "ANTHROPIC_AUTH_TOKEN": "every model call would authenticate with that token",
    "ANTHROPIC_BASE_URL": "every model call would go to that endpoint",
    "CLAUDE_CODE_USE_BEDROCK": "every model call would go to Amazon Bedrock",
    "CLAUDE_CODE_USE_VERTEX": "every model call would go to Google Vertex AI",
    "CLAUDE_CODE_USE_FOUNDRY": "every model call would go to Microsoft Foundry",
}


class MonotonicClock:
    def now(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Voice:
    """What `--speak` adds: a synthesiser and the player for the default output."""

    tts: Synthesizer
    player: Player


class JsonlMetrics:
    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, entry: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **entry}
        with self._path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class _Console(Console):
    def on_broken_pipe(self) -> None:
        # Rich's default exits the whole process; the Hub loses only the screen.
        raise BrokenPipeError("the terminal is gone")


class TerminalOutput:
    """Replies and notices as plain text; shown details rendered as markdown and appended to the show log."""

    def __init__(self, stdout: TextIO, stderr: TextIO, prompt: bool, show_log: Path) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._prompt = prompt
        self._show_log = show_log
        self._console = _Console(file=stdout, highlight=False)
        self._mid_reply = False

    def heard(self, text: str) -> None:
        # A terminal already shows what was typed; piped input is echoed so the transcript reads in order.
        if not self._prompt:
            self._stdout.write(f"{PROMPT}{text}\n")

    def reply(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()
        self._mid_reply = True

    def reply_end(self) -> None:
        self._stdout.write("\n" + (PROMPT if self._prompt else ""))
        self._stdout.flush()
        self._mid_reply = False

    def notice(self, text: str) -> None:
        # With stderr gone there is nowhere left to say it, and the conversation goes on without it.
        with contextlib.suppress(OSError):
            self._stderr.write(f"[hub] {text}\n")
            self._stderr.flush()

    def show(self, markdown: str) -> None:
        try:
            if self._mid_reply:
                self._stdout.write("\n")
            self._console.print(Markdown(markdown))
            self._stdout.flush()
        except OSError as e:
            self.notice(f"Could not show the details on screen: {e}")
        try:
            self._show_log.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._show_log.open("a") as f:
                f.write(f"<!-- shown {stamp} -->\n{markdown.rstrip()}\n\n")
        except OSError as e:
            self.notice(f"Could not write the show log: {e}")


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


async def _typed_lines(stdin: TextIO, clock: Clock) -> AsyncIterator[Line]:
    # A daemon thread, so a blocked read never keeps the process alive at exit.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Line | None] = asyncio.Queue()
    if isinstance(stdin, io.TextIOWrapper):
        stdin.reconfigure(errors="replace")

    def read() -> None:
        try:
            for line in stdin:
                loop.call_soon_threadsafe(queue.put_nowait, Line(line.rstrip("\n"), clock.now()))
        except OSError:
            pass  # A terminal that went away ends the conversation like the end of input.
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=read, daemon=True).start()
    while (line := await queue.get()) is not None:
        yield line


async def text_mode(
    *,
    settings: Settings,
    environ: MutableMapping[str, str],
    backend: AgentBackend,
    clock: SpeechClock,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    voice: Voice | None = None,
) -> None:
    """A terminal conversation: typed lines in, streamed replies out, until end of input and every Task is quiet.

    With a voice, replies are also spoken, and the conversation ends once the last one has finished playing.
    """
    _guard_billing(environ, stderr)
    interactive = stdin.isatty()
    output = TerminalOutput(stdout, stderr, prompt=interactive, show_log=settings.runtime_dir / "show.log")
    metrics = JsonlMetrics(settings.runtime_dir / "metrics.jsonl")
    speech = None
    if voice is not None:
        speech = Speech(voice.tts, voice.player, clock, metrics=metrics, notice=output.notice)
    hub = Hub(
        talker=talker_spec(settings),
        orchestrator=orchestrator_spec(settings),
        backend=backend,
        clock=clock,
        output=output,
        metrics=metrics,
        speech=speech,
    )
    try:
        await hub.start()
        await asyncio.to_thread(language.warm_up)
        if speech is not None:
            speech.start()
            speech.say("startup")
        if interactive:
            stdout.write(PROMPT)
            stdout.flush()
        await hub.run(_typed_lines(stdin, clock))
        if speech is not None:
            await speech.idle()
    finally:
        await hub.close()
        if speech is not None:
            await speech.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-hub", description="Run the Jarvis Hub.")
    parser.add_argument("--text", action="store_true", help="talk by typing, with no audio")
    parser.add_argument("--speak", action="store_true", help="with --text, also speak the replies aloud")
    args = parser.parse_args()

    if not args.text:
        sys.exit("jarvis-hub: only text mode exists so far; run `jarvis-hub --text`.")
    voice = None
    if args.speak:
        tts = KokoroSynthesizer()
        voice = Voice(tts, SoundPlayer(tts.sample_rate))
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
                voice=voice,
            )
        )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.exit(130)
