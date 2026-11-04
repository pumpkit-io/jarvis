import subprocess
from pathlib import Path

import pytest

from jarvis_hub.config import ConfigError, VoiceSettings, load_settings, parse_env


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('JARVIS_USER_NAME="Ada Lovelace"\n', {"JARVIS_USER_NAME": "Ada Lovelace"}),
        ("JARVIS_GITHUB_ORG=acme\n", {"JARVIS_GITHUB_ORG": "acme"}),
        ("JARVIS_USER_NAME='Ada'\n", {"JARVIS_USER_NAME": "Ada"}),
        ('export JARVIS_USER_NAME = "Ada" # me\n', {"JARVIS_USER_NAME": "Ada"}),
        ("JARVIS_GITHUB_ORG=acme # my org\n", {"JARVIS_GITHUB_ORG": "acme"}),
        ("JARVIS_USER_NAME=Old Name\nJARVIS_USER_NAME=Ada\n", {"JARVIS_USER_NAME": "Ada"}),
        ("JARVIS_USER_NAME=Ada Lovelace  \n", {"JARVIS_USER_NAME": "Ada Lovelace"}),
        ("JARVIS_USER_NAME=# nobody\n", {"JARVIS_USER_NAME": ""}),
        ("JARVIS_USER_NAME=Ada#1\n", {"JARVIS_USER_NAME": "Ada#1"}),
        ("JARVIS_USER_NAME=Ada\r\n", {"JARVIS_USER_NAME": "Ada"}),
        ("JARVIS_USER_NAME=Ada", {"JARVIS_USER_NAME": "Ada"}),
        ("# Local settings\n\n", {}),
        ("not an assignment\n", {}),
        ("1BAD=x\n", {}),
        ("JARVIS_USER_NAME=$(touch pwned)\n", {"JARVIS_USER_NAME": "$(touch pwned)"}),
        ('JARVIS_USER_NAME="$HOME"\n', {"JARVIS_USER_NAME": "$HOME"}),
        (b"\xef\xbb\xbfJARVIS_USER_NAME=Ada\n", {"JARVIS_USER_NAME": "Ada"}),
        ("JARVIS_USER_NAME=Niccolò\n", {"JARVIS_USER_NAME": "Niccolò"}),
    ],
    ids=[
        "double-quoted value",
        "plain value",
        "single-quoted value",
        "export, spaces and quoted value with comment",
        "inline comment stripped",
        "last assignment wins",
        "trailing spaces trimmed",
        "value that is only a comment",
        "hash inside a word kept",
        "CRLF line ending",
        "no final newline",
        "comments and blank lines",
        "stray line ignored",
        "invalid name ignored",
        "command substitution kept literal",
        "no variable expansion",
        "UTF-8 byte order mark before the first key",
        "UTF-8 value",
    ],
)
def test_parse_env(text: str | bytes, expected: dict[str, str]) -> None:
    assert parse_env(text if isinstance(text, bytes) else text.encode()) == expected


@pytest.mark.parametrize(
    "data",
    [b"JARVIS_USER_NAME=Niccol\xf2\n", b"\xff\xfeJ\x00=\x00"],
    ids=["Latin-1 value", "UTF-16 file"],
)
def test_parse_env_refuses_text_that_is_not_utf8(data: bytes) -> None:
    with pytest.raises(ConfigError, match="not UTF-8"):
        parse_env(data)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "org" / "jarvis"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def test_reads_dotenv_from_the_checkout(checkout: Path) -> None:
    (checkout / ".env").write_text('JARVIS_USER_NAME="Ada Lovelace"\nJARVIS_GITHUB_ORG=acme\n')
    settings = load_settings(checkout, environ={})
    assert settings.user_name == "Ada Lovelace"
    assert settings.org == "acme"


def test_defaults_without_dotenv(checkout: Path) -> None:
    settings = load_settings(checkout, environ={})
    assert settings.user_name is None
    assert settings.org == "pumpkit-io"
    assert settings.org_dir == checkout.parent.resolve()
    assert settings.runtime_dir == checkout.resolve() / ".jarvis"


def test_worktree_reads_dotenv_and_org_folder_of_the_main_checkout(checkout: Path, tmp_path: Path) -> None:
    (checkout / ".env").write_text("JARVIS_USER_NAME=Ada\n")
    worktree = tmp_path / "elsewhere" / "wt"
    git(checkout, "worktree", "add", "-q", str(worktree))
    settings = load_settings(worktree, environ={})
    assert settings.user_name == "Ada"
    assert settings.org_dir == checkout.parent.resolve()
    assert settings.runtime_dir == worktree.resolve() / ".jarvis"


def test_role_models_default_to_the_phase_0_choices(checkout: Path) -> None:
    settings = load_settings(checkout, environ={})
    assert (settings.talker.model, settings.talker.effort, settings.talker.thinking) == ("sonnet", "low", False)
    assert (settings.orchestrator.model, settings.orchestrator.effort) == ("sonnet", "medium")


