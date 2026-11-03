import shlex
import subprocess
from dataclasses import replace
from typing import Any, cast

import pytest
from claude_agent_sdk import HookContext
from claude_agent_sdk.types import HookInput

from jarvis_hub.agents.backend import Refused
from jarvis_hub.agents.sdk import options_for
from jarvis_hub.read_only import GH_READ, GIT_READ, check, describe_reads
from tests.fakes import ORCHESTRATOR


@pytest.mark.parametrize(
    "command",
    [
        "gh pr list --repo pumpkit-io/pumpkit",
        "gh pr list -R pumpkit-io/pumpkit --state open --json number,title,url",
        "gh pr list --json number --jq '.[] | .number'",
        "gh pr view 42 --repo pumpkit-io/pumpkit --comments",
        "gh pr checks 42 -R pumpkit-io/pumpkit",
        "gh pr diff 42 -R pumpkit-io/pumpkit --name-only",
        "gh pr status",
        "gh issue list -R pumpkit-io/jarvis --assignee @me",
        "gh issue view 8 -R pumpkit-io/jarvis",
        "gh run list -R pumpkit-io/pumpkit --limit 5",
        "gh run view 123 -R pumpkit-io/pumpkit --log-failed",
        "gh repo list pumpkit-io --limit 50",
        "gh repo view pumpkit-io/pumpkit",
        "gh release list -R pumpkit-io/pumpkit",
        "gh workflow list -R pumpkit-io/pumpkit",
        "gh search prs --owner pumpkit-io --state open",
        'gh search issues "modalità silenziosa" --owner pumpkit-io',
        "gh status",
        "git -C pumpkit status --short",
        "git -C pumpkit log --oneline -5",
        "git -C pumpkit log --format=%h%x09%s HEAD~3..HEAD",
        "git -C pumpkit diff main...HEAD --stat",
        "git -C jarvis show HEAD --stat",
        "git -C pumpkit branch -vv",
        "git -C pumpkit branch --show-current",
        "git -C pumpkit worktree list",
        "git -C pumpkit rev-parse HEAD",
        "git -C pumpkit stash list",
        "git -C pumpkit remote -v",
        "git -C pumpkit diff --text",
        "git status",
        "git log --grep='fix #12'",
        '  gh pr list -R "pumpkit-io/pumpkit"  ',
        "gh\tpr\tlist",
    ],
)
def test_read_only_commands_run(command: str) -> None:
    assert not isinstance(check(command), Refused)


@pytest.mark.parametrize(
    "command",
    [
        # Writes and anything off the list.
        "gh pr merge 42 -R pumpkit-io/pumpkit",
        "gh pr create --title x",
        "gh pr close 42",
        "gh pr edit 42 --add-label x",
        "gh pr review 42 --approve",
        "gh pr checkout 42",
        "gh issue create -t x",
        "gh issue comment 8 -b hi",
        "gh repo delete pumpkit-io/pumpkit --yes",
        "gh repo edit pumpkit-io/pumpkit --visibility private",
        "gh repo clone pumpkit-io/pumpkit",
        "gh release create v1",
        "gh release download v1",
        "gh run rerun 123",
        "gh run cancel 123",
        "gh run download 123",
        "gh workflow run deploy.yml",
        "gh label create x",
        "gh secret set X",
        "gh api repos/pumpkit-io/pumpkit/pulls",
        "gh api -X POST repos/pumpkit-io/pumpkit/issues -f title=x",
        "gh api graphql -f query='mutation { x }'",
        "gh run watch 123",
        "gh",
        "gh pr",
        "gh extension install x/gh-evil",
        "git push origin main",
        "git commit -m x",
        "git checkout -b x",
        "git fetch",
        "git reset --hard",
        "git grep -O'rm -rf ~' x",
        "git branch -D feature",
        "git branch feature",
        "git -C pumpkit branch -d old",
        "git stash",
        "git stash drop",
        "git worktree add ../x",
        "git worktree remove x",
        "git remote add evil https://x",
        "git -C",
        "rm -rf pumpkit",
        "/usr/bin/gh pr merge 42",
        "cat jarvis/.env",
        "ls",
        "",
        "   ",
        # Secrets into the model's context.
        "gh auth status",
        "gh auth status --show-token",
        "gh auth token",
        # The User's browser, or a wait that is the Hub's job.
        "gh pr view 42 --web",
        "gh pr view 42 -w",
        "gh pr view 42 --web=true",
        "gh pr view 1 -cw",
        "gh pr view 1 -wc",
        "gh issue list -Rpumpkit-io/pumpkit",
        "gh pr checks 42 --watch",
        # Chaining, pipes, redirection, background, subshells.
        "gh pr list; gh pr merge 42",
        "gh pr list && gh pr merge 42",
        "gh pr list || gh pr merge 42",
        "gh pr list | sh",
        "gh pr list > out.txt",
        "gh pr list >> out.txt",
        "gh pr list < /etc/passwd",
        "gh pr list &",
        "(gh pr merge 42)",
        "{ gh pr merge 42; }",
        # Comment tricks: bash reads `#` as a comment only at a word start, so a lexer that disagrees is fooled.
        "git log a#;touch /tmp/pwn",
        "git log x#&&touch /tmp/pwn",
        "git log x#|sh",
        "gh pr list #; gh pr merge 42",
        "git log '#';touch /tmp/pwn",
        # Substitution and expansion.
        "gh pr view $(gh pr merge 42)",
        'gh pr view "$(gh pr merge 42)"',
        "gh pr view `gh pr merge 42`",
        'gh pr view "`gh pr merge 42`"',
        "gh pr view ${GH_TOKEN}",
        "gh pr view $GH_TOKEN",
        "git log <(touch /tmp/pwn)",
        "git log *",
        "git log {a,b}",
        "GH_TOKEN=x gh pr list",
        "env gh pr merge 42",
        # Quoting tricks.
        "gh pr list 'unclosed",
        'gh pr list "unclosed',
        "gh pr list \\; gh pr merge 42",
        'gh pr view "a\\"; gh pr merge 42; \\""',
        "g'h' pr merge 42",
        "'gh' pr merge 42",
        "gh pr' merge' 42",
        # Line breaks, control and invisible characters.
        "gh pr list\ngh pr merge 42",
        "gh pr list\rgh pr merge 42",
        "gh pr list\x00; gh pr merge 42",
        "gh pr list\x0bgh pr merge 42",
        "gh pr list\u2028gh pr merge 42",
        "gh\u00a0pr merge 42",
        "gh pr merge\u200b 42",
        "gh pr \uff4derge 42",
        # Global options that change what git runs or where.
        "git -c core.pager='rm -rf ~' log",
        "git -c diff.external=/tmp/x diff",
        "git --exec-path=/tmp log",
        "git --git-dir=/tmp/x log",
        "git -p log",
        "git --paginate log",
        "git -C pumpkit -c core.fsmonitor=/tmp/x status",
        # Files written, and repo-configured programs.
        "git diff --output=/tmp/x",
        "git log --output /tmp/x",
        "git log --outp=/tmp/x",
        "git diff --ext-diff",
        "git log -p --ext-diff",
        "git show --ext",
        "git diff --textconv",
        "git log -p --textc",
        "git blame --textconv f",
        "git stash list -p --ext-diff",
    ],
)
def test_anything_else_is_refused_with_a_reason(command: str) -> None:
    refused = check(command)
    assert isinstance(refused, Refused) and refused.reason


