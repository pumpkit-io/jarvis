"""The real Workspace against scratch git repos and the installed sdlc worktree.sh."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from jarvis_hub import workspace as workspace_module
from jarvis_hub.config import load_settings
from jarvis_hub.subagents import Refusal, Subagent, Subagents, SubagentSetup
from jarvis_hub.workspace import GitWorkspace, find_worktree_script, is_org_remote, lock_key
from tests.fakes import FakeBackend, FakeClock, subagent_spec

SCRIPT = find_worktree_script(load_settings(Path(__file__).resolve().parents[1], {}).org_dir, Path.home())
needs_script = pytest.mark.skipif(SCRIPT is None, reason="the sdlc worktrees script is not installed")


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
    ("url", "matches"),
    [
        ("https://github.com/pumpkit-io/pumpkit.git", True),
        ("https://github.com/pumpkit-io/pumpkit", True),
        ("git@github.com:pumpkit-io/pumpkit.git", True),
        ("ssh://git@github.com/Pumpkit-IO/pumpkit.git", True),
        ("https://evil/x/github.com/pumpkit-io/pumpkit", False),
        ("https://github.com.evil.com/pumpkit-io/pumpkit", False),
        ("https://github.com/someone/pumpkit-io/pumpkit", False),
        ("https://github.com/pumpkit-io/pumpkit-ops", False),
        ("https://github.com/pumpkit-io/pumpkit.git\nhttps://evil", False),
    ],
    ids=lambda v: str(v),
)
def test_is_org_remote_takes_only_githubs_own_url_for_the_repo(url: str, matches: bool) -> None:
    assert is_org_remote(url, "pumpkit-io", "pumpkit") == matches


async def test_repo_refuses_a_clone_that_is_a_symlink_out_of_the_org_folder(clone: Path, tmp_path: Path) -> None:
    git(clone, "remote", "set-url", "origin", "https://github.com/pumpkit-io/pumpkit.git")
    elsewhere = tmp_path / "elsewhere"
    clone.rename(elsewhere)
    clone.symlink_to(elsewhere)
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    with pytest.raises(Refusal, match="leads outside the Org folder"):
        await workspace.repo("pumpkit")


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


@needs_script
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
    assert await workspace.release(path, "jarvis S1 (T1): Fix typo") is None
    assert not (path / ".sdlc" / "lock").exists()
    described = await workspace.describe(path)
    assert "Commits not on origin/main:" in described and "fix: the typo" in described
    assert "?? notes.txt" in described and "Not pushed." in described


@needs_script
async def test_describe_counts_commits_beyond_the_pushed_branch_without_an_upstream(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    path, branch = await workspace.worktree(clone, "fix/typo", "jarvis S1 (T1): Fix typo")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-qm", "fix: one")
    git(path, "push", "-q", "origin", branch)
    assert "Pushed to origin/fix/typo." in await workspace.describe(path)
    (path / "b.txt").write_text("b\n")
    git(path, "add", "b.txt")
    git(path, "commit", "-qm", "fix: two")
    assert "Pushed to origin/fix/typo, with 1 commit not pushed." in await workspace.describe(path)


@needs_script
async def test_two_worktrees_asked_at_once_get_different_branches(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    (one, a), (two, b) = await asyncio.gather(
        workspace.worktree(clone, "fix/typo", "jarvis S1 (T1): Fix typo"),
        workspace.worktree(clone, "fix/typo", "jarvis S2 (T1): Fix typo"),
    )
    assert {a, b} == {"fix/typo", "fix/typo-2"} and one != two
    holders = {(path / ".sdlc" / "lock").read_text().splitlines()[1] for path in (one, two)}
    assert holders == {"jarvis S1 (T1): Fix typo", "jarvis S2 (T1): Fix typo"}


@needs_script
async def test_a_worktree_that_cannot_be_locked_is_removed_with_its_new_branch(clone: Path, tmp_path: Path) -> None:
    assert SCRIPT is not None
    failing = tmp_path / "worktree.sh"
    failing.write_text(f'[ "$1" = lock ] && {{ echo "no lock today" >&2; exit 1; }}\nexec bash {SCRIPT} "$@"\n')
    workspace = GitWorkspace("pumpkit-io", clone.parent, failing)
    with pytest.raises(Refusal, match="no lock today.*both empty, are removed"):
        await workspace.worktree(clone, "fix/typo", "jarvis S1 (T1): Fix typo")
    assert git(clone, "branch", "--list", "fix/typo") == ""
    assert not (clone / ".claude" / "worktrees" / "typo").exists()


@needs_script
async def test_a_read_only_worktree_is_detached_locked_and_removed_by_discard(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    git(clone, "checkout", "-qb", "users-own-work")
    (clone / "wip.txt").write_text("the User's work\n")

    path = await workspace.scratch(clone, "read-what-changed", "jarvis S1 (T1): What changed")

    assert path == clone / ".claude" / "worktrees" / "read-what-changed"
    assert git(path, "branch", "--show-current") == ""
    assert git(path, "rev-parse", "HEAD") == git(clone, "rev-parse", "origin/main")
    assert (path / ".sdlc" / "lock").read_text().splitlines()[1] == "jarvis S1 (T1): What changed"
    assert await workspace.discard(path, "jarvis S1 (T1): What changed") is None
    assert not path.exists()
    assert git(clone, "branch", "--show-current") == "users-own-work" and (clone / "wip.txt").exists()


@needs_script
async def test_a_read_only_worktree_with_changes_is_kept_and_discard_says_why(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    path = await workspace.scratch(clone, "read-what-changed", "jarvis S1 (T1): What changed")
    (path / ".gitignore").write_text("changed\n")
    kept = await workspace.discard(path, "jarvis S1 (T1): What changed")
    assert kept and path.exists()


@needs_script
async def test_a_read_only_worktree_that_holds_new_commits_is_kept_and_discard_names_them(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    path = await workspace.scratch(clone, "read-what-changed", "jarvis S1 (T1): What changed")
    (path / "note.txt").write_text("found something\n")
    git(path, "add", "note.txt")
    git(path, "commit", "-qm", "note what changed")
    sha = git(path, "rev-parse", "--short", "HEAD")

    kept = await workspace.discard(path, "jarvis S1 (T1): What changed")

    assert path.exists() and kept and "note what changed" in kept and sha in kept
    assert git(path, "rev-parse", "--short", "HEAD") == sha


def test_the_repo_lock_key_is_the_same_for_two_spellings_of_one_clone(clone: Path) -> None:
    assert lock_key(clone) == lock_key(clone.parent / clone.name.upper())


@needs_script
async def test_a_read_only_worktree_is_kept_when_discard_cannot_check_its_commits(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    path = await workspace.scratch(clone, "read-what-changed", "jarvis S1 (T1): What changed")
    real_git = getattr(workspace_module, "_git")

    async def log_fails(repo: Path, *args: str) -> tuple[int, str, str]:
        return (128, "", "fatal: bad object") if args[:1] == ("log",) else await real_git(repo, *args)

    monkeypatch.setattr(workspace_module, "_git", log_fails)  # pyright: ignore[reportPrivateUsage]

    kept = await workspace.discard(path, "jarvis S1 (T1): What changed")

    assert path.exists() and kept and "could not check" in kept and "fatal: bad object" in kept


def take_over(path: Path, holder: str) -> None:
    assert SCRIPT is not None
    subprocess.run(["bash", str(SCRIPT), "lock", "--take-over", str(path), holder], cwd=path, check=True)


@needs_script
async def test_release_leaves_a_lock_someone_else_took_over_and_says_who_holds_it(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    path, _ = await workspace.worktree(clone, "fix/typo", "jarvis S1 (T1): Fix typo")
    take_over(path, "the User's session")

    left = await workspace.release(path, "jarvis S1 (T1): Fix typo")

    assert left is not None and "the User's session" in left
    assert (path / ".sdlc" / "lock").read_text().splitlines()[1] == "the User's session"


@needs_script
async def test_discard_keeps_a_read_only_worktree_someone_else_locked(clone: Path) -> None:
    workspace = GitWorkspace("pumpkit-io", clone.parent, SCRIPT)
    path = await workspace.scratch(clone, "read-what-changed", "jarvis S1 (T1): What changed")
    take_over(path, "the User's session")

    kept = await workspace.discard(path, "jarvis S1 (T1): What changed")

    assert kept is not None and "the User's session" in kept and path.exists()
    assert (path / ".sdlc" / "lock").read_text().splitlines()[1] == "the User's session"


class LocalOrgWorkspace(GitWorkspace):
    """Skips the GitHub remote check: the scratch clone's origin is a local path."""

    async def _check_clone(self, clone: Path, short: str) -> None:
        pass


@needs_script
@pytest.mark.parametrize("writes", [True, False])
async def test_a_title_with_line_breaks_still_locks_and_releases_the_worktree(clone: Path, writes: bool) -> None:
    ended: list[Subagent] = []
    subagents = Subagents(
        setup=SubagentSetup(session=subagent_spec, workspace=LocalOrgWorkspace("pumpkit-io", clone.parent, SCRIPT)),
        backend=FakeBackend(FakeClock()),
        clock=FakeClock(),
        on_end=ended.append,
        on_over_time=lambda s: None,
    )
    title = "Fix\ntypo\x85in the\x0cREADME"
    args = {"repo": "pumpkit", "title": title, "brief": "b", "model": "haiku", "effort": "low", "writes": writes}
    subagent = await subagents.prepare("T1", args, [])
    assert isinstance(subagent, Subagent)
    assert subagent.title == "Fix typo in the README"

    await subagents.free_interrupted(subagent)

    assert "held by" not in subagent.report and "not unlocked" not in subagent.report
    assert not (subagent.cwd / ".sdlc" / "lock").exists() if writes else not subagent.cwd.exists()
