"""The real agent backend: Claude Agent SDK sessions on the installed `claude` CLI and the User's Max login."""

import json
import shlex
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookContext,
    HookMatcher,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import (
    HookCallback,
    HookEvent,
    HookInput,
    HookJSONOutput,
    McpServerConfig,
    SystemPromptPreset,
    ThinkingConfig,
)

from jarvis_hub.agents.backend import (
    AgentEvent,
    BashGuard,
    HubTool,
    McpGuard,
    Refused,
    SessionSpec,
    TextDelta,
    TextEnd,
    ToolCall,
    TurnEnd,
    Usage,
)

TOOL_SERVER = "jarvis"
# How the CLI reports a turn stopped by interrupt(); is_error is set for these too, so it cannot tell them apart.
INTERRUPTED_REASONS = {"aborted_streaming", "aborted_tools"}


def _sdk_tool(hub_tool: HubTool) -> SdkMcpTool[Any]:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": await hub_tool.handler(args)}]}

    return tool(hub_tool.name, hub_tool.description, hub_tool.input_schema)(handler)


def _bash_hook(guard: BashGuard) -> HookCallback:
    async def hook(hook_input: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        tool_input: dict[str, Any] = dict(hook_input.get("tool_input", {}))
        verdict = guard(str(tool_input.get("command", "")))
        if isinstance(verdict, Refused):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": verdict.reason,
                }
            }
        # Every word re-quoted: bash runs the words the guard checked, however it would have read the original.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "read-only",
                "updatedInput": {**tool_input, "command": shlex.join(verdict)},
            }
        }

    return hook


def _mcp_hook(guard: McpGuard) -> HookCallback:
    async def hook(hook_input: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        verdict = guard(str(hook_input.get("tool_name", "")))
        if verdict is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": verdict.reason,
            }
        }

    return hook


def _skipping_permissions(spec: SessionSpec, cli_path: str, stderr: Callable[[str], None] | None) -> ClaudeAgentOptions:
    hooks: dict[HookEvent, list[HookMatcher]] = {}
    if spec.mcp_guard:
        # bypassPermissions skips every prompt but not hooks, so this deny holds; a hook that fails blocks the call.
        hooks["PreToolUse"] = [HookMatcher(matcher="mcp__.*", hooks=[_mcp_hook(spec.mcp_guard)], on_failure="block")]
    return ClaudeAgentOptions(
        cli_path=cli_path,
        cwd=spec.cwd,
        model=spec.model,
        effort=spec.effort,
        thinking=None if spec.thinking else {"type": "disabled"},
        system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_prompt},
        # The user's settings bring the sdlc skills and plugins, the project's the repo's own CLAUDE.md.
        setting_sources=["user", "project"],
        disallowed_tools=list(spec.denied_tools),
        permission_mode="bypassPermissions",
        max_turns=spec.max_turns,
        hooks=hooks or None,
        include_partial_messages=True,
        stderr=stderr,
    )


def options_for(spec: SessionSpec, cli_path: str, stderr: Callable[[str], None] | None = None) -> ClaudeAgentOptions:
    if spec.skip_permissions:
        return _skipping_permissions(spec, cli_path, stderr)
    mcp_servers: dict[str, McpServerConfig] = {}
    if spec.tools:
        mcp_servers[TOOL_SERVER] = create_sdk_mcp_server(TOOL_SERVER, tools=[_sdk_tool(t) for t in spec.tools])
    thinking: ThinkingConfig | None = None if spec.thinking else {"type": "disabled"}
    system_prompt: str | SystemPromptPreset = (
        {"type": "preset", "preset": "claude_code", "append": spec.system_prompt} if spec.preset else spec.system_prompt
    )
    builtins = [*spec.read_tools, *(["Bash"] if spec.bash_guard else [])]
    hooks: dict[HookEvent, list[HookMatcher]] = {}
    if spec.bash_guard:
        # Bash is left off allowed_tools, so under dontAsk only this hook's allow lets a command run;
        # a hook that fails blocks the call.
        hooks["PreToolUse"] = [HookMatcher(matcher="Bash", hooks=[_bash_hook(spec.bash_guard)], on_failure="block")]
    return ClaudeAgentOptions(
        # Without cli_path the SDK runs its own bundled CLI, not the one logged in to Max.
        cli_path=cli_path,
        cwd=spec.cwd,
        model=spec.model,
        effort=spec.effort,
        thinking=thinking,
        system_prompt=system_prompt,
        setting_sources=[],
        # Keeps the bundled skill listing out of the preset prompt.
        skills=[] if spec.preset else None,
        # Also keeps out the claude.ai connectors of the logged-in account, which setting_sources does not.
        strict_mcp_config=True,
        tools=builtins,
        mcp_servers=mcp_servers,
        allowed_tools=[*spec.read_tools, *(f"mcp__{TOOL_SERVER}__{t.name}" for t in spec.tools)],
        # Denies every tool call the allowlist or the Bash hook has not approved, without prompting.
        permission_mode="dontAsk",
        max_turns=spec.max_turns,
        hooks=hooks or None,
        include_partial_messages=True,
        stderr=stderr,
    )


