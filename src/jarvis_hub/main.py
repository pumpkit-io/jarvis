"""`jarvis-hub`: wiring, startup checks and the text-mode terminal adapters."""

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO
from uuid import uuid4

from rich.console import Console
from rich.markdown import Markdown

from jarvis_hub.agents.backend import AgentBackend, SessionSpec
from jarvis_hub.agents.sdk import SdkBackend
from jarvis_hub.config import ConfigError, Settings, VoiceSettings, load_settings
from jarvis_hub.hub import Clock, Hub, Line, Metrics, Output
from jarvis_hub.orchestrator import orchestrator_spec
from jarvis_hub.subagents import Subagent, SubagentSetup, subagent_session
from jarvis_hub.stats import parse_rows, parse_when, read_rows, render, render_bargein, render_voice_trace, summarise, summarise_bargein
from jarvis_hub.talker import talker_spec
from jarvis_hub.voice import language
from jarvis_hub.voice.clips import ClipSettings, ClipStore
from jarvis_hub.voice.engines import EngineChoice
from jarvis_hub.voice.input import Listening
from jarvis_hub.voice.player import Player, Samples, SoundPlayer
from jarvis_hub.voice.speech import Speech, SpeechClock
from jarvis_hub.voice.state import Timing
from jarvis_hub.voice.tts import KokoroSynthesizer, Synthesizer
from jarvis_hub.voice.admission import configured_assessor
from jarvis_hub.voice.voice_mode import AudioInput, VoiceMode, merge
from jarvis_hub.waits import WaitSetup
from jarvis_hub.workspace import default_workspace

PROMPT = "› "
# Marks what the User said aloud, as transcribed, apart from what they typed.
SAID = "» "

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
class Mouth:
    """What `--speak` adds: a synthesiser, and how to open a player at its sample rate."""

    tts: Synthesizer
    open_player: Callable[[int], Player]


PlayedTap = Callable[[Samples, int], None]


class Listener(AudioInput, Protocol):
    def start(self) -> None: ...

    def close(self) -> None: ...

    def reference(self, samples: Samples, rate: int) -> None:
        """What the speakers played, for echo cancellation."""
        ...


@dataclass(frozen=True)
class Ears:
    """What voice mode adds to `--speak`: the listener, and a player that reports what it plays to it."""

    tts: Synthesizer
    open_player: Callable[[int, PlayedTap], Player]
    open_listener: Callable[
        [EngineChoice, Listening, Callable[[dict[str, Any]], None], Callable[[str], None], ClipStore], Listener
    ]


def timing(voice: VoiceSettings) -> Timing:
    return Timing(
        follow_up_s=voice.follow_up_s,
        announce_follow_up_s=voice.announce_follow_up_s,
        listen_s=voice.listen_s,
        chime_s=voice.chime_guard_ms / 1000,
    )


def clip_settings(voice: VoiceSettings) -> ClipSettings:
    return ClipSettings(voice.audio_clips, voice.clips_max_count, voice.clips_max_days, voice.clips_max_mb)


def listening(voice: VoiceSettings) -> Listening:
    return Listening(
        wake_threshold=voice.wake_threshold,
        vad_threshold=voice.vad_threshold,
        end_silence_s=voice.end_silence_ms / 1000,
        end_extend_s=voice.end_extend_ms / 1000,
        smart_turn_threshold=voice.smart_turn_threshold,
        preroll_s=voice.preroll_ms / 1000,
        max_turn_s=voice.max_turn_s,
        wake_refractory_s=voice.wake_refractory_ms / 1000,
        onset_pad_s=voice.onset_pad_ms / 1000,
    )


class JsonlMetrics:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._run_id = uuid4().hex

    def record(self, entry: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "run_id": self._run_id, **entry}
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

    def heard(self, text: str, *, typed: bool = True) -> None:
        # A terminal already shows what was typed; piped input is echoed so the transcript reads in order.
        if not typed:
            self._stdout.write(f"\r{SAID}{text}\n")
            self._stdout.flush()
        elif not self._prompt:
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


def subagent_setup(settings: Settings) -> SubagentSetup:
    def session(subagent: Subagent) -> SessionSpec:
        return subagent_session(settings, subagent)

    return SubagentSetup(
        session=session,
        workspace=default_workspace(settings.org, settings.org_dir),
        log_dir=settings.runtime_dir / "subagents",
        max_effort=settings.max_effort,
        cap=settings.max_subagents,
    )


def settings_hub(
    settings: Settings, *, backend: AgentBackend, clock: Clock, output: Output, metrics: Metrics, **options: Any
) -> Hub:
    """A Hub with everything the settings decide filled in; `options` are what differs between the modes."""
    return Hub(
        talker=talker_spec(settings),
        orchestrator=orchestrator_spec(settings),
        backend=backend,
        clock=clock,
        output=output,
        metrics=metrics,
        subagents=subagent_setup(settings),
        waits=WaitSetup(org=settings.org),
        talker_restart_at=settings.talker_restart_at,
        state_path=settings.runtime_dir / "state.json",
        **options,
    )


