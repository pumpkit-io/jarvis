"""The real Workspace: the Org's clones in the Org folder, and worktrees from the sdlc `worktree.sh`."""

import asyncio
import json
import re
from pathlib import Path

from jarvis_hub.subagents import Refusal

SCRIPT = Path("skills") / "auto" / "worktrees" / "scripts" / "worktree.sh"
_NAME = re.compile(r"[A-Za-z0-9._-]+")
# How many numbered variants of a branch or folder name to try before giving up.
TRIES = 20


def find_worktree_script(org_dir: Path, home: Path) -> Path | None:
    """worktree.sh from the installed sdlc plugin, else from the Org's own sdlc clone."""
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    candidates: list[Path] = []
    try:
        plugins = json.loads(installed.read_text()).get("plugins", {})
        for key, installs in plugins.items():
            if str(key).split("@")[0] == "sdlc":
                candidates += [Path(i["installPath"]) / SCRIPT for i in installs if "installPath" in i]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    candidates.append(org_dir / "sdlc" / SCRIPT)
    return next((c for c in candidates if c.is_file()), None)


def is_org_remote(url: str, org: str, repo: str) -> bool:
    """Whether a remote URL is GitHub's `org/repo`, in any of the forms git and gh write it."""
    prefix = r"(https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    return re.fullmatch(rf"{prefix}{re.escape(org)}/{re.escape(repo)}(\.git)?/?", url.strip(), re.IGNORECASE) is not None


def lock_key(clone: Path) -> str:
    """One key per clone: its real path, case-folded because a case-insensitive disk reaches it by several spellings."""
    return str(clone.resolve()).casefold()


async def _run(*args: str, cwd: Path) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


async def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    return await _run("git", "-C", str(repo), *args, cwd=repo)


