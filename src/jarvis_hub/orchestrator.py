"""The Orchestrator's session, and the check that keeps its Bash read-only."""

import shlex

from jarvis_hub.agents.backend import SessionSpec
from jarvis_hub.config import Settings
from jarvis_hub.prompts import render

# Model turns per message: enough for a look-up of a few commands and a report.
MAX_TURNS = 12

READ_TOOLS = ("Read", "Grep", "Glob")

# `gh api` is left out: its flags turn a read into a write in too many spellings (-X, -f, -F, --input, -iXPOST).
GH_READ = {
    "pr": {"list", "view", "checks", "diff", "status"},
    "issue": {"list", "view", "status"},
    "run": {"list", "view"},
    "repo": {"list", "view"},
    "release": {"list", "view"},
    "workflow": {"list", "view"},
    "label": {"list"},
    "search": {"repos", "issues", "prs", "commits", "code"},
    "auth": {"status"},
}
# --web opens the User's browser; --watch blocks the Orchestrator, and waiting is the Hub's job (ADR 0003).
GH_REFUSED_FLAGS = ("--web", "-w", "--watch")

# `git grep` is left out: -O runs a pager command of the caller's choosing.
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "shortlog", "describe", "rev-list"}
GIT_BRANCH_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "--show-current", "--no-color"}

# Refused anywhere, even quoted: inside double quotes the shell still substitutes them.
SUBSTITUTION = set("$`\n\r")
# Refused outside quotes, where they chain, pipe or redirect; a quoted --jq filter may use `|`.
OPERATORS = set(";&|<>()")


def read_only_refusal(command: str) -> str | None:
    """Why the Orchestrator may not run this Bash command, or None when it is a read-only `gh` or `git` call."""
    if SUBSTITUTION & set(command) or _has_shell_operator(command):
        return "Only one plain gh or git command runs: no chaining, pipes, redirection or substitution."
    try:
        argv = shlex.split(command)
    except ValueError:
        return "The command could not be parsed."
    if not argv:
        return "Empty command."
    match argv[0]:
        case "gh":
            return _gh_refusal(argv[1:])
        case "git":
            return _git_refusal(argv[1:])
        case _:
            return "Only read-only gh and git commands run here; use Read, Grep and Glob for files."


def _has_shell_operator(command: str) -> bool:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return any(token and set(token) <= OPERATORS for token in lexer)
    except ValueError:
        return True


def _gh_refusal(args: list[str]) -> str | None:
    refusal = "Only gh commands that read run here (pr, issue, run, repo, release, workflow: list and view; search)."
    if args == ["status"]:
        return None
    if len(args) < 2 or args[1] not in GH_READ.get(args[0], set()):
        return refusal
    if any(a == f or a.startswith(f + "=") for a in args for f in GH_REFUSED_FLAGS):
        return "--web and --watch are not available to the Orchestrator."
    return None


def _git_refusal(args: list[str]) -> str | None:
    refusal = "Only git commands that read run here (status, log, diff, show, branch listing and similar)."
    if args[:1] == ["-C"]:
        if len(args) < 3:
            return refusal
        args = args[2:]
    if not args or args[0].startswith("-"):
        return "The only git option allowed before the subcommand is -C <repo>."
    sub, rest = args[0], args[1:]
    if any(a.startswith("--output") for a in rest):
        return "git may not write files here."
    if sub in GIT_READ:
        return None
    readable = {
        "branch": all(a in GIT_BRANCH_FLAGS for a in rest),
        "worktree": rest[:1] == ["list"],
        "stash": rest[:1] == ["list"],
        "remote": rest in ([], ["-v"], ["--verbose"]) or rest[:1] == ["get-url"],
    }
    return None if readable.get(sub, False) else refusal


def orchestrator_spec(settings: Settings) -> SessionSpec:
    """Without tools: the Hub adds its own, since it answers them."""
    values = {
        "user": settings.user_name or "the User",
        "org": settings.org,
        "org_dir": str(settings.org_dir),
        "preferences": str(settings.repo_dir / "docs" / "working-preferences.md"),
    }
    return SessionSpec(
        role="orchestrator",
        model=settings.orchestrator.model,
        effort=settings.orchestrator.effort,
        thinking=settings.orchestrator.thinking,
        system_prompt=render("orchestrator.md", values) + "\n" + render("boundary.md", values),
        cwd=settings.org_dir,
        preset=True,
        read_tools=READ_TOOLS,
        bash_guard=read_only_refusal,
        max_turns=MAX_TURNS,
    )
