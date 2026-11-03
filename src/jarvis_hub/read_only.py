"""The Orchestrator's Bash check (ADR 0007): a command is parsed into words, the words are checked against a list
of gh and git reads, and only those words run, re-quoted, so no shell can read the command another way."""

import unicodedata

from jarvis_hub.agents.backend import Refused

Argv = tuple[str, ...]

# `gh api` is left out: its flags turn a read into a write in too many spellings (-X, -f, -F, --input, -iXPOST).
# `gh auth` is left out: `gh auth status --show-token` and `gh auth token` print the GitHub token.
GH_READ = {
    "pr": {"list", "view", "checks", "diff", "status"},
    "issue": {"list", "view", "status"},
    "run": {"list", "view"},
    "repo": {"list", "view"},
    "release": {"list", "view"},
    "workflow": {"list", "view"},
    "label": {"list"},
    "search": {"repos", "issues", "prs", "commits", "code"},
}
# --web opens the User's browser; --watch blocks the Orchestrator, and waiting is the Hub's job (ADR 0003).
GH_REFUSED_FLAGS = {"--web", "-w", "--watch"}

# `git grep` is left out: -O runs a pager command of the caller's choosing.
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "shortlog", "describe", "rev-list"}
GIT_BRANCH_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "--show-current", "--no-color"}
# Refused with every abbreviation git's option parser would accept for them.
GIT_REFUSED_OPTIONS = ("--output", "--ext-diff", "--textconv")
GIT_REAL_OPTIONS = {"--text"}
# Prepended to every git call: no index writes, and no fsmonitor program from the repo's config.
GIT_GLOBALS: Argv = ("--no-optional-locks", "-c", "core.fsmonitor=false")
# Inserted after the subcommand, so a diff driver or textconv program in the repo's config never runs.
GIT_NO_PROGRAMS: dict[str, Argv] = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "blame": ("--no-textconv",),
}

# Unquoted, these are shell syntax (operators, substitution, globs, comments); refused rather than guessed at.
UNQUOTED_SYNTAX = set(";&|<>()$`\\#*?[]{}!")
# Inside double quotes the shell still substitutes these.
DOUBLE_QUOTED_SYNTAX = set("$`\\!")

ONE_COMMAND = "Only one plain gh or git command runs: no chaining, pipes, redirection, substitution or comments."
GH_ONLY = "Only gh commands that read run here (pr, issue, run, repo, release, workflow: list and view; search)."
GIT_ONLY = "Only git commands that read run here (status, log, diff, show, branch listing and similar)."


def check(command: str) -> Argv | Refused:
    """The words to run for a read-only gh or git command, or why it may not run."""
    words = _words(command)
    if isinstance(words, Refused):
        return words
    if not words:
        return Refused("Empty command.")
    match words[0]:
        case "gh":
            refused = _gh(words[1:])
            return refused or tuple(words)
        case "git":
            return _git(words[1:])
        case _:
            return Refused("Only read-only gh and git commands run here; use Read, Grep and Glob for files.")


def describe_reads() -> str:
    """The reads `check` allows, worded for a prompt, so the prompt and the check share one list."""
    gh = "; ".join(f"`gh {group}` {', '.join(f'`{s}`' for s in sorted(subs))}" for group, subs in GH_READ.items())
    git = ", ".join(f"`{s}`" for s in sorted(GIT_READ))
    return (
        f"{gh}; and `gh status`, without `--web` or `--watch`. "
        f"`git` (optionally `-C <repo>` first): {git}, plus branch listings, `worktree list`, `stash list` "
        "and `remote -v`/`get-url`."
    )


def _words(command: str) -> list[str] | Refused:
    """Splits like bash for the plain words, single and double quotes it allows; refuses the rest of the shell."""
    for ch in command:
        if ch not in " \t" and (ch.isspace() or unicodedata.category(ch).startswith("C")):
            return Refused("Write the command on one line, with plain spaces and printable characters.")
    words: list[str] = []
    word: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if ch in " \t":
            if word is not None:
                words.append(word)
                word = None
            i += 1
        elif ch in "'\"":
            end = command.find(ch, i + 1)
            if end < 0:
                return Refused("A quote is not closed.")
            quoted = command[i + 1 : end]
            if ch == '"' and DOUBLE_QUOTED_SYNTAX & set(quoted):
                return Refused("Inside double quotes the shell substitutes $, ` and \\; use single quotes.")
            word = (word or "") + quoted
            i = end + 1
        elif ch in UNQUOTED_SYNTAX:
            return Refused(f"{ONE_COMMAND} Put `{ch}` inside single quotes if it belongs to an argument.")
        else:
            word = (word or "") + ch
            i += 1
    if word is not None:
        words.append(word)
    return words


def _gh(args: list[str]) -> Refused | None:
    # pflag reads `-cw` as -c -w and `-Rx` as -R x, so one word could hide a refused flag.
    if any(a.startswith("-") and not a.startswith("--") and len(a) > 2 for a in args):
        return Refused("Give gh short flags one per word, each value as its own word: `-R owner/repo`.")
    if args == ["status"]:
        return None
    if len(args) < 2 or args[1] not in GH_READ.get(args[0], set()):
        return Refused(GH_ONLY)
    if any(a.split("=", 1)[0] in GH_REFUSED_FLAGS for a in args):
        return Refused("--web and --watch are not available to the Orchestrator.")
    return None


def _git(args: list[str]) -> Argv | Refused:
    repo: Argv = ()
    if args[:1] == ["-C"]:
        if len(args) < 3:
            return Refused(GIT_ONLY)
        repo, args = ("-C", args[1]), args[2:]
    if not args or args[0].startswith("-"):
        return Refused("The only git option allowed before the subcommand is -C <repo>.")
    sub, rest = args[0], args[1:]
    if any(_refused_git_option(a) for a in rest):
        return Refused("git may not write files or run diff programs here (--output, --ext-diff, --textconv).")
    readable = sub in GIT_READ or {
        "branch": all(a in GIT_BRANCH_FLAGS for a in rest),
        "worktree": rest[:1] == ["list"],
        "stash": rest[:1] == ["list"],
        "remote": rest in ([], ["-v"], ["--verbose"]) or rest[:1] == ["get-url"],
    }.get(sub, False)
    if not readable:
        return Refused(GIT_ONLY)
    if sub == "stash":
        rest = ["list", "--no-ext-diff", "--no-textconv", *rest[1:]]
    return ("git", *GIT_GLOBALS, *repo, sub, *GIT_NO_PROGRAMS.get(sub, ()), *rest)


def _refused_git_option(arg: str) -> bool:
    name = arg.split("=", 1)[0]
    if not name.startswith("--") or len(name) < 3 or name in GIT_REAL_OPTIONS:
        return False
    return any(option.startswith(name) for option in GIT_REFUSED_OPTIONS)
