import subprocess
from pathlib import Path

import pytest

from jarvis_hub.config import ConfigError, load_settings, parse_env


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
    ],
)
def test_parse_env(text: str, expected: dict[str, str]) -> None:
    assert parse_env(text) == expected


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


def test_process_environment_overrides_dotenv(checkout: Path) -> None:
    (checkout / ".env").write_text("JARVIS_TALKER_MODEL=sonnet\nJARVIS_USER_NAME=Ada\n")
    settings = load_settings(checkout, environ={"JARVIS_TALKER_MODEL": "haiku"})
    assert settings.talker.model == "haiku"
    assert settings.user_name == "Ada"


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
