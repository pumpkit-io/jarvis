"""`jarvis-hub` in voice mode, wired as the launcher wires it, with a scripted microphone and a recording player."""

import asyncio
import io
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis_hub.config import RoleModel, Settings, VoiceSettings
from jarvis_hub.main import Ears, timing, voice_mode
from jarvis_hub.voice.engines import EngineChoice
from jarvis_hub.voice.input import Listening
from jarvis_hub.voice.player import Samples
from tests.fakes import FakeBackend, FakeClock, FakeTts, RecordingPlayer, ScriptedAudio, Text, until


class HeldStdin(io.StringIO):
    """A terminal nobody types into, until `release` ends the input."""

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()

    def __next__(self) -> str:
        self.done.wait(timeout=5)
        raise StopIteration


class ScriptedMicrophone(ScriptedAudio):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.started = self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def reference(self, samples: Samples, rate: int) -> None:
        pass


def settings(tmp_path: Path, voice: VoiceSettings = VoiceSettings()) -> Settings:
    return Settings(
        user_name="Ada",
        org="acme",
        org_dir=tmp_path,
        repo_dir=tmp_path / "jarvis",
        runtime_dir=tmp_path / "jarvis" / ".jarvis",
        talker=RoleModel("sonnet", "low", False),
        orchestrator=RoleModel("sonnet", "medium", True),
        voice=voice,
    )


async def test_voice_mode_wakes_hears_answers_aloud_and_idles(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text("Half past nine, sir.")]])
    microphone = ScriptedMicrophone(clock)
    player = RecordingPlayer()
    chosen: list[tuple[EngineChoice, Listening]] = []

    def open_listener(
        choice: EngineChoice, listening: Listening, record: Any, notice: Any, clips: Any
    ) -> ScriptedMicrophone:
        chosen.append((choice, listening))
        return microphone

    def open_player(rate: int, on_played: Callable[[Samples, int], None]) -> RecordingPlayer:
        return player

    stdin, stdout, stderr = HeldStdin(), io.StringIO(), io.StringIO()
    config = settings(tmp_path, VoiceSettings(follow_up_s=3.0, end_silence_ms=400, max_turn_s=20.0, onset_pad_ms=300, bargein_min_speech_ms=150))
    running = asyncio.create_task(
        voice_mode(
            settings=config,
            environ={},
            backend=backend,
            clock=clock,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            ears=Ears(FakeTts(), open_player, open_listener),
            headphones=True,
        )
    )
    await until(lambda: microphone.started and player.texts == ["At your service, sir."])
    microphone.wake()
    await until(lambda: microphone.capturing == [True])
    microphone.say("What time is it?")
    await until(lambda: microphone.capturing == [True, False, True])
    clock.advance(3.0)
    await until(lambda: microphone.capturing == [True, False, True, False])
    stdin.done.set()
    await asyncio.wait_for(running, timeout=5)

    assert stdout.getvalue() == "\r» What time is it?\nHalf past nine, sir.\n"
    assert player.texts == ["At your service, sir.", "", "Half past nine, sir."]
    [(choice, listening)] = chosen
    assert choice.aec == "off"
    assert (listening.end_silence_s, listening.max_turn_s, listening.onset_pad_s) == (0.4, 20.0, 0.3)
    assert listening.bargein_min_speech_s == 0.15
    assert microphone.closed and player.closed
    rows = [json.loads(line) for line in (config.runtime_dir / "metrics.jsonl").read_text().splitlines()]
    assert [r["to"] for r in rows if r["kind"] == "voice"] == ["listening", "thinking", "speaking", "follow_up", "idle"]


def test_the_barge_in_windows_reach_the_voice_state_machine() -> None:
    voice = timing(VoiceSettings(bargein_decide_ms=600, bargein_guard_ms=400))

    assert (voice.decide_s, voice.guard_s) == (0.6, 0.4)
