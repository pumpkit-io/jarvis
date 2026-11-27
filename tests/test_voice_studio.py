import argparse
import io
import json
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pytest

from jarvis_hub.config import VoiceSettings
from jarvis_hub.voice.packs import VoicePackError, find_pack, load_pack
from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.studio import Studio, add_arguments, set_env
from jarvis_hub.voice.tts import ClonedVoice

RATE = 24_000


def tone(amplitude: float, seconds: float) -> Samples:
    t = np.arange(round(RATE * seconds)) / RATE
    return (amplitude * np.sin(2 * np.pi * 997 * t)).astype(np.float32)


class FakeDesigner:
    sample_rate = RATE

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def design(self, text: str, language: str, instruct: str) -> Samples:
        self.calls.append((text, language, instruct))
        return tone(0.3, 1.0)


class FakeCloner:
    """Every clone is a 997 Hz tone at amplitude 0.1, which reads -23.01 LUFS, so it needs a -2.0 dB trim."""

    sample_rate = RATE

    def __init__(self) -> None:
        self.voices: list[ClonedVoice] = []
        self.clips: list[bytes] = []

    def stream(self, text: str, voice: ClonedVoice) -> Iterable[Samples]:
        self.voices.append(voice)
        self.clips.append(voice.clip.read_bytes())
        audio = tone(0.1, 3.0)
        yield audio[:RATE]
        yield audio[RATE:]


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.user_voices = tmp_path / ".jarvis" / "voices"
        self.takes = tmp_path / ".jarvis" / "voice-takes"
        self.env = tmp_path / ".env"
        self.designer = FakeDesigner()
        self.cloner = FakeCloner()
        self.played: list[Samples] = []
        self.out = io.StringIO()
        self.voice = VoiceSettings(user_voices=self.user_voices)
        self.environ: dict[str, str] = {}

    def run(self, *argv: str) -> str:
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        studio = Studio(self.voice, self.takes, self.env, self.out, lambda: self.designer, lambda: self.cloner,
                        lambda samples, rate: self.played.append(samples), environ=self.environ,
                        stamp=lambda: "20261127-2200")
        studio.run(parser.parse_args(list(argv)))
        return self.out.getvalue()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_design_saves_and_plays_each_take_reading_the_reference_in_a_native_accent(rig: Rig) -> None:
    output = rig.run("design", "--lang", "it", "--takes", "3", "--describe", "A warm, low voice.")

    takes = sorted(rig.takes.glob("*.wav"))
    assert [t.name for t in takes] == [f"take_20261127-2200_it_0{i}.wav" for i in (1, 2, 3)]
    assert len(rig.played) == 3
    text, language, instruct = rig.designer.calls[0]
    assert language == "Italian" and "Certo." in text
    assert instruct.startswith("A warm, low voice.") and "Italian" in instruct
    sidecar = json.loads(takes[0].with_suffix(".json").read_text())
    assert sidecar["language"] == "it" and sidecar["transcript"] == text and sidecar["description"] == instruct
    assert "voice try" in output and "voice use" in output


def test_design_can_read_a_reference_of_your_own_and_stay_silent(rig: Rig) -> None:
    rig.run("design", "--lang", "en", "--takes", "1", "--text", "Very good, sir. Shall I proceed?", "--no-play")

    [take] = rig.takes.glob("*.wav")
    assert json.loads(take.with_suffix(".json").read_text())["transcript"] == "Very good, sir. Shall I proceed?"
    assert rig.designer.calls[0][2].startswith("A calm, composed")
    assert rig.played == []


def test_use_makes_a_pack_at_the_measured_trim_and_chooses_it_for_its_language(rig: Rig) -> None:
    rig.env.write_text("JARVIS_USER_NAME=Ada\nJARVIS_VOICE_EN=old-voice\nJARVIS_AEC=off\n")
    rig.run("design", "--lang", "en", "--takes", "1", "--no-play")
    [take] = rig.takes.glob("*.wav")

    output = rig.run("use", str(take), "--name", "my-butler")

    pack = find_pack("my-butler", rig.user_voices)
    assert (pack.language, pack.trim_db) == ("en", -2.0)
    assert pack.transcript == json.loads(take.with_suffix(".json").read_text())["transcript"]
    assert pack.clip.read_bytes() == take.read_bytes()
    assert rig.env.read_text() == "JARVIS_USER_NAME=Ada\nJARVIS_VOICE_EN=my-butler\nJARVIS_AEC=off\n"
    assert "JARVIS_VOICE_EN=my-butler" in output and "restart" in output.lower()


def test_add_makes_a_pack_from_your_own_recording(rig: Rig, tmp_path: Path) -> None:
    recording = tmp_path / "me.wav"
    recording.write_bytes(b"RIFF mine")

    rig.run("add", str(recording), "--lang", "it", "--transcript", "Buonasera a tutti.", "--name", "la-mia-voce")

    pack = load_pack(rig.user_voices / "la-mia-voce")
    assert (pack.language, pack.transcript, pack.trim_db) == ("it", "Buonasera a tutti.", -2.0)
    assert rig.env.read_text() == "JARVIS_VOICE_IT=la-mia-voce\n"


