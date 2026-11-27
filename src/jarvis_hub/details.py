"""A Task report's details: shown on screen at once, read to the Talker only on request, part by part (ADR 0033)."""

import re
from html import escape
from pathlib import Path
from typing import Any

from jarvis_hub.event_log import EventLog, report_id
from jarvis_hub.secret_patterns import redact

# About 430 tokens, or two minutes of speech: one stretch the User can stop or continue.
PART_CHARS = 1500
# A file larger than this is no longer something to read out or show whole.
FILE_CHARS = 200_000
TEXT_SUFFIXES = frozenset({"", ".md", ".markdown", ".txt"})
REPORT_ID = re.compile(r"R(\d+)")
# A markdown heading, or a line that is all bold, as reports title their sections.
HEADING = re.compile(r"#{1,6} [^\n]*|\*\*[^*\n]+\*\*:?")

# Coarsest first: paragraphs, then lines, then sentences, then words, each with what joins its pieces again.
_BREAKS = ((re.compile(r"\n\s*\n"), "\n\n"), (re.compile(r"\n"), "\n"), (re.compile(r"(?<=[.!?])\s+"), " "),
           (re.compile(r"\s+"), " "))


def split_parts(text: str, size: int = PART_CHARS) -> list[str]:
    """`text` in parts of at most `size` characters, each ending at the coarsest break that fits."""
    return _split(text.strip(), size, 0) if text.strip() else []


def _split(text: str, size: int, level: int) -> list[str]:
    if len(text) <= size:
        return [text]
    if level == len(_BREAKS):
        return [text[at:at + size] for at in range(0, len(text), size)]
    pattern, joiner = _BREAKS[level]
    parts: list[str] = []
    pieces = [piece for piece in (piece.strip() for piece in pattern.split(text)) if piece]
    for piece in _with_headings(pieces) if level == 0 else pieces:
        for small in _split(piece, size, level + 1):
            if parts and len(parts[-1]) + len(joiner) + len(small) <= size:
                parts[-1] += joiner + small
            else:
                parts.append(small)
    return parts


def _with_headings(paragraphs: list[str]) -> list[str]:
    """Each heading joined to the paragraph after it, so no part ends on a heading."""
    joined: list[str] = []
    for paragraph in paragraphs:
        if joined and HEADING.fullmatch(joined[-1].rsplit("\n\n", 1)[-1]):
            joined[-1] += "\n\n" + paragraph
        else:
            joined.append(paragraph)
    return joined


def spoken_parts(details: str) -> list[str]:
    """The parts the Talker reads, without the secrets the screen may show (ADR 0028)."""
    return split_parts(redact(details))


def read_report(log: EventLog, args: dict[str, Any]) -> str:
    """The Talker's `read_report` tool: one part of a report's details, and what follows it."""
    found = REPORT_ID.fullmatch(str(args.get("report") or "").strip().upper())
    if found is None:
        return "report is the id in a Task event or recall match, such as R12."
    report = report_id(int(found[1]))
    part = 1 if args.get("part") is None else args["part"]
    if not isinstance(part, int) or part < 1:
        return "part is a whole number from 1."
    stored = log.report_details(int(found[1]))
    if stored is None:
        return f"No report {report} with details in the last 30 days."
    task_id, details = stored
    parts = spoken_parts(details)
    if part > len(parts):
        return f"{report} has {len(parts)} part{'s' if len(parts) != 1 else ''}."
    after = (f"<next>Read part {part + 1} of {len(parts)} when the User wants more.</next>" if part < len(parts)
             else "<end>This is the last part.</end>")
    return "\n".join([f'<report id="{report}" task="{escape(task_id)}" part="{part}" of="{len(parts)}">',
                      escape(parts[part - 1], quote=False), "</report>", after])


def file_details(value: object) -> str:
    """The text of the file at `value` as details markdown, headed by its name.

    Raises ValueError saying what the Orchestrator can do instead."""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise ValueError("details_file must be an absolute path.")
    if not path.is_file():
        raise ValueError(f"There is no file at {path}.")
    if path.stat().st_size > 4 * FILE_CHARS:
        raise ValueError(_too_long(path))
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not a text file: put what it says in details_markdown.") from None
    if len(text) > FILE_CHARS:
        raise ValueError(_too_long(path))
    if path.suffix.lower() not in TEXT_SUFFIXES:
        fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", text))])
        text = f"{fence}{path.suffix[1:].lower()}\n{text}\n{fence}"
    return f"**{path.name}**\n\n{text}"


def _too_long(path: Path) -> str:
    return (f"{path} is too long to attach: at most {FILE_CHARS:,} characters. Put the part the User wants in "
            "details_markdown.")
