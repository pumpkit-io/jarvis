"""Phase 0 capability checks for the Agent SDK on the Max login. Throwaway.

Run: env -u ANTHROPIC_API_KEY uv run python caps.py
"""

import asyncio
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from common import options, write_json

results: dict[str, object] = {}


def text_delta(msg) -> str | None:
    if isinstance(msg, StreamEvent):
        ev = msg.event
        if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
            return ev["delta"]["text"]
    return None


async def collect(client: ClaudeSDKClient):
    out = []
    async for m in client.receive_response():
        out.append(m)
    return out


def init_of(msgs):
    return next(m.data for m in msgs if isinstance(m, SystemMessage) and m.subtype == "init")


def result_of(msgs) -> ResultMessage:
    return next(m for m in msgs if isinstance(m, ResultMessage))


def reply_text(msgs) -> str:
    return "".join(
        b.text for m in msgs if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, TextBlock)
    )


async def two_turns_streaming_and_subscription():
    async with ClaudeSDKClient(options(include_partial_messages=True, model="haiku", thinking={"type": "disabled"})) as c:
        await c.query("Remember the number 41. Reply with one short sentence.")
        t1 = await collect(c)
        await c.query("What number did I ask you to remember, plus one? Digits only.")
        t2 = await collect(c)
        server = await c.get_server_info()
    init = init_of(t1)
    deltas = [d for d in map(text_delta, t1 + t2) if d]
    rate = [m.rate_limit_info.raw for m in t1 + t2 if isinstance(m, RateLimitEvent)]
    r2 = result_of(t2)
    results["subscription"] = {
        "apiKeySource": init.get("apiKeySource"),
        "rate_limit_events": rate,
        "total_cost_usd_reported": r2.total_cost_usd,
        "account": {k: ((server or {}).get("account") or {}).get(k) for k in ("subscriptionType", "apiProvider")},
    }
    results["two_turn"] = {"turn2_reply": reply_text(t2), "same_session": result_of(t1).session_id == r2.session_id}
    results["stream_deltas"] = {"count": len(deltas), "first": deltas[:3]}
    results["init_default_tools_empty"] = {
        "tools": init.get("tools"),
        "mcp_servers": init.get("mcp_servers"),
        "skills": init.get("skills"),
        "plugins": init.get("plugins"),
        "slash_commands_n": len(init.get("slash_commands", [])),
        "agents": init.get("agents"),
        "model": init.get("model"),
        "claude_code_version": init.get("claude_code_version"),
    }


async def interrupt_mid_stream():
    async with ClaudeSDKClient(options(include_partial_messages=True, model="haiku", thinking={"type": "disabled"})) as c:
        await c.query("Count from 1 to 300, one number per line, no other text.")
        seen = ""
        t_interrupt = None
        chars_at_interrupt = None
        msgs = []
        async for m in c.receive_response():
            msgs.append(m)
            d = text_delta(m)
            if d:
                seen += d
                if t_interrupt is None and len(seen) > 40:
                    t_interrupt = time.perf_counter()
                    chars_at_interrupt = len(seen)
                    await c.interrupt()
        t_end = time.perf_counter()
        r = result_of(msgs)
        await c.query("Reply with the single word READY.")
        after = await collect(c)
    results["interrupt"] = {
        "chars_at_interrupt": chars_at_interrupt,
        "chars_before_result": len(seen),  # deltas keep arriving after interrupt(); the Hub must drop them
        "last_line_seen": seen.strip().splitlines()[-1] if seen.strip() else None,
        "interrupt_to_result_ms": round((t_end - t_interrupt) * 1000) if t_interrupt else None,
        "result_subtype": r.subtype,
        "terminal_reason": r.terminal_reason,
        "next_turn_reply": reply_text(after),
    }


async def system_prompt_replaces_default():
    msgs = [m async for m in query(
        prompt="What is your name? One word.",
        options=options(model="haiku", thinking={"type": "disabled"},
                        system_prompt="Your name is Zorblax. You are not Claude and not Claude Code."),
    )]
    r = result_of(msgs)
    results["system_prompt_string"] = {"reply": reply_text(msgs), "usage": r.usage}


async def setting_sources_compare():
    # Run inside the jarvis worktree, whose project settings carry a SessionStart hook.
    repo = str(Path(__file__).resolve().parents[2])
    out = {}
    for label, src in (("empty", []), ("omitted", None)):
        msgs = [m async for m in query(
            prompt="Reply OK.",
            options=options(model="haiku", thinking={"type": "disabled"}, cwd=repo, setting_sources=src,
                            include_hook_events=True),
        )]
        init = init_of(msgs)
        hooks = [m.data.get("hook_event_name") or m.subtype for m in msgs
                 if isinstance(m, SystemMessage) and m.subtype.startswith("hook")]
        out[label] = {
            "plugins": [p.get("name") for p in init.get("plugins", [])],
            "skills_n": len(init.get("skills", [])),
            "mcp_servers": [s.get("name") for s in init.get("mcp_servers", [])],
            "slash_commands_n": len(init.get("slash_commands", [])),
            "hook_events": hooks,
            "input_plus_cache_tokens": sum(result_of(msgs).usage.get(k, 0) for k in
                                           ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
        }
    results["setting_sources"] = out


calls: list[dict] = []


@tool("lookup_code", "Return the secret code for a given name.", {"name": str})
async def lookup_code(args):
    calls.append(args)
    return {"content": [{"type": "text", "text": f"The code for {args['name']} is 7319."}]}


async def in_process_tool_and_no_builtins():
    server = create_sdk_mcp_server("jarvis", tools=[lookup_code])
    async with ClaudeSDKClient(options(
        model="haiku", thinking={"type": "disabled"}, max_turns=3,
        mcp_servers={"jarvis": server}, allowed_tools=["mcp__jarvis__lookup_code"],
    )) as c:
        await c.query("Use your tool to look up the code for Ada, then tell me the code.")
        msgs = await collect(c)
        await c.query("Run `ls` in the shell and list the files. If you have no tool for that, say NO_SHELL.")
        msgs2 = await collect(c)
    init = init_of(msgs)
    results["in_process_tool"] = {
        "handler_calls": calls,
        "reply": reply_text(msgs),
        "tool_uses": [b.name for m in msgs if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, ToolUseBlock)],
    }
    results["tools_empty_plus_mcp"] = {
        "init_tools": init.get("tools"),
        "shell_probe_reply": reply_text(msgs2),
        "shell_probe_tool_uses": [b.name for m in msgs2 if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, ToolUseBlock)],
    }


async def main():
    for step in (two_turns_streaming_and_subscription, interrupt_mid_stream, system_prompt_replaces_default,
                 setting_sources_compare, in_process_tool_and_no_builtins):
        try:
            await step()
        except Exception as e:  # spike: record and keep going
            results[step.__name__] = {"error": repr(e)}
    write_json("caps.json", results)


if __name__ == "__main__":
    asyncio.run(main())
