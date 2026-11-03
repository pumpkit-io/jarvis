"""Settings from `.env` in the main checkout, with the process environment taking precedence."""

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard, get_args

DEFAULT_ORG = "pumpkit-io"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

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
class Settings:
    user_name: str | None
    org: str
    org_dir: Path
    repo_dir: Path
    runtime_dir: Path
    talker: RoleModel
    orchestrator: RoleModel


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


def _is_effort(value: str) -> TypeGuard[Effort]:
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


def load_settings(repo_dir: Path, environ: Mapping[str, str]) -> Settings:
    repo_dir = repo_dir.resolve()
    main = main_checkout(repo_dir)
    env_file = main / ".env"
    values = parse_env(env_file.read_bytes()) if env_file.is_file() else {}
    values.update({k: v for k, v in environ.items() if k.startswith("JARVIS_") and v})

    def get(name: str, default: str) -> str:
        return values.get(name) or default

    def effort(name: str, default: Effort) -> Effort:
        value = get(name, default)
        if not _is_effort(value):
            raise ConfigError(f"{name}={value!r} is not one of {', '.join(get_args(Effort))}")
        return value

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
        ),
    )
