"""The runtime prompts, kept as files beside `src/` so they can be read and edited as prose."""

from pathlib import Path

# The Hub runs from its own checkout (`uv run`, editable install), so the prompts sit beside `src/`.
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def render(name: str, values: dict[str, str]) -> str:
    """Fills each `{key}` in the prompt file. Rendered once per launch so the prompt stays cached."""
    text = (PROMPTS_DIR / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
