"""The real Workspace against scratch git repos and the installed sdlc worktree.sh."""

import subprocess
from pathlib import Path

import pytest

from jarvis_hub.subagents import Refusal
from jarvis_hub.workspace import GitWorkspace, find_worktree_script

SCRIPT = find_worktree_script(Path.home() / "Projects" / "pumpkit-io", Path.home())


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """An Org folder holding one clone, whose origin is a local bare repo with a main branch."""
    origin, seed, org = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "org"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / ".gitignore").write_text(".claude/worktrees/\n.sdlc/\n")
    git(seed, "add", ".gitignore")
    git(seed, "commit", "-qm", "init")
    git(seed, "push", "-q", str(origin), "main")
    org.mkdir()
    git(org, "clone", "-q", str(origin), "pumpkit")
    return org / "pumpkit"


@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        ("someone/pumpkit", "outside the Org"),
        ("..", "not a repo name"),
        ("pumpkit-io/../etc", "outside the Org"),
        ("pump kit", "not a repo name"),
        ("pumpkit", "is not a clone of pumpkit-io/pumpkit"),
    ],
    ids=["another owner", "a parent folder", "a path", "a space", "a clone of something else"],
)
async def test_repo_refuses_what_is_not_an_org_clone(clone: Path, name: str, refusal: str) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    with pytest.raises(Refusal, match=refusal):
        await workspace.repo(name)


async def test_repo_accepts_an_org_clone_by_name_or_full_name(clone: Path) -> None:
    git(clone, "remote", "set-url", "origin", "https://github.com/pumpkit-io/pumpkit.git")
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    assert await workspace.repo("pumpkit") == ("pumpkit", clone)
    assert await workspace.repo("Pumpkit-IO/pumpkit") == ("pumpkit", clone)


@pytest.mark.skipif(SCRIPT is None, reason="the sdlc worktrees script is not installed")
async def test_worktree_makes_a_fresh_locked_branch_and_describe_says_what_is_on_it(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    git(clone, "branch", "fix/typo")

    path, branch = await workspace.worktree(clone, "fix/typo", "jarvis S1 (T1): Fix typo")

    assert branch == "fix/typo-2"
    assert git(path, "branch", "--show-current") == "fix/typo-2"
    assert (path / ".sdlc" / "lock").read_text().splitlines()[1] == "jarvis S1 (T1): Fix typo"
    (path / "README.md").write_text("fixed\n")
    git(path, "add", "README.md")
    git(path, "commit", "-qm", "fix: the typo")
    (path / "notes.txt").write_text("draft\n")
    await workspace.release(path)
    assert not (path / ".sdlc" / "lock").exists()
    described = await workspace.describe(path)
    assert "Commits not on origin/main:" in described and "fix: the typo" in described
    assert "?? notes.txt" in described and "Not pushed." in described
