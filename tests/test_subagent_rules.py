"""The rules behind spawn_subagent (model and effort, branch names, connector tools) and the session it builds."""

from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.agents.backend import Refused
from jarvis_hub.agents.sdk import options_for
from jarvis_hub.config import Effort, RoleModel, Settings
from jarvis_hub.subagent_rules import (
    PUMPKIT_READS,
    PUMPKIT_SERVERS,
    READ_CONNECTOR_TOOLS,
    asked_effort,
    branch_name,
    check_choice,
    connector_check,
)
from jarvis_hub.subagents import VOICE_TOOLS, Subagent, subagent_session

PROMPTS = Path(__file__).parents[1] / "prompts"


@pytest.mark.parametrize(
    ("model", "effort", "max_effort", "words", "refusal"),
    [
        ("haiku", "low", "max", [], None),
        ("sonnet", "medium", "max", [], None),
        ("opus", "high", "max", [], None),
        ("claude-opus-5-5", "high", "max", [], None),
        ("sonnet", "high", "medium", [], "above JARVIS_MAX_EFFORT=medium"),
        ("opus", "xhigh", "max", ["Fix the typo in pumpkit."], "only when the User asks"),
        ("opus", "max", "max", ["Review it carefully."], "only when the User asks"),
        ("opus", "xhigh", "max", ["Fix it.", "Actually, go maximum effort."], None),
        ("opus", "max", "max", ["Review it at xhigh."], "only when the User asks"),
        ("opus", "max", "high", ["Give it max effort."], "above JARVIS_MAX_EFFORT=high"),
        ("gpt-5", "low", "max", [], "not a model"),
        ("sonnet", "extreme", "max", [], "not an effort"),
    ],
    ids=[
        "haiku low",
        "sonnet medium",
        "opus high",
        "full model id",
        "above the cap",
        "xhigh unasked",
        "max unasked",
        "max asked covers xhigh, in a later turn",
        "xhigh asked does not cover max",
        "asked but above the cap",
        "unknown model",
        "unknown effort",
    ],
)
def test_check_choice(model: str, effort: str, max_effort: Effort, words: list[str], refusal: str | None) -> None:
    verdict = check_choice(model, effort, max_effort=max_effort, user_words=words)
    if refusal is None:
        assert verdict is None
    else:
        assert isinstance(verdict, Refused) and refusal in verdict.reason


@pytest.mark.parametrize(
    ("words", "asked"),
    [
        ("Review it at xhigh effort.", "xhigh"),
        ("Run it at x-high.", "xhigh"),
        ("Use extra high effort for this one.", "xhigh"),
        ("Give it max effort.", "max"),
        ("Go maximum effort on the review.", "max"),
        ("Effort: max, please.", "max"),
        ("Set the effort to maximum.", "max"),
        ("Mettici il massimo impegno.", "max"),
        ("Usa lo sforzo massimo.", "max"),
        ("Fix the typo, max two files.", None),
        ("Change at most a maximum of three files.", None),
        ("Al massimo due file, per favore.", None),
        ("I'm on my Max plan, so go ahead.", None),
        ("Ask Max about the deploy.", None),
        ("Make the effort to keep it small.", None),
        ("Give it extra high priority.", None),
        ("Don't use xhigh.", None),
        ("Non usare sforzo massimo.", None),
        ("No max effort, please.", None),
        ("Do not go to maximum effort on this.", None),
        ("Never at max effort.", None),
        ("Without xhigh effort, but give it max effort.", "max"),
        ("No rush. Use max effort.", "max"),
    ],
    ids=lambda v: str(v),
)
def test_asked_effort_hears_only_an_explicit_effort_request(words: str, asked: str | None) -> None:
    assert asked_effort([words]) == asked


@pytest.mark.parametrize(
    ("kind", "title", "expected"),
    [
        ("fix", "Fix typo X in pumpkit", "fix/fix-typo-x-in-pumpkit"),
        ("feat", "Add a dark mode!", "feat/add-a-dark-mode"),
        ("chore", "  Bump deps -- again  ", "chore/bump-deps-again"),
        ("fix", "Perché è rotto?", "fix/perche-e-rotto"),
        ("feat", "x" * 80, "feat/" + "x" * 40),
        ("fix", "!!!", "fix/work"),
    ],
    ids=["typo", "punctuation", "dashes and spaces", "accents folded", "long title cut", "nothing left"],
)
def test_branch_name(kind: str, title: str, expected: str) -> None:
    assert branch_name(kind, title) == expected


@pytest.mark.parametrize(
    ("tool", "allowed"),
    [
        ("mcp__claude_ai_Pumpkit__list_posts", True),
        ("mcp__claude_ai_Pumpkit__get_post", True),
        ("mcp__claude_ai_Pumpkit__get_usage", True),
        ("mcp__pumpkit__list_posts", True),
        ("mcp__claude_ai_Pumpkit__post_now", False),
        ("mcp__claude_ai_Pumpkit__schedule_post", False),
        ("mcp__claude_ai_Pumpkit__start_post", False),
        ("mcp__claude_ai_Pumpkit__manage_subscription", False),
        ("mcp__digitalocean__droplet-list", False),
        ("mcp__digitalocean__droplet-delete", False),
        ("mcp__posthog__exec", False),
        ("mcp__claude-in-chrome__navigate", False),
        ("mcp__plugin_voicemode_voicemode__converse", False),
        ("mcp__claude_ai_Gmail__authenticate", False),
        ("mcp__claude_ai_Claude_Docs__read", True),
        ("mcp__claude_ai_Claude_Docs__update", False),
        ("mcp__some_new_server__anything", False),
    ],
    ids=lambda v: str(v),
)
def test_connector_check(tool: str, allowed: bool) -> None:
    verdict = connector_check(tool)
    assert (verdict is None) == allowed
    if not allowed:
        assert isinstance(verdict, Refused) and "Approval" in verdict.reason


