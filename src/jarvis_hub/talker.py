"""The Talker's session: the one agent that holds the conversation and speaks as Jarvis."""

from jarvis_hub.agents.backend import STRING, HubTool, SessionSpec, ToolHandler, object_schema
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


def talker_tools(
    *,
    delegate: ToolHandler,
    status: ToolHandler,
    cancel: ToolHandler,
    show: ToolHandler,
    end_conversation: ToolHandler,
    quiet_mode: ToolHandler,
) -> tuple[HubTool, ...]:
    return (
        HubTool(
            "delegate",
            "Hand work that needs the Org to the Orchestrator as a Task. Returns at once with the Task id, and "
            "the result arrives later as an event, so there is nothing to add after calling it. "
            "Pass task_id to continue a Task, such as answering its question.",
            object_schema(
                {
                    "request": {**STRING, "description": "What the User wants, in their words."},
                    "context": {**STRING, "description": "Anything from the conversation the request depends on."},
                    "task_id": {**STRING, "description": "The Task this continues, such as T3."},
                },
                ("request",),
            ),
            delegate,
        ),
        HubTool(
            "status",
            "List this session's Tasks: state, age, request and last report, with their Subagents, Watchers and "
            "Checks. Answered at once.",
            object_schema({}),
            status,
        ),
        HubTool(
            "cancel",
            "Cancel a Task by id, or only one Watcher or Check of it; the Task then goes on.",
            object_schema({"task_id": {**STRING, "description": "Such as T3, or W1 or C2 for one wait."}}, ("task_id",)),
            cancel,
        ),
        HubTool(
            "show",
            "Put markdown on the User's screen, never spoken: lists, tables, plans, links, code.",
            object_schema({"markdown": STRING}, ("markdown",)),
            show,
        ),
        HubTool(
            "end_conversation",
            "The User is done for now: stop listening after this reply.",
            object_schema({}),
            end_conversation,
        ),
        HubTool(
            "quiet_mode",
            "Turn Quiet mode on or off. While it is on, news waits until the User's next Wake word, which ends it; "
            "news that needs the User still comes through.",
            object_schema({"on": {"type": "boolean"}}, ("on",)),
            quiet_mode,
        ),
    )
