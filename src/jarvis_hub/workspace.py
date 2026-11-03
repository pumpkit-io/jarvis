"""The real Workspace: the Org's clones in the Org folder, and worktrees from the sdlc `worktree.sh`."""

import asyncio
import json
import os
import re
from pathlib import Path

from jarvis_hub.subagents import Refusal

SCRIPT = Path("skills") / "auto" / "worktrees" / "scripts" / "worktree.sh"
_NAME = re.compile(r"[A-Za-z0-9._-]+")


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


async def _run(*args: str, cwd: Path) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


class GitWorkspace:
    def __init__(self, org: str, org_dir: Path, script: Path | None) -> None:
        self._org = org
        self._org_dir = org_dir
        self._script = script

    async def repo(self, name: str) -> tuple[str, Path]:
        owner, _, short = name.strip().rpartition("/")
        if owner and owner.lower() != self._org.lower():
            raise Refusal(f"{name} is outside the Org {self._org}; Subagents work only on {self._org} repos.")
        if not _NAME.fullmatch(short) or short in (".", ".."):
            raise Refusal(f"{name!r} is not a repo name.")
        clone = self._org_dir / short
        if clone.exists():
            code, url, _ = await _run("git", "-C", str(clone), "remote", "get-url", "origin", cwd=self._org_dir)
            if code != 0 or not re.search(rf"github\.com[:/]{re.escape(self._org)}/{re.escape(short)}(\.git)?$", url, re.I):
                raise Refusal(f"{clone} is not a clone of {self._org}/{short}.")
            return short, clone
        code, _, err = await _run("gh", "repo", "clone", f"{self._org}/{short}", str(clone), cwd=self._org_dir)
        if code != 0:
            raise Refusal(f"{self._org}/{short} could not be cloned, so it is probably not an Org repo: {err}")
        return short, clone

    async def worktree(self, clone: Path, branch: str, holder: str) -> tuple[Path, str]:
        if self._script is None:
            raise Refusal("The sdlc worktrees script was not found; install the sdlc plugin.")
        for n in range(1, 21):
            candidate = branch if n == 1 else f"{branch}-{n}"
            if await self._branch_exists(clone, candidate):
                continue
            code, out, err = await _run("bash", str(self._script), "create", candidate, cwd=clone)
            if code != 0:
                if "is taken by" in err:
                    continue
                raise Refusal(f"worktree.sh create {candidate} failed: {err}")
            path = Path(out.splitlines()[-1])
            code, _, err = await _run("bash", str(self._script), "lock", str(path), holder, cwd=clone)
            if code != 0:
                raise Refusal(f"Could not lock {path}: {err}")
            return path, candidate
        raise Refusal(f"Every name from {branch} to {branch}-20 is taken.")

    async def release(self, worktree: Path) -> None:
        if self._script is not None:
            await _run("bash", str(self._script), "release", str(worktree), cwd=worktree)

    async def describe(self, worktree: Path) -> str:
        wt = str(worktree)
        _, base, _ = await _run("git", "-C", wt, "rev-parse", "--abbrev-ref", "origin/HEAD", cwd=worktree)
        _, commits, _ = await _run("git", "-C", wt, "log", "--oneline", f"{base or 'origin/main'}..HEAD", cwd=worktree)
        _, status, _ = await _run("git", "-C", wt, "status", "--short", cwd=worktree)
        code, upstream, _ = await _run("git", "-C", wt, "rev-parse", "--abbrev-ref", "@{upstream}", cwd=worktree)
        lines = [f"Commits not on {base or 'origin/main'}:" if commits else f"No commits beyond {base or 'origin/main'}."]
        lines += [f"- {c}" for c in commits.splitlines()]
        lines.append(f"Uncommitted changes:\n{status}" if status else "No uncommitted changes.")
        lines.append(f"Tracks {upstream}." if code == 0 and upstream else "Not pushed.")
        return "\n".join(lines)

    async def _branch_exists(self, clone: Path, branch: str) -> bool:
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
            code, _, _ = await _run("git", "-C", str(clone), "show-ref", "--quiet", "--verify", ref, cwd=clone)
            if code == 0:
                return True
        return False


def default_workspace(org: str, org_dir: Path) -> GitWorkspace:
    return GitWorkspace(org, org_dir, find_worktree_script(org_dir, Path(os.path.expanduser("~"))))