def turn_end(result: ResultMessage) -> TurnEnd:
    raw: dict[str, Any] = result.usage or {}
    usage = Usage(
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
        cache_read_tokens=raw.get("cache_read_input_tokens", 0),
        cache_creation_tokens=raw.get("cache_creation_input_tokens", 0),
    )
    if result.terminal_reason in INTERRUPTED_REASONS:
        return TurnEnd("interrupted", usage)
    if result.is_error:
        detail = "; ".join(result.errors or []) or result.result or result.subtype
        if result.api_error_status:
            detail = f"HTTP {result.api_error_status}: {detail}"
        return TurnEnd("failed", usage, detail)
    return TurnEnd("completed", usage)


class SdkSession:
    def __init__(self, client: ClaudeSDKClient) -> None:
        self._client = client

    async def send(self, message: str) -> AsyncIterator[AgentEvent]:
        blocks: dict[int, dict[str, Any]] = {}
        try:
            await self._client.query(message)
            async for m in self._client.receive_response():
                if isinstance(m, ResultMessage):
                    yield turn_end(m)
                    return
                if not isinstance(m, StreamEvent) or m.parent_tool_use_id is not None:
                    continue
                for event in _translate(m.event, blocks):
                    yield event
        except ClaudeSDKError as e:
            yield TurnEnd("failed", error=str(e))
            return
        yield TurnEnd("failed", error="the stream ended without a result")

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def close(self) -> None:
        await self._client.disconnect()


def _translate(event: dict[str, Any], blocks: dict[int, dict[str, Any]]) -> list[AgentEvent]:
    """Raw API stream events to AgentEvents. A tool call is reported when its input is complete."""
    kind = event.get("type")
    index: int = event.get("index", -1)
    if kind == "content_block_start":
        block: dict[str, Any] = event.get("content_block", {})
        blocks[index] = {"type": block.get("type"), "name": block.get("name", ""), "json": ""}
    elif kind == "content_block_delta":
        delta: dict[str, Any] = event.get("delta", {})
        if delta.get("type") == "text_delta":
            return [TextDelta(delta.get("text", ""))]
        if delta.get("type") == "input_json_delta" and index in blocks:
            blocks[index]["json"] += delta.get("partial_json", "")
    elif kind == "content_block_stop" and index in blocks:
        done = blocks.pop(index)
        if done["type"] == "text":
            return [TextEnd()]
        if done["type"] in ("tool_use", "server_tool_use"):
            try:
                args: dict[str, Any] = json.loads(done["json"] or "{}")
            except json.JSONDecodeError:
                args = {}
            return [ToolCall(done["name"].removeprefix(f"mcp__{TOOL_SERVER}__"), args)]
    return []


class SdkBackend:
    def __init__(self, cli_path: str, stderr: Callable[[str], None] | None = None) -> None:
        self._cli_path = cli_path
        self._stderr = stderr

    async def open(self, spec: SessionSpec) -> SdkSession:
        client = ClaudeSDKClient(options_for(spec, self._cli_path, self._stderr))
        await client.connect()
        return SdkSession(client)