def test_role_models_come_from_dotenv(checkout: Path) -> None:
    (checkout / ".env").write_text(
        "JARVIS_TALKER_MODEL=haiku\nJARVIS_ORCHESTRATOR_MODEL=opus\nJARVIS_ORCHESTRATOR_EFFORT=high\n"
    )
    settings = load_settings(checkout, environ={})
    assert settings.talker.model == "haiku"
    assert settings.talker.effort == "low"
    assert (settings.orchestrator.model, settings.orchestrator.effort) == ("opus", "high")


@pytest.mark.parametrize(
    ("dotenv", "environ", "expected"),
    [
        ("JARVIS_TALKER_MODEL=sonnet\n", {"JARVIS_TALKER_MODEL": "haiku"}, "haiku"),
        ("JARVIS_TALKER_MODEL=haiku\n", {"JARVIS_TALKER_MODEL": ""}, "haiku"),
        ("", {"JARVIS_TALKER_MODEL": "opus"}, "opus"),
        ("JARVIS_TALKER_MODEL=haiku\n", {"TALKER_MODEL": "opus"}, "haiku"),
    ],
    ids=[
        "environment overrides .env",
        "empty environment value leaves .env alone",
        "environment without .env",
        "only JARVIS_ names are read",
    ],
)
def test_process_environment_over_dotenv(
    checkout: Path, dotenv: str, environ: dict[str, str], expected: str
) -> None:
    (checkout / ".env").write_text(dotenv)
    assert load_settings(checkout, environ=environ).talker.model == expected


def test_a_dotenv_that_is_not_utf8_is_a_config_error(checkout: Path) -> None:
    (checkout / ".env").write_bytes(b"JARVIS_USER_NAME=Niccol\xf2\n")
    with pytest.raises(ConfigError, match="not UTF-8"):
        load_settings(checkout, environ={})


def test_empty_values_fall_back_to_defaults(checkout: Path) -> None:
    (checkout / ".env").write_text("JARVIS_USER_NAME=\nJARVIS_GITHUB_ORG=\nJARVIS_TALKER_MODEL=\n")
    settings = load_settings(checkout, environ={})
    assert settings.user_name is None
    assert settings.org == "pumpkit-io"
    assert settings.talker.model == "sonnet"


def test_unknown_effort_is_refused(checkout: Path) -> None:
    (checkout / ".env").write_text("JARVIS_ORCHESTRATOR_EFFORT=extreme\n")
    with pytest.raises(ConfigError, match="JARVIS_ORCHESTRATOR_EFFORT"):
        load_settings(checkout, environ={})


def test_voice_settings_default_to_the_ticket_and_phase_0(checkout: Path) -> None:
    voice = load_settings(checkout, environ={}).voice
    assert (voice.follow_up_s, voice.announce_follow_up_s, voice.listen_s) == (6.0, 4.0, 6.0)
    assert (voice.wake_engine, voice.wake_threshold, voice.stt_engine, voice.aec) == ("openwakeword", 0.5, "parakeet", "webrtc")
    assert (voice.vad_threshold, voice.end_silence_ms, voice.end_extend_ms) == (0.5, 500, 1500)
    assert (voice.smart_turn_threshold, voice.preroll_ms) == (0.5, 1500)
    assert (voice.max_turn_s, voice.chime_guard_ms, voice.wake_refractory_ms, voice.onset_pad_ms) == (30.0, 500, 2000, 200)
    assert (voice.bargein_min_speech_ms, voice.bargein_decide_ms, voice.bargein_guard_ms) == (200, 800, 250)


def test_voice_settings_come_from_dotenv(checkout: Path) -> None:
    (checkout / ".env").write_text(
        "JARVIS_FOLLOWUP_SECONDS=8\nJARVIS_WAKE_THRESHOLD=0.3\nJARVIS_AEC=off\nJARVIS_END_SILENCE_MS=400\n"
        "JARVIS_END_EXTEND_MS=1000\nJARVIS_SMART_TURN_THRESHOLD=0.6\nJARVIS_VAD_THRESHOLD=0.4\n"
        "JARVIS_PREROLL_MS=1200\nJARVIS_LISTEN_SECONDS=5\nJARVIS_ANNOUNCE_FOLLOWUP_SECONDS=3\n"
        "JARVIS_MAX_TURN_SECONDS=20\nJARVIS_CHIME_GUARD_MS=700\nJARVIS_WAKE_REFRACTORY_MS=1500\nJARVIS_ONSET_PAD_MS=300\n"
        "JARVIS_BARGEIN_MIN_SPEECH_MS=150\nJARVIS_BARGEIN_DECIDE_MS=600\nJARVIS_BARGEIN_GUARD_MS=400\n"
    )
    voice = load_settings(checkout, environ={}).voice
    assert (voice.follow_up_s, voice.announce_follow_up_s, voice.listen_s) == (8.0, 3.0, 5.0)
    assert (voice.wake_threshold, voice.aec, voice.vad_threshold) == (0.3, "off", 0.4)
    assert (voice.end_silence_ms, voice.end_extend_ms, voice.smart_turn_threshold, voice.preroll_ms) == (400, 1000, 0.6, 1200)
    assert (voice.max_turn_s, voice.chime_guard_ms, voice.wake_refractory_ms, voice.onset_pad_ms) == (20.0, 700, 1500, 300)
    assert (voice.bargein_min_speech_ms, voice.bargein_decide_ms, voice.bargein_guard_ms) == (150, 600, 400)


