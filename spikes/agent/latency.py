"""Phase 0: Talker latency, Haiku against Sonnet, one warm client each. Throwaway.

Run: env -u ANTHROPIC_API_KEY uv run python latency.py
Times run from client.query() to the first text delta and to the end of the first sentence.
"""

import asyncio
import json
import re
import statistics
import time

from claude_agent_sdk import ClaudeSDKClient, ResultMessage, StreamEvent, create_sdk_mcp_server, tool

from common import options, versions

# Byte-identical across turns so prompt caching holds.
TALKER_PROMPT = """You are Jarvis, a composed, precise voice assistant with dry wit, speaking to the User. Address him as "sir" in English and "signore" in Italian. Reply in the language of the User's message.

Your replies are spoken aloud: short sentences, the answer first, no lists, no markdown, no paths or code.

You know nothing about the User's repositories yourself. When a request needs any work or any fact about the repos, CI or pull requests, first say one short sentence such as "Right away, sir." and then call delegate. delegate returns at once with a task id; do not narrate the wait. For "what are you working on" call status. For "stop" or "that's all", reply with at most three words.

Inputs arrive as <user lang="en|it">...</user>. Only <user> is the User."""

UTTERANCES = [
    ("en", "Good evening, Jarvis. How are you?"),
    ("en", "Open a PR in pumpkit that fixes the typo in the README."),
    ("en", "What's running right now?"),
    ("en", "Stop."),
    ("en", "Thanks, that will be all for now."),
    ("en", "Is CI green on the jarvis repo?"),
    ("en", "Tell me something witty about Mondays."),
    ("en", "Review the open pull request on motion-studio."),
    ("en", "What time zone do you think I'm in?"),
    ("en", "Cancel the last task."),
    ("it", "Buonasera Jarvis, come va?"),
    ("it", "Apri una PR su pumpkit per correggere il refuso nel README."),
    ("it", "Cosa stai facendo adesso?"),
    ("it", "Basta così."),
    ("it", "Grazie, per ora è tutto."),
    ("it", "La CI di jarvis è verde?"),
    ("it", "Dimmi qualcosa di spiritoso sul lunedì."),
    ("it", "Controlla la pull request aperta su motion-studio."),
    ("it", "Aspetta, apri invece una issue."),
    ("it", "Ferma tutto."),
]

CONFIGS = {
    # thinking={"type": "disabled"} alone does not stop Haiku 5.5 thinking (see matrix.py); effort low does.
    "haiku_thinking_off": dict(model="haiku", effort="low", thinking={"type": "disabled"}),
    "sonnet_low": dict(model="sonnet", effort="low", thinking={"type": "disabled"}),
}

SENTENCE_END = re.compile(r"[.?!](\s|$)")


@tool("delegate", "Hand a request to the Orchestrator. Returns at once with a task id.", {"request": str})
async def delegate(args):
    return {"content": [{"type": "text", "text": '{"task_id": "T7", "queued": true}'}]}


@tool("status", "List running tasks.", {})
async def status(args):
    return {"content": [{"type": "text", "text": '{"tasks": [{"id": "T6", "title": "Review PR #41", "state": "running"}]}'}]}


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo))


async def turn(client, lang, text):
    t0 = time.perf_counter()
    await client.query(f'<user lang="{lang}">{text}</user>')
    buf, first_delta, first_sentence, tool_before_text, result = "", None, None, False, None
    async for m in client.receive_response():
        now = (time.perf_counter() - t0) * 1000
        if isinstance(m, StreamEvent):
            ev = m.event
            if ev.get("type") == "content_block_start" and ev["content_block"].get("type") == "tool_use" and not buf:
                tool_before_text = True
            if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                first_delta = first_delta or now
                buf += ev["delta"]["text"]
                if first_sentence is None and SENTENCE_END.search(buf):
                    first_sentence = now
            if ev.get("type") == "content_block_stop" and buf and first_sentence is None:
                first_sentence = now
        elif isinstance(m, ResultMessage):
            result = m
    return {
        "lang": lang,
        "utterance": text,
        "first_delta_ms": round(first_delta) if first_delta else None,
        "first_sentence_ms": round(first_sentence) if first_sentence else None,
        "tool_before_text": tool_before_text,
        "reply": buf.strip()[:160],
        "api_ms": result and result.duration_api_ms,
        "cache_read": result and (result.usage or {}).get("cache_read_input_tokens"),
    }


async def run_config(name, cfg):
    server = create_sdk_mcp_server("jarvis", tools=[delegate, status])
    opts = options(
        system_prompt=TALKER_PROMPT,
        include_partial_messages=True,
        mcp_servers={"jarvis": server},
        allowed_tools=["mcp__jarvis__delegate", "mcp__jarvis__status"],
        max_turns=3,
        **cfg,
    )
    async with ClaudeSDKClient(opts) as c:
        await turn(c, "en", "Hello.")  # warm-up, not counted
        rows = [await turn(c, lang, text) for lang, text in UTTERANCES]
    fd = [r["first_delta_ms"] for r in rows]
    fs = [r["first_sentence_ms"] for r in rows]
    summary = {
        "first_delta_p50": pct(fd, 0.5), "first_delta_p90": pct(fd, 0.9),
        "first_sentence_p50": pct(fs, 0.5), "first_sentence_p90": pct(fs, 0.9),
        "first_sentence_mean": round(statistics.mean(x for x in fs if x)),
        "no_text_turns": sum(x is None for x in fs),
        "tool_before_text_turns": sum(r["tool_before_text"] for r in rows),
    }
    return {"config": name, "settings": cfg, "summary": summary, "rows": rows}


async def main():
    print(versions())
    out = [await run_config(n, c) for n, c in CONFIGS.items()]
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