async def text_mode(
    *,
    settings: Settings,
    environ: MutableMapping[str, str],
    backend: AgentBackend,
    clock: SpeechClock,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    voice: Mouth | None = None,
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
        try:
            player = voice.open_player(voice.tts.sample_rate)
        except Exception as e:
            output.notice(f"Speech is off: no audio output: {type(e).__name__}: {e}")
        else:
            speech = Speech(
                voice.tts, player, clock, metrics=metrics, notice=output.notice,
                on_ready=lambda: output.notice("Speech ready."),
            )
    hub = settings_hub(
        settings, backend=backend, clock=clock, output=output, metrics=metrics, speech=speech,
        on_model_ready=lambda: output.notice("Model ready."),
    )
    try:
        output.notice("Model loading.")
        await hub.start()
        await asyncio.to_thread(language.warm_up)
        if speech is not None:
            output.notice("Speech loading.")
            speech.start()
            speech.say("ready", "en")
        if interactive:
            stdout.write(PROMPT)
            stdout.flush()
        await hub.run(_typed_lines(stdin, clock))
        if speech is not None:
            await speech.idle()
    finally:
        try:
            await hub.close()
        finally:
            if speech is not None:
                await speech.close()


async def voice_mode(
    *,
    settings: Settings,
    environ: MutableMapping[str, str],
    backend: AgentBackend,
    clock: SpeechClock,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    ears: Ears,
    headphones: bool = False,
    debug: bool = False,
) -> None:
    """Idle until the Wake word, then a spoken conversation; lines typed meanwhile get text-only replies.

    Ends with the typed input (Ctrl-D) or Ctrl-C.
    """
    _guard_billing(environ, stderr)
    output = TerminalOutput(stdout, stderr, prompt=stdin.isatty(), show_log=settings.runtime_dir / "show.log")
    metrics = JsonlMetrics(settings.runtime_dir / "metrics.jsonl")
    voice = settings.voice
    choice = EngineChoice(aec="off" if headphones else voice.aec, wake=voice.wake_engine, stt=voice.stt_engine)
    clips = ClipStore(settings.runtime_dir / "clips", clip_settings(voice))
    listener = ears.open_listener(choice, listening(voice), metrics.record, output.notice, clips)
    try:
        player = ears.open_player(ears.tts.sample_rate, listener.reference)
    except Exception as e:
        output.notice(f"Voice mode needs an audio output: {type(e).__name__}: {e}. Run `jarvis-hub --text` instead.")
        return
    admission = configured_assessor(voice.admission_backend, voice.admission_model,
                                    settings.repo_dir / "prompts" / "admission.md", notice=output.notice)
    conversation = VoiceMode(
        audio=listener,
        tts=ears.tts,
        player=player,
        clock=clock,
        timing=timing(voice),
        metrics=metrics,
        notice=output.notice,
        debug=debug,
        recognition=admission,
    )
    hub = settings_hub(
        settings,
        backend=backend,
        clock=clock,
        output=output,
        metrics=metrics,
        speech=conversation.speech,
        speak_typed=False,
        on_end_conversation=conversation.end_conversation,
        floor=conversation,
        preconnect_orchestrator=True,
        on_model_ready=lambda: output.notice("Model ready."),
    )
    try:
        output.notice("Model loading.")
        if admission is not None:
            admission.start()
        await hub.start()
        await asyncio.to_thread(language.warm_up)
        conversation.start()
        output.notice("Listening loading.")
        listener.start()
        if stdin.isatty():
            stdout.write(PROMPT)
            stdout.flush()
        await hub.run(merge(_typed_lines(stdin, clock), conversation.lines(hub.interrupt, hub.woke, hub.admit_spoken_revision)))
    finally:
        try:
            await hub.close()
        finally:
            listener.close()
            await conversation.close()


def _open_listener(
    choice: EngineChoice,
    settings: Listening,
    record: Callable[[dict[str, Any]], None],
    notice: Callable[[str], None],
    clips: ClipStore,
) -> Listener:
    from jarvis_hub.voice.engines import load_engines
    from jarvis_hub.voice.input import Capture

    return Capture(lambda: load_engines(choice), settings, record, notice, clips=clips)


def _open_player(rate: int, on_played: PlayedTap) -> Player:
    return SoundPlayer(rate, on_played=on_played)


def _ignored_in_text(error: str) -> None:
    sys.stderr.write(f"jarvis-hub: {error}; voice settings are not used with --text, so it is ignored.\n")


def print_stats(
    settings: Settings, since: str | None, until: str | None, stdout: TextIO, *, bargein: bool = False, voice: bool = False,
    latency_plan: Path | None = None, latency_observations: Path | None = None, metrics_path: Path | None = None,
) -> None:
    if latency_plan is not None or latency_observations is not None:
        if latency_plan is None or latency_observations is None or since or until or voice or bargein:
            raise ValueError("Controlled latency needs both plan and observations, without time filters or other reports.")
        from jarvis_hub.latency import latency_report
        path = metrics_path or settings.runtime_dir / "metrics.jsonl"
        try:
            plan_bytes, observation_bytes = latency_plan.read_bytes(), latency_observations.read_bytes()
            digest = hashlib.sha256(plan_bytes).hexdigest()
            metric_bytes = path.read_bytes() if path.exists() else None
            snapshot = "" if metric_bytes is None else metric_bytes.decode("utf-8")
            report = latency_report(json.loads(plan_bytes), json.loads(observation_bytes), parse_rows(snapshot), plan_sha256=digest)
            report["input_hashes"] = dict(plan=digest, observations=hashlib.sha256(observation_bytes).hexdigest(),
                                          metrics=hashlib.sha256(metric_bytes).hexdigest() if metric_bytes is not None else None)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("Controlled latency input files are missing, unreadable or malformed JSON.") from None
        stdout.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
        return
    if metrics_path is not None:
        raise ValueError("A metrics override requires the controlled latency plan and observations.")
    now = datetime.now(timezone.utc)
    start = parse_when(since, now) if since else None
    end = parse_when(until, now, end=True) if until else None
    rows = read_rows(settings.runtime_dir / "metrics.jsonl")
    if voice:
        stdout.write(render_voice_trace(rows, start, end) + "\n")
        return
    if bargein:
        stdout.write(render_bargein(summarise_bargein(rows, start, end), start, end) + "\n")
        return
    stdout.write(render(summarise(rows, start, end), start, end) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis-hub", description="Run the Jarvis Hub.")
    parser.add_argument("--text", action="store_true", help="talk by typing, with no audio")
    parser.add_argument("--speak", action="store_true", help="with --text, speak generated replies; readiness is nonverbal")
    parser.add_argument("--headphones", action="store_true", help="voice mode with echo cancellation off")
    parser.add_argument("--debug", action="store_true", help="show the voice state machine's transitions")
    commands = parser.add_subparsers(dest="command", metavar="stats")
    stats = commands.add_parser("stats", help="print usage and latency per role from the metrics log")
    stats.add_argument("--since", help="start of the range: a time ago like 24h or 7d, or a date like 2026-11-01")
    stats.add_argument(
        "--until", help="end of the range, written like --since (a bare date takes in that whole day); default now"
    )
    stats.add_argument("--latency-plan", type=Path, help="frozen controlled-sample JSON plan; outputs an offline JSON report")
    stats.add_argument("--latency-observations", type=Path, help="hash-bound sample bindings and reviewed physical observations")
    stats.add_argument("--metrics", type=Path, help="metrics JSONL for the controlled latency report")
    stats.add_argument("--voice", action="store_true", help="trace captured requests, recognition and meaningful audio")
    stats.add_argument("--bargein", action="store_true", help="report the Barge-in decision log instead")
    args = parser.parse_args()

    if args.command == "stats":
        try:
            settings = load_settings(Path(__file__).resolve().parents[2], os.environ, ignore_voice=lambda error: None)
            print_stats(settings, args.since, args.until, sys.stdout, bargein=args.bargein, voice=args.voice,
                        latency_plan=args.latency_plan, latency_observations=args.latency_observations, metrics_path=args.metrics)
        except (ConfigError, ValueError) as e:
            sys.exit(f"jarvis-hub: {e}")
        return

    voice = Mouth(KokoroSynthesizer(), SoundPlayer) if args.speak else None
    cli = shutil.which("claude")
    if cli is None:
        sys.exit("jarvis-hub: the claude CLI is not on PATH; install Claude Code and log in with your Max plan.")
    try:
        settings = load_settings(
            Path(__file__).resolve().parents[2], os.environ, ignore_voice=_ignored_in_text if args.text else None
        )
    except ConfigError as e:
        sys.exit(f"jarvis-hub: {e}")

    # Ctrl-C reaches the claude CLI too (same process group), so it ends the conversation rather than a reply.
    try:
        if args.text:
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
        else:
            asyncio.run(
                voice_mode(
                    settings=settings,
                    environ=os.environ,
                    backend=SdkBackend(cli),
                    clock=MonotonicClock(),
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    ears=Ears(KokoroSynthesizer(), _open_player, _open_listener),
                    headphones=args.headphones,
                    debug=args.debug,
                )
            )
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.exit(130)