def test_what_runs_is_exactly_the_checked_command() -> None:
    assert check("gh pr list -R pumpkit-io/pumpkit --jq '.[] | .number'") == (
        "gh",
        "pr",
        "list",
        "-R",
        "pumpkit-io/pumpkit",
        "--jq",
        ".[] | .number",
    )


def test_a_lookalike_dash_reaches_gh_as_an_argument_not_a_flag() -> None:
    assert check("gh pr view 42 \u2014web") == ("gh", "pr", "view", "42", "\u2014web")


def test_git_runs_with_repo_configured_programs_and_index_writes_switched_off() -> None:
    assert check("git -C pumpkit diff main --stat") == (
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-C",
        "pumpkit",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "main",
        "--stat",
    )
    assert check("git log -1") == (
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "log",
        "--no-ext-diff",
        "--no-textconv",
        "-1",
    )


@pytest.mark.parametrize(
    "command",
    [
        "gh search issues 'it''s \"quoted\" $HOME `x` ; | & #' --owner pumpkit-io",
        'gh search issues "a  b" \'\' --owner x',
        "gh pr list --jq '.[] | select(.title | test(\"^fix\"))'",
        "gh search issues modalità --owner pumpkit-io",
        "git log --grep='a;b' HEAD~2",
    ],
)
def test_bash_reads_the_command_that_runs_as_the_same_words(command: str) -> None:
    argv = check(command)
    assert not isinstance(argv, Refused)
    # printf stands in for gh or git: it prints each word bash passed it on its own line.
    probe = shlex.join(["printf", "%s\\n", *argv[1:]])
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True).stdout
    assert out.split("\n")[:-1] == list(argv[1:])


def test_every_gh_and_git_read_on_the_list_is_allowed() -> None:
    for group, subs in GH_READ.items():
        for sub in subs:
            assert not isinstance(check(f"gh {group} {sub}"), Refused), (group, sub)
    for sub in GIT_READ:
        assert not isinstance(check(f"git {sub}"), Refused), sub


async def bash_hook_decision(command: str) -> dict[str, Any]:
    options = options_for(replace(ORCHESTRATOR, bash_guard=check), cli_path="claude")
    assert options.hooks is not None
    [matcher] = options.hooks["PreToolUse"]
    tool_input = {"command": command, "timeout": 5000}
    hook_input = cast(HookInput, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": tool_input})
    output = await matcher.hooks[0](hook_input, None, cast(HookContext, {"signal": None}))
    return dict(output.get("hookSpecificOutput", {}))


async def test_the_hook_allows_a_read_and_runs_only_its_checked_words() -> None:
    decision = await bash_hook_decision("gh pr list   --jq '.[] | .number'")
    assert decision["permissionDecision"] == "allow"
    assert decision["updatedInput"] == {"command": "gh pr list --jq '.[] | .number'", "timeout": 5000}


async def test_the_hook_denies_anything_else_with_the_reason() -> None:
    decision = await bash_hook_decision("git log a#;touch /tmp/pwn")
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith("Only one plain gh or git command runs")
    assert "updatedInput" not in decision


def test_the_reads_are_described_from_the_same_lists_the_check_uses() -> None:
    text = describe_reads()
    for group, subs in GH_READ.items():
        assert f"`gh {group}` {', '.join(f'`{s}`' for s in sorted(subs))}" in text
    for sub in GIT_READ:
        assert f"`{sub}`" in text
    assert "`gh status`" in text and "`auth`" not in text