def test_both_pumpkit_server_names_allow_the_same_reads_and_nothing_else() -> None:
    for server in PUMPKIT_SERVERS:
        allowed = {t.removeprefix(f"mcp__{server}__") for t in READ_CONNECTOR_TOOLS if t.startswith(f"mcp__{server}__")}
        assert allowed == set(PUMPKIT_READS), server


def a_subagent(branch: str | None) -> Subagent:
    worktrees = Path("/org/pumpkit/.claude/worktrees")
    cwd = worktrees / "fix-typo" if branch else worktrees / "read-fix-typo"
    return Subagent("S2", "T1", "pumpkit", "Fix typo", "brief", "sonnet", "medium", cwd, 0.0, branch)


def a_settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "org" / "jarvis"
    return Settings(
        user_name="Ada",
        org="pumpkit-io",
        org_dir=repo.parent,
        repo_dir=repo,
        runtime_dir=repo / ".jarvis",
        talker=RoleModel("sonnet", "low"),
        orchestrator=RoleModel("sonnet", "medium"),
    )


def test_the_subagent_prompt_carries_the_boundary_verbatim(tmp_path: Path) -> None:
    spec = subagent_session(a_settings(tmp_path), a_subagent("fix/fix-typo"))
    assert (PROMPTS / "boundary.md").read_text() in spec.system_prompt


def test_the_subagent_prompt_leaves_out_the_orchestrators_working_rules(tmp_path: Path) -> None:
    spec = subagent_session(a_settings(tmp_path), a_subagent("fix/fix-typo"))
    assert "## Working rules" not in spec.system_prompt


def test_the_subagent_prompt_points_to_the_preferences_and_the_repos_own_instructions(tmp_path: Path) -> None:
    settings = a_settings(tmp_path)
    spec = subagent_session(settings, a_subagent("fix/fix-typo"))
    assert f"`{settings.repo_dir / 'docs' / 'working-preferences.md'}`" in spec.system_prompt
    assert "AGENTS.md" in spec.system_prompt and "sdlc skills" in spec.system_prompt
    assert "{" not in spec.system_prompt.replace("{org}", "")


def test_a_writing_subagent_is_told_its_branch_and_lock(tmp_path: Path) -> None:
    spec = subagent_session(a_settings(tmp_path), a_subagent("fix/fix-typo"))
    assert "Branch: `fix/fix-typo`" in spec.system_prompt and "locked under S2" in spec.system_prompt


def test_the_subagent_session_runs_in_its_worktree_skipping_permissions_with_the_guard(tmp_path: Path) -> None:
    spec = subagent_session(a_settings(tmp_path), a_subagent("fix/fix-typo"))
    assert (spec.role, spec.model, spec.effort, spec.cwd) == ("subagent", "sonnet", "medium", a_subagent("x").cwd)
    assert spec.preset and spec.skip_permissions and spec.mcp_guard is connector_check
    assert set(VOICE_TOOLS) <= set(spec.denied_tools)


def test_a_read_only_subagent_is_told_its_worktree_has_no_branch(tmp_path: Path) -> None:
    spec = subagent_session(a_settings(tmp_path), a_subagent(None))
    assert "no branch, detached at the default branch" in spec.system_prompt
    assert spec.cwd == Path("/org/pumpkit/.claude/worktrees/read-fix-typo")


def test_a_subagent_session_loads_settings_skips_permissions_and_guards_mcp_tools(tmp_path: Path) -> None:
    options = options_for(subagent_session(a_settings(tmp_path), a_subagent("fix/x")), "/usr/local/bin/claude")
    assert options.setting_sources == ["user", "project"]
    assert options.permission_mode == "bypassPermissions"
    assert options.disallowed_tools == list(VOICE_TOOLS)
    assert not options.strict_mcp_config and options.tools is None
    [matcher] = (options.hooks or {})["PreToolUse"]
    assert matcher.matcher == "mcp__.*" and matcher.on_failure == "block"


async def test_the_mcp_hook_denies_a_tool_off_the_read_list_and_passes_one_on_it(tmp_path: Path) -> None:
    options = options_for(subagent_session(a_settings(tmp_path), a_subagent("fix/x")), "/usr/local/bin/claude")
    [matcher] = (options.hooks or {})["PreToolUse"]
    [hook] = matcher.hooks

    async def decide(tool: str) -> Any:
        return await hook({"tool_name": tool, "tool_input": {}}, None, {"signal": None})  # type: ignore[arg-type]

    denied = await decide("mcp__claude_ai_Pumpkit__post_now")
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"  # type: ignore[index]
    assert await decide("mcp__claude_ai_Pumpkit__list_posts") == {}
