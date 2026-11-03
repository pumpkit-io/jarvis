"""The Orchestrator's session: read-only tools, its Bash through the Hub's check (ADR 0007)."""

from jarvis_hub.agents.backend import STRING, HubTool, SessionSpec, ToolHandler, object_schema
from jarvis_hub.config import Settings
from jarvis_hub.prompts import render
from jarvis_hub.read_only import check, describe_reads

# Model turns per message: enough for a look-up of a few commands and a report.
MAX_TURNS = 12

READ_TOOLS = ("Read", "Grep", "Glob")


def orchestrator_spec(settings: Settings) -> SessionSpec:
    """Without tools: the Hub adds its own, since it answers them."""
    values = {
        "user": settings.user_name or "the User",
        "org": settings.org,
        "org_dir": str(settings.org_dir),
        "preferences": str(settings.repo_dir / "docs" / "working-preferences.md"),
        "bash_reads": describe_reads(),
    }
    return SessionSpec(
        role="orchestrator",
        model=settings.orchestrator.model,
        effort=settings.orchestrator.effort,
        thinking=settings.orchestrator.thinking,
        system_prompt="\n".join(
            render(name, values) for name in ("orchestrator.md", "working-rules.md", "repo-catalog.md", "boundary.md")
        ),
        cwd=settings.org_dir,
        preset=True,
        read_tools=READ_TOOLS,
        bash_guard=check,
        max_turns=MAX_TURNS,
    )


def orchestrator_tools(*, report: ToolHandler) -> tuple[HubTool, ...]:
    return (
        HubTool(
            "report",
            "Tell the User about a Task. The only way to reach them.",
            object_schema(
                {
                    "task_id": STRING,
                    "spoken": {**STRING, "description": "One sentence, written to be heard."},
                    "details_markdown": {**STRING, "description": "Details for the screen."},
                    "needs_user": {"type": "boolean", "description": "True when the Task waits on the User."},
                },
                ("task_id", "spoken", "needs_user"),
            ),
            report,
        ),
    )
