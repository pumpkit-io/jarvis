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


# Only an affirmative request grants; anything doubtful denies, since the User can always say it plainly.
@pytest.mark.parametrize(
    ("words", "asked"),
    [
        ("Use max effort.", "max"),
        ("Run it with maximum effort.", "max"),
        ("effort: xhigh", "xhigh"),
        ("usa lo sforzo massimo", "max"),
        ("Usa lo sforzo massimo.", "max"),
        ("Review it at xhigh effort.", "xhigh"),
        ("Run it at x-high effort.", "xhigh"),
        ("Use xhigh effort.", "xhigh"),
        ("Run it at x-high.", None),
        ("Use xhigh resolution images", None),
        ("Go x-high contrast", None),
        ("Use x high DPI exports", None),
        ("Use extra high effort for this one.", "xhigh"),
        ("Give it max effort.", "max"),
        ("Go maximum effort on the review.", "max"),
        ("Effort: max, please.", "max"),
        ("Set the effort to maximum.", "max"),
        ("Mettici il massimo impegno.", "max"),
        ("Imposta lo sforzo al massimo.", "max"),
        ("Sforzo: massimo.", "max"),
        ("Open a PR fixing typo X, max effort.", "max"),
        ("Yes, use maximum effort on it.", "max"),
        ("No rush. Use max effort.", "max"),
        ("No, use max effort.", None),
        ("Without xhigh effort, but give it max effort.", None),
        ("Fix the typo, max two files.", None),
        ("Change at most a maximum of three files.", None),
        ("Change at max three files.", None),
        ("Run the tests at max concurrency.", None),
        ("Al massimo due file, per favore.", None),
        ("I'm on my Max plan, so go ahead.", None),
        ("Ask Max about the deploy.", None),
        ("Make the effort to keep it small.", None),
        ("Give it extra high priority.", None),
        ("Max effort.", "max"),
        ("Don't use xhigh.", None),
        ("Non usare sforzo massimo.", None),
        ("No max effort, please.", None),
        ("Do not go to maximum effort on this.", None),
        ("Never at max effort.", None),
        ("Niente sforzo massimo.", None),
        ("Evita sforzo massimo.", None),
        ("Skip max effort.", None),
        ("Instead of max effort use low.", None),
        ("Rather than maximum effort, keep it low.", None),
        ("Senza sforzo massimo.", None),
        ("Tranne il massimo impegno.", None),
        ("Forget max effort.", None),
        ("Ignore max effort.", None),
        ("Stop using max effort.", None),
        ("Max effort is unnecessary.", None),
        ("Use low effort; max effort is overkill.", None),
        ("Lower than max effort.", None),
        ("Keep it under max effort.", None),
        ("Cap it below xhigh effort.", None),
        ("Neither xhigh nor max.", None),
        ("Max effort? No.", None),
        ("Use max effort. Actually forget it.", None),
        ("Use max effort. No.", None),
        ("Use max effort. Wait, never mind.", None),
        ("Is max effort needed?", None),
        ("Use max effort?", None),
        ("Should we use max effort", None),
        ("If it gets hard, use max effort.", None),
        ("Use max effort, that is too much.", None),
        ("Use max effort, not xhigh.", None),
        ("Evitiamo sforzo massimo.", None),
        ("Lascia perdere lo sforzo massimo.", None),
        ("Usa lo sforzo massimo. Anzi, lascia stare.", None),
        ("Usa lo sforzo massimo?", None),
        ("Usa lo sforzo massimo, è troppo.", None),
        ("With max effort, please.", "max"),
        ("Con lo sforzo massimo, grazie.", "max"),
        ("The last run went at max effort and it was slow.", None),
        ("I ran it at max effort yesterday.", None),
        ("Tell me what you'd do at max effort.", None),
        ("It will go at max effort.", None),
        ("Il run con sforzo massimo è fallito.", None),
        ("Okay, use max effort.", "max"),
        ("Tony said to use max effort but I disagree.", None),
        ("Pepper says go with max effort.", None),
        ("I told Bob to use max effort.", None),
        ("Other agents use max effort by default.", None),
        ("Opus is great when you use max effort.", None),
        ("We always go with max effort and it was a disaster.", None),
        ("'Use max effort' is what the docs suggest.", None),
        ("Oh great, use max effort and burn all my credits.", None),
        ("I hate when you use max effort.", None),
        ("Debug it with max effort, said the intern.", None),
        ("Mio fratello dice: usa lo sforzo massimo.", None),
        ("Lui usa sforzo massimo sempre, è uno spreco.", None),
        ("The old config had effort: max and it burned my quota.", None),
        ("Effort=max was the bug.", None),
        ("Sforzo: massimo era l'impostazione di ieri.", None),
        ("Go with max effort, just kidding.", None),
    ],
    ids=lambda v: str(v),
)
def test_asked_effort_grants_only_an_affirmative_request(words: str, asked: str | None) -> None:
    assert asked_effort([words]) == asked


@pytest.mark.parametrize(
    ("turns", "asked"),
    [
        (["Fix it.", "Actually, go maximum effort."], "max"),
        (["Use max effort.", "Fix the typo too."], "max"),
        (["Use max effort.", "Forget max effort."], None),
        (["Use max effort.", "Actually, never mind."], None),
        (["Use max effort.", "Run it at xhigh effort."], "xhigh"),
        (["Use max effort.", "Run it at xhigh."], None),
        (["Use max effort.", "Make it high."], None),
        (["Use max effort.", "Hmm, let's do high instead."], None),
        (["Use max effort.", "Medium is enough."], None),
        (["Use max effort.", "Nah."], None),
        (["Use max effort.", "high"], None),
        (["Use max effort.", "Ignore that last bit."], None),
        (["Use max effort.", "Back to normal."], None),
        (["Use max effort.", "Go with max effort, just kidding."], None),
        (["Use max effort.", "No rush."], "max"),
        (["Usa lo sforzo massimo.", "Facciamo alto."], None),
        (["Usa lo sforzo massimo.", "Meglio medio."], None),
        (["Usa lo sforzo massimo.", "No."], None),
        (["Don't use max effort.", "Use max effort."], "max"),
    ],
    ids=lambda v: str(v),
)
def test_asked_effort_follows_the_users_latest_word_on_effort(turns: list[str], asked: str | None) -> None:
    assert asked_effort(turns) == asked


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
    verdict = connector_check(tool, {})
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