@pytest.mark.parametrize(
    "line",
    [
        "JARVIS_FOLLOWUP_SECONDS=soon",
        "JARVIS_FOLLOWUP_SECONDS=-1",
        "JARVIS_WAKE_THRESHOLD=1.5",
        "JARVIS_END_SILENCE_MS=0",
        "JARVIS_AEC=maybe",
        "JARVIS_STT_ENGINE=whisper",
        "JARVIS_TTS_ENGINE=cloud",
        "JARVIS_WAKE_ENGINE=porcupine",
        "JARVIS_MAX_TURN_SECONDS=0",
    ],
)
def test_voice_settings_out_of_range_are_refused(checkout: Path, line: str) -> None:
    (checkout / ".env").write_text(line + "\n")
    with pytest.raises(ConfigError, match=line.split("=")[0]):
        load_settings(checkout, environ={})


def test_text_mode_warns_about_a_bad_voice_setting_and_starts_on_the_defaults(checkout: Path) -> None:
    (checkout / ".env").write_text("JARVIS_AEC=maybe\nJARVIS_FOLLOWUP_SECONDS=8\nJARVIS_MAX_SUBAGENTS=2\n")
    warnings: list[str] = []
    settings = load_settings(checkout, environ={}, ignore_voice=warnings.append)
    assert settings.voice == VoiceSettings()
    assert settings.max_subagents == 2
    assert len(warnings) == 1 and "JARVIS_AEC" in warnings[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0.40), ("0.5", 0.5), ("0.25", 0.25), ("1", 1.0), (" 0.6 ", 0.6), ("", 0.40)],
    ids=["default", "half", "a quarter", "the whole window", "spaces trimmed", "empty falls back"],
)
def test_talker_restart_threshold(checkout: Path, value: str | None, expected: float) -> None:
    environ = {} if value is None else {"JARVIS_TALKER_RESTART_AT": value}
    assert load_settings(checkout, environ=environ).talker_restart_at == expected


@pytest.mark.parametrize("value", ["0", "-0.2", "1.5", "40%", "abc", "nan", "inf"])
def test_talker_restart_threshold_must_be_a_fraction_of_the_window(checkout: Path, value: str) -> None:
    (checkout / ".env").write_text(f"JARVIS_TALKER_RESTART_AT={value}\n")
    with pytest.raises(ConfigError, match="JARVIS_TALKER_RESTART_AT"):
        load_settings(checkout, environ={})


def test_subagent_limits_default_to_max_effort_and_three_at_once(checkout: Path) -> None:
    settings = load_settings(checkout, environ={})
    assert (settings.max_effort, settings.max_subagents) == ("max", 3)


def test_subagent_limits_come_from_dotenv(checkout: Path) -> None:
    (checkout / ".env").write_text("JARVIS_MAX_EFFORT=high\nJARVIS_MAX_SUBAGENTS=2\n")
    settings = load_settings(checkout, environ={})
    assert (settings.max_effort, settings.max_subagents) == ("high", 2)


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("JARVIS_MAX_EFFORT=ultra", "JARVIS_MAX_EFFORT"),
        ("JARVIS_MAX_SUBAGENTS=0", "JARVIS_MAX_SUBAGENTS"),
        ("JARVIS_MAX_SUBAGENTS=two", "JARVIS_MAX_SUBAGENTS"),
        ("JARVIS_MAX_SUBAGENTS=-1", "JARVIS_MAX_SUBAGENTS"),
    ],
    ids=["unknown effort", "zero Subagents", "a word", "negative"],
)
def test_bad_subagent_limits_are_refused(checkout: Path, line: str, name: str) -> None:
    (checkout / ".env").write_text(line + "\n")
    with pytest.raises(ConfigError, match=name):
        load_settings(checkout, environ={})


def test_audio_clips_are_off_by_default_and_opt_in_with_retention_limits(checkout: Path) -> None:
    off = load_settings(checkout, environ={}).voice
    assert (off.audio_clips, off.clips_max_count, off.clips_max_days, off.clips_max_mb) == (False, 500, 14.0, 500.0)

    (checkout / ".env").write_text("JARVIS_AUDIO_CLIPS=on\nJARVIS_CLIPS_MAX_COUNT=50\nJARVIS_CLIPS_MAX_DAYS=3\nJARVIS_CLIPS_MAX_MB=20\n")
    on = load_settings(checkout, environ={}).voice
    assert (on.audio_clips, on.clips_max_count, on.clips_max_days, on.clips_max_mb) == (True, 50, 3.0, 20.0)


def test_audio_clips_must_be_on_or_off(checkout: Path) -> None:
    with pytest.raises(ConfigError, match="JARVIS_AUDIO_CLIPS"):
        load_settings(checkout, environ={"JARVIS_AUDIO_CLIPS": "yes"})
