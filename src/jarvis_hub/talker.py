"""The Talker's session: the one agent that holds the conversation and speaks as Jarvis."""

from pathlib import Path

from jarvis_hub.agents.backend import SessionSpec
from jarvis_hub.config import Settings

# The Hub runs from its own checkout (`uv run`, editable install), so the prompts sit beside `src/`.
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def talker_spec(settings: Settings) -> SessionSpec:
    """Rendered once per launch: the prompt must stay byte-identical across turns to stay cached."""
    template = (PROMPTS_DIR / "talker.md").read_text()
    user = settings.user_name or "the User (name not known)"
    return SessionSpec(
        role="talker",
        model=settings.talker.model,
        effort=settings.talker.effort,
        thinking=settings.talker.thinking,
        system_prompt=template.replace("{user}", user),
        cwd=settings.repo_dir,
    )
