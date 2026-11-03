"""The Talker's session: the one agent that holds the conversation and speaks as Jarvis."""

from jarvis_hub.agents.backend import SessionSpec
from jarvis_hub.config import Settings
from jarvis_hub.prompts import render


def talker_spec(settings: Settings) -> SessionSpec:
    """Without tools: the Hub adds its own, since it answers them."""
    return SessionSpec(
        role="talker",
        model=settings.talker.model,
        effort=settings.talker.effort,
        thinking=settings.talker.thinking,
        system_prompt=render("talker.md", {"user": settings.user_name or "not known"}),
        cwd=settings.repo_dir,
    )
