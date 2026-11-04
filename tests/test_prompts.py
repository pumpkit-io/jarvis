"""Runtime prompts stay separate from the instructions for developer agents."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SHARED = ("boundary.md", "working-rules.md", "repo-catalog.md")


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