def test_use_on_a_clip_without_a_take_record_points_to_add(rig: Rig, tmp_path: Path) -> None:
    clip = tmp_path / "random.wav"
    clip.write_bytes(b"RIFF")

    with pytest.raises(VoicePackError, match="voice add"):
        rig.run("use", str(clip), "--name", "random")


def test_a_taken_name_is_refused_before_anything_changes(rig: Rig) -> None:
    rig.env.write_text("JARVIS_VOICE_EN=jarvis-en\n")
    rig.run("design", "--lang", "en", "--takes", "1", "--no-play")
    [take] = rig.takes.glob("*.wav")

    with pytest.raises(VoicePackError, match="already exists"):
        rig.run("use", str(take), "--name", "jarvis-it")
    assert rig.env.read_text() == "JARVIS_VOICE_EN=jarvis-en\n"
    assert rig.cloner.voices == []


def test_try_plays_a_take_at_the_level_jarvis_would_speak_it(rig: Rig) -> None:
    rig.run("design", "--lang", "en", "--takes", "1", "--no-play")
    [take] = rig.takes.glob("*.wav")

    output = rig.run("try", str(take))

    assert len(rig.played) >= 3
    # The fake clone's 0.1 tone reads -23.01 LUFS, so -25 LUFS needs it 1.99 dB down.
    assert float(np.max(np.abs(np.concatenate(rig.played)))) == pytest.approx(0.1 * 10 ** (-1.99 / 20), rel=0.01)
    assert rig.cloner.clips[0] == take.read_bytes()
    assert "-2.0 dB" in output and f"voice use {take}" in output


def test_try_plays_a_saved_voice_by_name_with_its_own_trim(rig: Rig) -> None:
    output = rig.run("try", "jarvis-it")

    assert rig.cloner.voices[0].language == "Italian" and rig.cloner.voices[0].trim_db == -4.3
    assert float(np.max(np.abs(np.concatenate(rig.played)))) == pytest.approx(0.1 * 10 ** (-4.3 / 20), rel=0.01)
    assert "JARVIS_VOICE_IT=jarvis-it" in output


def test_list_marks_the_voice_each_language_speaks_and_why_a_broken_one_cannot_be_used(rig: Rig) -> None:
    (rig.user_voices / "broken").mkdir(parents=True)
    rig.voice = VoiceSettings(voice_it="jarvis-it", user_voices=rig.user_voices)

    lines = rig.run("list").splitlines()

    assert any("jarvis-en" in line and "*" in line and "en" in line for line in lines)
    assert any("broken" in line and "voice.toml" in line for line in lines)
    assert any("voice try" in line for line in lines) and any("voice design" in line for line in lines)


def test_use_warns_when_the_shell_overrides_the_setting_it_writes(rig: Rig) -> None:
    rig.environ["JARVIS_VOICE_EN"] = "from-shell"
    rig.run("design", "--lang", "en", "--takes", "1", "--no-play")
    [take] = rig.takes.glob("*.wav")

    output = rig.run("use", str(take), "--name", "my-butler")

    assert "JARVIS_VOICE_EN=from-shell" in output and "unset" in output


@pytest.mark.parametrize("record", ["[]", "not json", '{"language": "en"}'])
def test_a_malformed_take_record_is_refused_cleanly(rig: Rig, tmp_path: Path, record: str) -> None:
    clip = tmp_path / "take.wav"
    clip.write_bytes(b"RIFF")
    clip.with_suffix(".json").write_text(record)

    with pytest.raises(VoicePackError, match="take record"):
        rig.run("use", str(clip), "--name", "taken")


def test_set_env_replaces_every_assignment_keeps_comments_and_appends_when_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# JARVIS_VOICE_EN=commented\nexport JARVIS_VOICE_EN = a\nJARVIS_X=1\nJARVIS_VOICE_EN=b")

    set_env(env, "JARVIS_VOICE_EN", "c")
    set_env(env, "JARVIS_VOICE_IT", "d")

    assert env.read_text() == "# JARVIS_VOICE_EN=commented\nJARVIS_VOICE_EN=c\nJARVIS_X=1\nJARVIS_VOICE_IT=d\n"


def test_set_env_keeps_the_files_permissions(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("JARVIS_X=1\n")
    env.chmod(0o600)

    set_env(env, "JARVIS_VOICE_EN", "c")

    assert env.stat().st_mode & 0o777 == 0o600


def test_set_env_writes_through_a_symlinked_env_to_its_target(tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "jarvis.env"
    target.parent.mkdir()
    target.write_text("JARVIS_X=1\n")
    env = tmp_path / ".env"
    env.symlink_to(target)

    set_env(env, "JARVIS_VOICE_EN", "c")

    assert env.is_symlink() and target.read_text() == "JARVIS_X=1\nJARVIS_VOICE_EN=c\n"


def test_an_empty_shell_variable_is_not_reported_as_an_override(rig: Rig) -> None:
    rig.environ["JARVIS_VOICE_EN"] = ""
    rig.run("design", "--lang", "en", "--takes", "1", "--no-play")
    [take] = rig.takes.glob("*.wav")

    assert "overrides" not in rig.run("use", str(take), "--name", "my-butler")
