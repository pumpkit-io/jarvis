"""Settings from `.env` in the main checkout, with the process environment taking precedence."""

import math
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard, TypeVar, get_args

DEFAULT_ORG = "pumpkit-io"
# Of the Talker's context window: a long conversation keeps its speed by restarting well before it fills.
DEFAULT_TALKER_RESTART_AT = 0.40

Effort = Literal["low", "medium", "high", "xhigh", "max"]
_Choice = TypeVar("_Choice", bound=str)
# Lowest first, so a level's index orders it.
EFFORTS: tuple[Effort, ...] = get_args(Effort)

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_QUOTED = re.compile(r"^\"([^\"]*)\"|^'([^']*)'")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RoleModel:
    model: str
    effort: Effort
    thinking: bool = False


@dataclass(frozen=True)
class VoiceSettings:
    """Voice mode's windows, engines and thresholds; Phase 0 measured the defaults (docs/notes/phase-0-spikes.md)."""

    follow_up_s: float = 6.0
    announce_follow_up_s: float = 4.0
    listen_s: float = 6.0
    wake_engine: Literal["openwakeword"] = "openwakeword"
    wake_threshold: float = 0.5
    stt_engine: Literal["parakeet"] = "parakeet"
    aec: Literal["webrtc", "off"] = "webrtc"
    vad_threshold: float = 0.5
    end_silence_ms: int = 500
    end_extend_ms: int = 1500
    smart_turn_threshold: float = 0.5
    preroll_ms: int = 1500
    max_turn_s: float = 30.0
    chime_guard_ms: int = 500
    wake_refractory_ms: int = 2000
    onset_pad_ms: int = 200


@dataclass(frozen=True)
class Settings:
    user_name: str | None
    org: str
    org_dir: Path
    repo_dir: Path
    runtime_dir: Path
    talker: RoleModel
    orchestrator: RoleModel
    voice: VoiceSettings = VoiceSettings()
    talker_restart_at: float = DEFAULT_TALKER_RESTART_AT
    # No Subagent runs above this effort, whatever the User or the Orchestrator asks.
    max_effort: Effort = "max"
    max_subagents: int = 3


def parse_env(data: bytes) -> dict[str, str]:
    """Read `NAME=value` lines without running anything: no expansion, no substitution.

    Quoted values end at the closing quote; unquoted ones lose a ` #` comment and trailing spaces.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConfigError(f".env is not UTF-8 text ({e.reason} at byte {e.start})") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        name, value = match.groups()
        quoted = _QUOTED.match(value)
        if quoted:
            value = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
        else:
            value = "" if value.startswith("#") else re.sub(r"\s#.*$", "", value).rstrip()
        values[name] = value
    return values


def is_effort(value: str) -> TypeGuard[Effort]:
    return value in get_args(Effort)


def main_checkout(repo_dir: Path) -> Path:
    """The main checkout holds `.env` and sits in the Org folder; a worktree's own path would hide both."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return repo_dir.resolve()
    return Path(common).resolve().parent


