"""The Orchestrator's session: read-only tools, its Bash through the Hub's check (ADR 0007)."""

from jarvis_hub.agents.backend import STRING, HubTool, SessionSpec, ToolHandler, object_schema
from jarvis_hub.approvals import GRANTS
from jarvis_hub.config import Settings
from jarvis_hub.prompts import render
from jarvis_hub.read_only import check, describe_reads
from jarvis_hub.waits import MAX_REPEATS, WATCH_KINDS

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


# Only the recovery message after a restart is on several Tasks at once; any other turn is on its own Task.
RESTART_TASK = {**STRING, "description": "Only after a restart: the Task this is for."}


APPROVAL = {
    "type": "object",
    "description": "Ask the User to approve one exact Boundary action. spoken is then the question.",
    "properties": {
        "action": {**STRING, "enum": ["merge_pr", "delete_branch", "subagent_action"]},
        "repo": {**STRING, "description": "An Org repo, such as pumpkit."},
        "pr": {"type": "integer", "description": "merge_pr: the PR number."},
        "branch": {**STRING, "description": "delete_branch: the branch name."},
        "sha": {
            **STRING,
            "description": "merge_pr: the PR head commit; delete_branch: the branch tip. The full 40 characters, "
            "as you read it now.",
        },
        "description": {**STRING, "description": "subagent_action: the one exact action, such as 'post the X "
        "thread from Pumpkit post 812 now'."},
        "grants": {
            "type": "array",
            "items": {**STRING, "enum": list(GRANTS)},
            "description": "subagent_action: the connector tools it needs, by group; leave out when none.",
        },
        "post": {
            **STRING,
            "description": "With the x_post grant: the Pumpkit post id it publishes. The post tools take only this id.",
        },
    },
    "required": ["action"],
}


def orchestrator_tools(
    *,
    report: ToolHandler,
    spawn_subagent: ToolHandler,
    list_subagents: ToolHandler,
    cancel_subagent: ToolHandler,
    watch: ToolHandler,
    schedule_check: ToolHandler,
    cancel_wait: ToolHandler,
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
                    "approval": APPROVAL,
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
                    "approval_id": {
                        **STRING,
                        "description": "An approved subagent_action Approval of this Task, such as A2, which the "
                        "Subagent's brief then carries.",
                    },
                    "task_id": RESTART_TASK,
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
        HubTool(
            "watch",
            "Set a Watcher on the Task you are on: the Hub waits, at no cost, until a CI run or a PR's checks finish, "
            "and its end arrives as a watcher event. ci_run takes a run id (pumpkit#12345678, from gh run list), "
            "pr_checks a PR number (pumpkit#42).",
            object_schema(
                {
                    "kind": {**STRING, "enum": list(WATCH_KINDS)},
                    "target": {**STRING, "description": "An Org repo and a number, such as pumpkit#42."},
                    "task_id": RESTART_TASK,
                },
                ("kind", "target"),
            ),
            watch,
        ),
        HubTool(
            "schedule_check",
            "Set a Check on the Task you are on: a timer event after after_seconds (60 at least), then again after "
            "twice as long each time, max_repeats times in all. Only for a change nothing announces, such as a "
            "human review.",
            object_schema(
                {
                    "after_seconds": {"type": "integer", "minimum": 60},
                    "note": {**STRING, "description": "What to look at when it comes due."},
                    "max_repeats": {"type": "integer", "minimum": 1, "maximum": MAX_REPEATS},
                    "task_id": RESTART_TASK,
                },
                ("after_seconds", "note"),
            ),
            schedule_check,
        ),
        HubTool(
            "cancel_wait",
            "Stop a Watcher or Check of the Task you are on.",
            object_schema({"wait_id": {**STRING, "description": "Such as W1 or C2."}}, ("wait_id",)),
            cancel_wait,
        ),
    )
