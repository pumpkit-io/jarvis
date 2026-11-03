from pathlib import Path

from jarvis_hub.config import RoleModel, Settings
from jarvis_hub.talker import talker_spec


def settings(user_name: str | None = "Ada", model: str = "sonnet") -> Settings:
    return Settings(
        user_name=user_name,
        org="acme",
        org_dir=Path("/org"),
        repo_dir=Path("/org/jarvis"),
        runtime_dir=Path("/org/jarvis/.jarvis"),
        talker=RoleModel(model, "low", False),
        orchestrator=RoleModel("sonnet", "medium"),
    )


def test_talker_runs_the_configured_model_at_low_effort_without_thinking_or_tools() -> None:
    spec = talker_spec(settings(model="haiku"))
    assert (spec.role, spec.model, spec.effort, spec.thinking) == ("talker", "haiku", "low", False)
    assert list(spec.tools) == []


def test_prompt_is_short_and_byte_identical_for_a_launch() -> None:
    first, second = talker_spec(settings()).system_prompt, talker_spec(settings()).system_prompt
    assert first == second
    # About four characters per token: well under the 1,000-token budget.
    assert len(first) < 3600


def test_prompt_keeps_the_character_and_names_the_user() -> None:
    prompt = talker_spec(settings()).system_prompt
    assert "J.A.R.V.I.S." in prompt
    assert '"sir"' in prompt and '"signore"' in prompt
    assert "Ada" in prompt
    assert "{" not in prompt


def test_prompt_without_a_user_name() -> None:
    prompt = talker_spec(settings(user_name=None)).system_prompt
    assert "not known" in prompt