def load_settings(
    repo_dir: Path, environ: Mapping[str, str], *, ignore_voice: Callable[[str], None] | None = None
) -> Settings:
    """With `ignore_voice` (text mode), voice settings stay at their defaults and a bad one is reported to it."""
    repo_dir = repo_dir.resolve()
    main = main_checkout(repo_dir)
    env_file = main / ".env"
    values = parse_env(env_file.read_bytes()) if env_file.is_file() else {}
    values.update({k: v for k, v in environ.items() if k.startswith("JARVIS_") and v})

    def get(name: str, default: str) -> str:
        return values.get(name) or default

    def effort(name: str, default: Effort) -> Effort:
        value = get(name, default)
        if not is_effort(value):
            raise ConfigError(f"{name}={value!r} is not one of {', '.join(get_args(Effort))}")
        return value

    def number(name: str, default: float, low: float, high: float, *, low_open: bool = False) -> float:
        raw = get(name, "")
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"{name}={raw!r} is not a number") from None
        if value < low or value > high or (low_open and value == low):
            raise ConfigError(f"{name}={raw!r} is out of range ({low:g} to {high:g})")
        return value

    def choice(name: str, default: _Choice, allowed: tuple[_Choice, ...]) -> _Choice:
        value = get(name, default)
        for option in allowed:
            if value == option:
                return option
        raise ConfigError(f"{name}={value!r} is not one of {', '.join(allowed)}")

    def voice_settings() -> VoiceSettings:
        return VoiceSettings(
            follow_up_s=number("JARVIS_FOLLOWUP_SECONDS", 6.0, 0, 120, low_open=True),
            announce_follow_up_s=number("JARVIS_ANNOUNCE_FOLLOWUP_SECONDS", 4.0, 0, 120, low_open=True),
            listen_s=number("JARVIS_LISTEN_SECONDS", 6.0, 0, 120, low_open=True),
            wake_engine=choice("JARVIS_WAKE_ENGINE", "openwakeword", ("openwakeword",)),
            wake_threshold=number("JARVIS_WAKE_THRESHOLD", 0.5, 0, 1, low_open=True),
            stt_engine=choice("JARVIS_STT_ENGINE", "parakeet", ("parakeet",)),
            aec=choice("JARVIS_AEC", "webrtc", ("webrtc", "off")),
            vad_threshold=number("JARVIS_VAD_THRESHOLD", 0.5, 0, 1, low_open=True),
            end_silence_ms=round(number("JARVIS_END_SILENCE_MS", 500, 0, 5000, low_open=True)),
            end_extend_ms=round(number("JARVIS_END_EXTEND_MS", 1500, 0, 10000)),
            smart_turn_threshold=number("JARVIS_SMART_TURN_THRESHOLD", 0.5, 0, 1),
            preroll_ms=round(number("JARVIS_PREROLL_MS", 1500, 0, 5000)),
            max_turn_s=number("JARVIS_MAX_TURN_SECONDS", 30.0, 0, 600, low_open=True),
            chime_guard_ms=round(number("JARVIS_CHIME_GUARD_MS", 500, 0, 5000)),
            wake_refractory_ms=round(number("JARVIS_WAKE_REFRACTORY_MS", 2000, 0, 10000)),
            onset_pad_ms=round(number("JARVIS_ONSET_PAD_MS", 200, 0, 1000)),
        )

    if ignore_voice is None:
        voice = voice_settings()
    else:
        try:
            voice_settings()
        except ConfigError as e:
            ignore_voice(str(e))
        voice = VoiceSettings()

    def fraction(name: str, default: float) -> float:
        raw = get(name, str(default))
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not 0 < value <= 1:
            raise ConfigError(f"{name}={raw!r} is not a fraction of the window above 0 and up to 1")
        return value

    def count(name: str, default: int) -> int:
        value = get(name, str(default))
        if not value.isdigit() or int(value) < 1:
            raise ConfigError(f"{name}={value!r} is not a whole number of at least 1")
        return int(value)

    return Settings(
        user_name=values.get("JARVIS_USER_NAME") or None,
        org=get("JARVIS_GITHUB_ORG", DEFAULT_ORG),
        org_dir=main.parent,
        repo_dir=repo_dir,
        runtime_dir=repo_dir / ".jarvis",
        # Sonnet, not the Spec's Haiku: Haiku called tools before speaking (docs/adr/0005).
        talker=RoleModel(model=get("JARVIS_TALKER_MODEL", "sonnet"), effort="low", thinking=False),
        orchestrator=RoleModel(
            model=get("JARVIS_ORCHESTRATOR_MODEL", "sonnet"),
            effort=effort("JARVIS_ORCHESTRATOR_EFFORT", "medium"),
            # Left to the model: the Orchestrator judges results, and its latency is not heard.
            thinking=True,
        ),
        voice=voice,
        talker_restart_at=fraction("JARVIS_TALKER_RESTART_AT", DEFAULT_TALKER_RESTART_AT),
        max_effort=effort("JARVIS_MAX_EFFORT", "max"),
        max_subagents=count("JARVIS_MAX_SUBAGENTS", 3),
    )
