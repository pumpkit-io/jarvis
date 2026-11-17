"""Runtime prompts stay separate from the instructions for developer agents."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SHARED = ("boundary.md", "working-rules.md", "repo-catalog.md", "persona.md")


def imports(memory_file: Path) -> list[str]:
    return re.findall(r"^@(\S+)$", memory_file.read_text(), flags=re.MULTILINE)


def test_developer_instructions_do_not_import_runtime_prompts() -> None:
    assert imports(ROOT / "CLAUDE.md") == ["AGENTS.md"]
    assert imports(ROOT / "AGENTS.md") == []
    for name in SHARED:
        assert (ROOT / "prompts" / name).is_file()


def test_agents_md_keeps_no_copy_of_a_shared_prompt() -> None:
    agents = (ROOT / "AGENTS.md").read_text()
    for name in SHARED:
        for line in (ROOT / "prompts" / name).read_text().splitlines():
            if line.startswith("- "):
                assert line not in agents, (name, line)


def test_shared_prompts_have_no_runtime_placeholders() -> None:
    for name in SHARED:
        assert "{" not in (ROOT / "prompts" / name).read_text(), name


def test_the_persona_alone_holds_jarvis_character() -> None:
    persona = (ROOT / "prompts" / "persona.md").read_text()
    talker = (ROOT / "prompts" / "talker.md").read_text()
    for name in ("Iron Man", "Stark", "J.A.R.V.I.S"):
        assert name not in persona and name not in talker, name
    assert '"sir"' in persona and "sir" not in talker


def sdlc_section() -> str:
    text = (ROOT / "prompts" / "orchestrator.md").read_text()
    return text.split("## Briefs that use the sdlc skills\n", 1)[1].split("\n## ", 1)[0]


def test_the_orchestrator_starts_sdlc_briefs_with_a_slash_command_only_in_set_up_repos() -> None:
    section = sdlc_section()
    assert "first line of its brief" in section and "`/sdlc:<skill> <request>`" in section
    assert "`## Agent skills`" in section and "`docs/agents/issue-tracker.md`" in section
    assert "not set up" in section and "{org_dir}" in section


def test_the_orchestrator_maps_each_command_skill_to_its_work() -> None:
    rows = [line for line in sdlc_section().splitlines() if line.startswith("| ") and "/sdlc:" in line]
    mapped = set(re.findall(r"`/sdlc:([a-z-]+)", "\n".join(rows)))
    assert mapped == {
        "grill-with-docs",
        "to-spec",
        "to-tickets",
        "implement",
        "implement-spec",
        "refresh-spec",
        "triage",
        "deslop",
        "improve-codebase-architecture",
        "update-design",
        "retro",
        "handoff",
        "setup-sdlc",
        "check-sdlc",
    }


def test_the_orchestrator_uses_grill_with_docs_only_when_the_user_asks() -> None:
    section = sdlc_section()
    assert "`grill-with-docs` only when the User asks for it" in section
    assert "does not trigger it" in section and 'Leave the word "grill" out' in section
    assert "not set up, say so in your report" in section
