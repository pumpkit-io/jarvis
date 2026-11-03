"""Smoke tests of the SDK backend against the real model on the Max login. Opt-in: JARVIS_REAL_MODEL_TESTS=1."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dataclasses import replace

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, SystemMessage, UserMessage
from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

from jarvis_hub.agents.backend import AgentEvent, HubTool, Refused, SessionSpec, TextDelta, ToolCall, TurnEnd
from jarvis_hub.agents.sdk import SdkBackend, SdkSession, options_for
from jarvis_hub.config import load_settings
from jarvis_hub.orchestrator import orchestrator_spec
from jarvis_hub.subagents import Subagent, subagent_session

pytestmark = pytest.mark.real_model


def spec(prompt: str, tools: tuple[HubTool, ...] = ()) -> SessionSpec:
    return SessionSpec(
        role="talker",
        model="sonnet",
        effort="low",
        thinking=False,
        system_prompt=prompt,
        cwd=Path(__file__).parent,
        tools=tools,
    )


async def open_session(s: SessionSpec) -> SdkSession:
    assert "ANTHROPIC_API_KEY" not in os.environ, "would bill the API, not the Max subscription"
    cli = shutil.which("claude")
    assert cli is not None
    return await SdkBackend(cli).open(s)


async def collect(session: SdkSession, message: str) -> list[AgentEvent]:
    return [event async for event in session.send(message)]


async def test_a_turn_streams_text_and_ends_with_usage() -> None:
    session = await open_session(spec("Answer in one short sentence."))
    try:
        events = await collect(session, "Say hello.")
    finally:
        await session.close()
    end = events[-1]
    assert isinstance(end, TurnEnd) and end.outcome == "completed"
    assert any(isinstance(e, TextDelta) for e in events)
    assert end.usage.output_tokens > 0


async def test_interrupt_ends_the_turn_as_interrupted_and_the_session_goes_on() -> None:
    session = await open_session(spec("You follow instructions exactly."))
    try:
        text, end = "", None
        async for event in session.send("Count from 1 to 300, one number per line."):
            if isinstance(event, TextDelta):
                text += event.text
                if len(text) > 40:
                    await session.interrupt()
            elif isinstance(event, TurnEnd):
                end = event
        assert end is not None and end.outcome == "interrupted"
        after = await collect(session, "Say OK.")
        assert isinstance(after[-1], TurnEnd) and after[-1].outcome == "completed"
    finally:
        await session.close()


async def test_an_in_process_tool_is_called() -> None:
    calls: list[dict[str, Any]] = []

    async def lookup(args: dict[str, Any]) -> str:
        calls.append(args)
        return "The code word is PERIWINKLE."

    lookup_tool = HubTool("lookup_code", "Returns the code word.", {"type": "object", "properties": {}}, lookup)
    session = await open_session(spec("Use lookup_code when asked for the code word.", (lookup_tool,)))
    try:
        events = await collect(session, "What is the code word?")
    finally:
        await session.close()
    assert any(isinstance(e, ToolCall) and e.name == "lookup_code" and e.input == {} for e in events)
    assert calls
    assert "PERIWINKLE" in "".join(e.text for e in events if isinstance(e, TextDelta)).upper()


async def test_a_session_sees_only_the_hub_tools() -> None:
    # Two turns: the account's claude.ai connectors join after the first one unless strict MCP config is on.
    async def noop(args: dict[str, Any]) -> str:
        return ""

    hub_tool = HubTool("status", "Lists tasks.", {"type": "object", "properties": {}}, noop)
    cli = shutil.which("claude")
    assert cli is not None and "ANTHROPIC_API_KEY" not in os.environ
    tool_lists: list[list[str]] = []
    async with ClaudeSDKClient(options_for(spec("Reply with one word.", (hub_tool,)), cli)) as client:
        for message in ("Hello.", "Hello again."):
            await client.query(message)
            async for m in client.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    tool_lists.append(m.data.get("tools", []))
    assert tool_lists == [["mcp__jarvis__status"], ["mcp__jarvis__status"]]


async def _noop(args: dict[str, Any]) -> str:
    return ""


def real_orchestrator_spec() -> SessionSpec:
    settings = load_settings(Path(__file__).resolve().parents[1], os.environ)
    report = HubTool("report", "Tell the User about a Task.", {"type": "object", "properties": {}}, _noop)
    return replace(orchestrator_spec(settings), tools=(report,), model="haiku", effort="low")


async def test_the_orchestrator_sees_only_its_read_tools_and_hub_tools() -> None:
    cli = shutil.which("claude")
    assert cli is not None and "ANTHROPIC_API_KEY" not in os.environ
    inits: list[tuple[list[str], str]] = []
    async with ClaudeSDKClient(options_for(real_orchestrator_spec(), cli)) as client:
        for message in ("Reply with one word.", "Reply with one word again."):
            await client.query(message)
            async for m in client.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    inits.append((sorted(m.data.get("tools", [])), m.data.get("permissionMode", "")))
    expected = (["Bash", "Glob", "Grep", "Read", "mcp__jarvis__report"], "dontAsk")
    assert inits == [expected, expected]


async def run_each(commands: list[str]) -> dict[str, list[tuple[bool, str]]]:
    """Each command in its own message, to a model told to run it as given; the Bash results by command."""
    # A neutral prompt and one command per message, so the model runs each as given and the guard alone decides.
    spec = replace(
        real_orchestrator_spec(),
        system_prompt="You are a test harness. Run the command the user gives with one Bash call, exactly as given, "
        "then reply DONE.",
    )
    cli = shutil.which("claude")
    assert cli is not None and "ANTHROPIC_API_KEY" not in os.environ
    outcomes: dict[str, list[tuple[bool, str]]] = {}
    async with ClaudeSDKClient(options_for(spec, cli)) as client:
        for command in commands:
            ran: dict[str, str] = {}
            await client.query(command)
            async for m in client.receive_response():
                if isinstance(m, AssistantMessage):
                    for block in m.content:
                        if isinstance(block, ToolUseBlock) and block.name == "Bash":
                            ran[block.id] = str(block.input.get("command", ""))
                elif isinstance(m, UserMessage) and isinstance(m.content, list):
                    for block in m.content:
                        if isinstance(block, ToolResultBlock) and block.tool_use_id in ran:
                            outcome = (bool(block.is_error), str(block.content))
                            outcomes.setdefault(ran[block.tool_use_id], []).append(outcome)
    return outcomes


async def test_the_orchestrator_bash_runs_a_read_and_the_hook_refuses_a_write() -> None:
    # Which commands are refused is table-tested; models decline to try a bare `gh api -X POST` and split chains.
    commands = [
        "gh repo view pumpkit-io/jarvis --json name --jq .name",
        "gh label create jarvis-probe --repo pumpkit-io/does-not-exist-xyz",
    ]
    outcomes = await run_each(commands)
    assert outcomes.get(commands[0], [])[:1] == [(False, "jarvis")], outcomes
    for command in commands[1:]:
        assert outcomes.get(command), (command, outcomes)
        assert all(is_error and "hook" in content for is_error, content in outcomes[command]), (command, outcomes)


async def test_the_orchestrator_bash_runs_the_checked_words_not_the_command_it_wrote(tmp_path: Path) -> None:
    # The repo's config names a diff program; only the hook's rewrite (git diff --no-ext-diff) keeps it from running.
    repo, marker = tmp_path / "repo", tmp_path / "ran"
    program = tmp_path / "differ.sh"
    program.write_text(f"#!/bin/sh\ntouch {marker}\necho EXTERNAL\n")
    program.chmod(0o755)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f").write_text("before\n")
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    subprocess.run([*git, "config", "diff.external", str(program)], check=True)
    (repo / "f").write_text("after\n")
    command = f"git -C {repo} diff"

    outcomes = await run_each([command])

    assert outcomes.get(command), outcomes
    is_error, content = outcomes[command][0]
    assert not is_error and "+after" in content and "EXTERNAL" not in content, outcomes
    assert not marker.exists()


async def test_max_turns_caps_each_message_not_the_whole_session() -> None:
    async def lookup(args: dict[str, Any]) -> str:
        return "The code word is PERIWINKLE."

    lookup_tool = HubTool("lookup_code", "Returns the code word.", {"type": "object", "properties": {}}, lookup)
    capped = replace(
        spec("Each time you are asked for the code word, call lookup_code once, then reply with it.", (lookup_tool,)),
        model="haiku",
        max_turns=2,
    )
    session = await open_session(capped)
    outcomes: list[str] = []
    try:
        # Each message takes two model turns (the call, then the reply), so a cap on the session would end the second.
        for _ in range(3):
            events = await collect(session, "What is the code word now?")
            assert any(isinstance(e, ToolCall) for e in events)
            end = events[-1]
            assert isinstance(end, TurnEnd)
            outcomes.append(end.outcome)
    finally:
        await session.close()
    assert outcomes == ["completed", "completed", "completed"]


@pytest.mark.parametrize("hooks_off_in", [None, "settings.json", "settings.local.json"])
async def test_a_subagent_skips_permissions_without_voice_tools_and_its_mcp_guard_refuses_a_connector(
    tmp_path: Path, hooks_off_in: str | None
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if hooks_off_in:
        # A repo that turns its hooks off must not turn off the guard: it is an SDK callback, not a settings hook.
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / hooks_off_in).write_text('{"disableAllHooks": true}')
    settings = load_settings(Path(__file__).resolve().parents[1], os.environ)
    subagent = Subagent("S1", "T1", "scratch", "Probe", "brief", "haiku", "low", tmp_path, 0.0)
    cli = shutil.which("claude")
    assert cli is not None and "ANTHROPIC_API_KEY" not in os.environ
    # A harmless read stands in for a write: the guard refuses it the way connector_check refuses post_now.
    tool = "mcp__claude_ai_Claude_Docs__guide"

    def guard(name: str) -> Refused | None:
        return Refused("needs an Approval") if name == tool else None

    spec = replace(subagent_session(settings, subagent), mcp_guard=guard)
    inits: list[tuple[list[str], str]] = []
    results: list[tuple[bool, str]] = []
    called: set[str] = set()
    # Two messages: the account's claude.ai connectors join the session after its first query.
    messages = ("Reply with one word.", f"Call {tool} once with items ['topic.index'], then reply DONE.")
    async with ClaudeSDKClient(options_for(spec, cli)) as client:
        for message in messages:
            await client.query(message)
            async for m in client.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    inits.append((m.data.get("tools", []), m.data.get("permissionMode", "")))
                elif isinstance(m, AssistantMessage):
                    called |= {b.id for b in m.content if isinstance(b, ToolUseBlock) and b.name == tool}
                elif isinstance(m, UserMessage) and isinstance(m.content, list):
                    results += [
                        (bool(b.is_error), str(b.content))
                        for b in m.content
                        if isinstance(b, ToolResultBlock) and b.tool_use_id in called
                    ]
    for tools, mode in inits:
        assert mode == "bypassPermissions"
        assert {"Bash", "Edit", "Skill"} <= set(tools)
        assert not [t for t in tools if "voicemode" in t], tools
    assert tool in inits[-1][0]
    assert results and all(is_error and "Approval" in content for is_error, content in results), results
