"""The runtime prompts, kept as prose files so people edit them as text and AGENTS.md can import the shared ones."""

from pathlib import Path

# The Hub runs from its own checkout (`uv run`, editable install), so the prompts sit beside `src/`.
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def render(name: str, values: dict[str, str]) -> str:
    """The prompt file with each `{key}` replaced by its value; other braces are left as they are."""
    text = (PROMPTS_DIR / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
