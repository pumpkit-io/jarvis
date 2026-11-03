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


def orchestrator_tools(
    *,
    report: ToolHandler,
    spawn_subagent: ToolHandler,
    list_subagents: ToolHandler,
    cancel_subagent: ToolHandler,
) -> tuple[HubTool, ...]:
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
        HubTool(
            "spawn_subagent",
            "Start a Subagent on the Task you are on: its own session, in an Org repo, cloned first if missing. "
            "With writes, it works in a new worktree on a branch of its own; without, in a worktree with no branch, "
            "removed when it ends. Returns at once; its report arrives later as an event.",
            object_schema(
                {
                    "repo": {**STRING, "description": "An Org repo, such as pumpkit."},
                    "title": {**STRING, "description": "A few words naming the work; it also names the branch."},
                    "brief": {**STRING, "description": "Everything the Subagent needs, written like a peer's brief."},
                    "model": {**STRING, "enum": ["haiku", "sonnet", "opus"]},
                    "effort": {**STRING, "enum": ["low", "medium", "high", "xhigh", "max"]},
                    "writes": {"type": "boolean", "description": "True when it changes the repo."},
                    "branch_kind": {**STRING, "enum": ["feat", "fix", "chore"], "description": "With writes."},
                },
                ("repo", "title", "brief", "model", "effort", "writes"),
            ),
            spawn_subagent,
        ),
        HubTool(
            "list_subagents",
            "Every Subagent this session: state, age, model, effort, repo, branch, folder, log, last report.",
            object_schema({}),
            list_subagents,
        ),
        HubTool(
            "cancel_subagent",
            "Stop a Subagent. Its worktree and branch are kept, and its report says what is on the branch.",
            object_schema({"subagent_id": {**STRING, "description": "Such as S2."}}, ("subagent_id",)),
            cancel_subagent,
        ),
    )
