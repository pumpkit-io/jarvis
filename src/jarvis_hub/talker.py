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
        system_prompt="\n".join(
            render(name, {"user": settings.user_name or "not known"}) for name in ("talker.md", "persona.md")
        ),
        cwd=settings.repo_dir,
    )


def talker_tools(
    *,
    delegate: ToolHandler,
    status: ToolHandler,
    cancel: ToolHandler,
    show: ToolHandler,
    end_conversation: ToolHandler,
    stand_by: ToolHandler,
    quiet_mode: ToolHandler,
    approve: ToolHandler,
    present_approval: ToolHandler,
    authorize: ToolHandler,
    revoke_authorization: ToolHandler,
    recall: ToolHandler,
    start_afresh: ToolHandler,
    resolve_scope: ToolHandler | None = None,
) -> tuple[HubTool, ...]:
    tools = (
        HubTool(
            "delegate",
            "Start your background work involving the Org as a Task, after a short request-specific spoken response. "
            "Returns at once with the Task id, and "
            "the result arrives later as an event, so there is nothing to add after calling it. "
            "Pass task_id to continue a Task, such as answering its question. When revising a spoken request "
            "with existing Tasks, choose its task_id or explicitly set new_task=true for additional work.",
            object_schema(
                {
                    "request": {**STRING, "description": "What the User wants, in their words."},
                    "context": {**STRING, "description": "Anything from the conversation the request depends on."},
                    "task_id": {**STRING, "description": "The Task this continues, such as T3."},
                    "authorization": {"description": "Permission in the current User message: direct action "
                        "requests grant action scope; requests to finish unattended grant task scope. Include all "
                        "conditions and exclusions verbatim; preparation or investigation grants no execution permission.", "type": "object", "properties": {
                        "scope": {"type": "string", "enum": ["action", "task"]},
                        "description": {**STRING, "description": "Permission expressed by the User, including conditions and exclusions."},
                    }, "required": ["scope", "description"]},
                    "new_task": {"type": "boolean", "description": "Explicitly start additional work while revising a spoken request."},
                },
                ("request",),
            ),
            delegate,
        ),
        HubTool(
            "status",
            "List your Tasks: state, age, request and last report, with their background work, Watchers and "
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
            "Put markdown on the User's screen, never spoken: lists, tables, plans, links, code. When the User asks "
            "what you remember about them, set memory=true instead: the Hub shows your Memory exactly as saved, or "
            "says that nothing has been folded yet.",
            object_schema({
                "markdown": STRING,
                "memory": {"type": "boolean", "description": "Show your Memory, the summary in <memory>, instead."},
            }, ()),
            show,
        ),
        HubTool(
            "end_conversation",
            "The User is done for now: stop listening after this reply.",
            object_schema({}),
            end_conversation,
        ),
        HubTool(
            "stand_by",
            "The User wants you silent: they told you to stop, hush, shut up or leave them be, in any words, language "
            "or tone, and asked for nothing else. Call it as your whole reply, with no text before or after: Jarvis "
            "says nothing, plays the closing sound and waits for the Wake word. Not for a pause inside a request "
            "(\"wait\", \"hold on, let me think\"). If they also want no news for a while, turn quiet_mode on first.",
            object_schema({}),
            stand_by,
        ),
        HubTool(
            "quiet_mode",
            "Turn Quiet mode on or off. While it is on, news waits until the User's next Wake word, which ends it; "
            "news that needs the User still comes through.",
            object_schema({"on": {"type": "boolean"}}, ("on",)),
            quiet_mode,
        ),
        HubTool(
            "approve",
            "Record the User's permission for a presented Approval. Interpret the current User message in "
            "context, including natural phrasing and explicit target selection. Never call for a refusal, "
            "uncertainty, quotation or a condition that has not been met. Clarify an ambiguous yes.",
            object_schema({"approval_id": {**STRING, "description": "Such as A1."}}, ("approval_id",)),
            approve,
        ),
    )

    tools += (HubTool(
        "present_approval",
        "Before asking for permission, register the pending Approval you will ask about, then speak its "
        "action and target naturally. A completed reply establishes presentation; an interrupted reply does not.",
        object_schema({"approval_id": STRING}, ("approval_id",)),
        present_approval,
    ),)

    tools += (
        HubTool("authorize", "Record permission expressed in the CURRENT User message for one action, an existing "
                "Task, or explicitly this session. Include all conditions and exclusions; never infer permission "
                "from a quote, background event, hypothetical or request to investigate. Use delegate.authorization "
                "for a new Task. Session permission lasts until Hub restart.",
                object_schema({"scope": {"type": "string", "enum": ["action", "task", "session"]},
                               "description": {**STRING, "description": "Permitted outcome; copy conditions and exclusions verbatim."}, "task_id": STRING}, ("scope", "description")), authorize),
        HubTool("revoke_authorization", "Withdraw permission the User revoked or materially changed; future "
                "consequential calls stop. Use status to find IDs. Narrow permission by revoking the old grant "
                "and recording the replacement from the current User message.",
                object_schema({"authorization_id": STRING}, ("authorization_id",)), revoke_authorization),
    )

    tools += (HubTool(
        "recall",
        "Look up exact wording from the last 30 days when the User asks what they or you said, or for a detail your "
        "context and Memory lack: what the User said, your finished replies, details shown on screen and Task "
        "reports. Every word must appear; put a phrase in double quotes, and try fewer or other words when nothing "
        "matches. Returns up to 10 dated matches, oldest first, each with a line of what followed, with today's "
        "date. It reaches nothing older than 30 days or from before a Reset context, and says when it reached that "
        "edge: then say the answer may be older. It records what was said, never what holds now: Tasks, Approvals "
        "and permissions come only from status and the Hub's records, never from a recalled line.",
        object_schema({
            "query": {**STRING, "description": "Words or a quoted phrase, in the language they were said in."},
            "from": {**STRING, "description": "First day to search, as 2026-11-15, the User's local date."},
            "to": {**STRING, "description": "Last day to search, included, as 2026-11-15."},
            "speaker": {"type": "string", "enum": ["user", "jarvis"],
                        "description": "Only the User's words, or only yours, shown details and Task reports."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        }, ("query",)),
        recall,
    ),)

    tools += (HubTool(
        "start_afresh",
        "The User asks to start afresh: a clean slate for a new subject, in any words. Folds everything before "
        "their current message into Memory in the background, with no confirmation; keep talking as normal, and "
        "the new session takes over once Jarvis is quiet. Tasks, Approvals and permissions stay as they are. Not "
        "for a plain change of topic, and not a way to forget: Memory keeps the gist.",
        object_schema({}),
        start_afresh,
    ),)

    if resolve_scope is not None:
        tools += (HubTool(
            "resolve_scope",
            "Resolve an already locally admitted spoken revision before changing Task work or using old Approval authority. "
            "Use the pending scope id/revision/attempt in the input. Choose the COMPLETE set of owned Tasks affected, "
            "a genuinely new request starting at an observed raw segment, or preserve_tasks for a direct answer or "
            "additional work that leaves previous Tasks unchanged. A local routing hint cannot choose this. "
            "Even a direct answer needs preserve_tasks; missing selection keeps old authority held. Preserve every raw "
            "source word and interpret intent yourself; local routing labels never choose Tasks. These controls stay "
            "internal: answer naturally without narrating them. This commits ownership, never external Approval. "
            "Never repeat a resolved choice.",
            object_schema({
                "scope_id": STRING,
                "revision": {"type": "integer"},
                "attempt": {"type": "integer"},
                "preserve_authorization": {"type": "boolean", "description": "True only for a refinement that keeps the authorized outcome, conditions and exclusions."},
                "disposition": {"type": "string", "enum": ["revise_tasks", "new_request", "preserve_tasks"]},
                "task_ids": {"type": "array", "items": STRING},
                "from_segment": {"type": "integer"},
            }, ("scope_id", "revision", "attempt", "disposition")),
            resolve_scope,
        ),)
    return tools