class GitWorkspace:
    def __init__(self, org: str, org_dir: Path, script: Path | None) -> None:
        self._org = org
        self._org_dir = org_dir
        self._script = script
        # One lock per repo: a clone, or a name chosen and then taken, must not interleave with another Subagent's.
        self._locks: dict[str, asyncio.Lock] = {}

    async def repo(self, name: str) -> tuple[str, Path]:
        owner, _, short = name.strip().rpartition("/")
        if owner and owner.lower() != self._org.lower():
            raise Refusal(f"{name} is outside the Org {self._org}; Subagents work only on {self._org} repos.")
        if not _NAME.fullmatch(short) or short in (".", ".."):
            raise Refusal(f"{name!r} is not a repo name.")
        async with self._lock(self._org_dir / short):
            clone = self._org_dir / short
            if clone.exists() or clone.is_symlink():
                await self._check_clone(clone, short)
                return short, clone
            code, _, err = await _run("gh", "repo", "clone", f"{self._org}/{short}", str(clone), cwd=self._org_dir)
            if code != 0:
                raise Refusal(f"{self._org}/{short} could not be cloned, so it is probably not an Org repo: {err}")
            return short, clone

    async def _check_clone(self, clone: Path, short: str) -> None:
        if clone.resolve().parent != self._org_dir.resolve():
            raise Refusal(f"{clone} leads outside the Org folder {self._org_dir}.")
        code, url, _ = await _git(clone, "remote", "get-url", "origin")
        if code != 0 or not is_org_remote(url, self._org, short):
            raise Refusal(f"{clone} is not a clone of {self._org}/{short}.")

    async def worktree(self, clone: Path, branch: str, holder: str) -> tuple[Path, str]:
        if self._script is None:
            raise Refusal("The sdlc worktrees script was not found; install the sdlc plugin.")
        async with self._lock(clone):
            for n in range(1, TRIES + 1):
                candidate = branch if n == 1 else f"{branch}-{n}"
                if await self._branch_exists(clone, candidate):
                    continue
                code, out, err = await _run("bash", str(self._script), "create", candidate, cwd=clone)
                if code != 0:
                    if "is taken by" in err:
                        continue
                    raise Refusal(f"worktree.sh create {candidate} failed: {err}")
                path = Path(out.splitlines()[-1])
                if "already exists" in err:
                    # Another process made this branch since the check above: it is theirs, so try the next name.
                    continue
                code, _, err = await _run("bash", str(self._script), "lock", str(path), holder, cwd=clone)
                if code != 0:
                    raise Refusal(
                        f"Could not lock {path}: {err} The worktree {path} and branch {candidate} are kept; "
                        "no Subagent started."
                    )
                return path, candidate
        raise Refusal(f"Every name from {branch} to {branch}-{TRIES} is taken.")

    async def scratch(self, clone: Path, name: str, holder: str) -> Path:
        if self._script is None:
            raise Refusal("The sdlc worktrees script was not found; install the sdlc plugin.")
        async with self._lock(clone):
            await _git(clone, "fetch", "-q", "origin")
            code, base, _ = await _git(clone, "rev-parse", "--verify", "--quiet", "origin/HEAD")
            base = base if code == 0 and base else "HEAD"
            folder = clone / ".claude" / "worktrees"
            free = (folder / (name if n == 1 else f"{name}-{n}") for n in range(1, TRIES + 1))
            path = next((p for p in free if not p.exists()), None)
            if path is None:
                raise Refusal(f"Every folder from {name} to {name}-{TRIES} in {folder} is taken.")
            code, _, err = await _git(clone, "worktree", "add", "-q", "--detach", str(path), base)
            if code != 0:
                raise Refusal(f"Could not add a read-only worktree at {path}: {err}")
            code, _, err = await _run("bash", str(self._script), "lock", str(path), holder, cwd=clone)
            if code != 0:
                raise Refusal(f"Could not lock {path}: {err} The detached worktree is kept; no Subagent started.")
            return path

    async def release(self, worktree: Path, holder: str) -> str | None:
        # worktree.sh writes the time taken, then the holder, one per "\n"; splitlines() would also cut at \x85 or U+2028.
        try:
            lines = (worktree / ".sdlc" / "lock").read_text().split("\n")
        except FileNotFoundError:
            return None
        if lines[1:2] != [holder]:
            now = lines[1] if len(lines) > 1 else "an unnamed holder"
            return f"its lock is held by {now} now, so it was left in place."
        if self._script is not None:
            await _run("bash", str(self._script), "release", str(worktree), cwd=worktree)
        return None

    async def discard(self, worktree: Path, holder: str) -> str | None:
        if (left := await self.release(worktree, holder)) is not None:
            return left
        code, common, err = await _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if code != 0:
            return err or "it is no longer a git worktree."
        # A detached HEAD that gained commits would leave them dangling once the worktree is gone.
        code, new, err = await _git(worktree, "log", "--oneline", "HEAD", "--not", "--branches", "--remotes")
        if code != 0:
            return f"could not check it for commits found on no branch or remote, so it is kept: {err or 'git log failed.'}"
        if new:
            listed = "\n".join(f"- {c}" for c in new.splitlines())
            return f"it holds commits found on no branch or remote, which removing it would lose:\n{listed}"
        code, _, err = await _git(Path(common).parent, "worktree", "remove", str(worktree))
        return None if code == 0 else err or "git worktree remove failed."

    async def describe(self, worktree: Path) -> str:
        _, base, _ = await _git(worktree, "rev-parse", "--abbrev-ref", "origin/HEAD")
        base = base or "origin/main"
        _, commits, _ = await _git(worktree, "log", "--oneline", f"{base}..HEAD")
        _, status, _ = await _git(worktree, "status", "--short")
        lines = [f"Commits not on {base}:" if commits else f"No commits beyond {base}."]
        lines += [f"- {c}" for c in commits.splitlines()]
        lines.append(f"Uncommitted changes:\n{status}" if status else "No uncommitted changes.")
        lines.append(await self._pushed(worktree))
        return "\n".join(lines)

    async def _pushed(self, worktree: Path) -> str:
        """Whether the branch is on origin and how far ahead of it the worktree is, upstream set or not."""
        _, branch, _ = await _git(worktree, "branch", "--show-current")
        remote = f"origin/{branch}"
        code, _, _ = await _git(worktree, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}")
        if not branch or code != 0:
            return "Not pushed."
        _, ahead, _ = await _git(worktree, "rev-list", "--count", f"{remote}..HEAD")
        count = int(ahead or 0)
        if count == 0:
            return f"Pushed to {remote}."
        return f"Pushed to {remote}, with {count} commit{'s' if count != 1 else ''} not pushed."

    async def _branch_exists(self, clone: Path, branch: str) -> bool:
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
            code, _, _ = await _git(clone, "show-ref", "--quiet", "--verify", ref)
            if code == 0:
                return True
        return False

    def _lock(self, clone: Path) -> asyncio.Lock:
        return self._locks.setdefault(lock_key(clone), asyncio.Lock())


def default_workspace(org: str, org_dir: Path) -> GitWorkspace:
    return GitWorkspace(org, org_dir, find_worktree_script(org_dir, Path.home()))
