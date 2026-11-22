"""Memory (ADR 0028): the Summariser session that folds the Conversation's older part into a summary, and the checks
a summary must pass before the Talker reads it."""

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from html import escape

from jarvis_hub.agents.backend import SessionSpec
from jarvis_hub.config import Settings
from jarvis_hub.conversation import Entry
from jarvis_hub.prompts import render
from jarvis_hub.secret_patterns import find_secret, redact, redact_markup

ROLE = "summariser"
# The headings `prompts/memory.md` asks for, in order.
SECTIONS = ("Standing facts and preferences", "Open threads and promises", "Decisions", "Recent topics")
# About how long a summary may be, in estimated tokens, and how far past it one may run before it is refused.
SUMMARY_TOKENS = 5_000
OVER_CAP = 1.2


def summariser_spec(settings: Settings) -> SessionSpec:
    """No tools and one model turn: the Summariser only reads what the Hub sends and writes the summary."""
    return SessionSpec(
        role=ROLE,
        model=settings.memory.model,
        effort=settings.memory.effort,
        thinking=False,
        system_prompt=render("memory.md", {}),
        cwd=settings.repo_dir,
        max_turns=1,
    )


def summariser_message(previous: str | None, entries: Sequence[Entry], *, now: float) -> str:
    """The Summariser's one message: the summary so far and the exchanges to fold into it, secrets already
    removed so the Summariser never reads them."""
    # Redacted before rendering, which escapes the reply: a secret holding `&` would no longer match once escaped.
    exchanges = "\n".join(
        replace(entry, message=redact_markup(entry.message), reply=redact(entry.reply)).render() for entry in entries
    )
    earlier = escape(redact(previous), quote=False) if previous else "None yet."
    return (
        f"<today>{datetime.fromtimestamp(now).astimezone().strftime('%Y-%m-%d')}</today>\n"
        f"<previous_summary>\n{earlier}\n</previous_summary>\n"
        f"<conversation>\n{exchanges}\n</conversation>\n"
        "Write the new summary."
    )


def problem(summary: str, estimate: Callable[[int], int]) -> str | None:
    """Why the Hub refuses `summary`, or None when it passes: a section missing, too long, or a secret in it."""
    missing = [name for name in SECTIONS if not re.search(rf"(?im)^#+\s*{re.escape(name)}\b", summary)]
    if missing:
        return f"missing {', '.join(missing)}"
    if (size := estimate(len(summary))) > SUMMARY_TOKENS * OVER_CAP:
        return f"{size} tokens, over the {SUMMARY_TOKENS} cap"
    if find_secret(summary) is not None:
        return "holds a secret"
    return None
