"""Scripted delegation run: does the real Talker speak before its first tool call?

The Talker is real (SDK on the Max login); the Orchestrator is a stub that reports after REPORT_DELAY seconds,
so the run costs only Talker turns. Each line is typed once the Hub has gone quiet, as a User waiting for the reply would.
A request to open an issue gets a question back (needs_user), so the next line answers it.

    env -u ANTHROPIC_API_KEY -u VIRTUAL_ENV uv run python spikes/hub/delegation.py --model sonnet --out run.json
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

from jarvis_hub.agents.backend import AgentEvent, SessionSpec, ToolCall, TurnEnd
from jarvis_hub.agents.sdk import SdkBackend
from jarvis_hub.config import load_settings
from jarvis_hub.hub import Hub, Line
from jarvis_hub.orchestrator import orchestrator_spec
from jarvis_hub.talker import talker_spec

ROOT = Path(__file__).resolve().parents[2]
LINES = ROOT / "docs" / "notes" / "talker-delegation.txt"
# About a real look-up's time, so a Task is still working if the Talker checks on it straight away.
REPORT_DELAY = 3.0


class StubOrchestrator:
    def __init__(self, spec: SessionSpec) -> None:
        self._report = next(t.handler for t in spec.tools if t.name == "report")

    async def send(self, message: str) -> AsyncIterator[AgentEvent]:
        task_id = message.split('"')[1]
        request = message.split("<request>")[1].lower()
        asks = request.startswith(("open an issue", "apri una issue")) and 'continues="true"' not in message
        args: dict[str, Any] = {
            "task_id": task_id,
            "spoken": "Which repo should the issue go in?" if asks else "Here is what I found; the details are on screen.",
            "details_markdown": None if asks else "| Item | State |\n|---|---|\n| stub | done |",
            "needs_user": asks,
        }
        await asyncio.sleep(REPORT_DELAY)
        yield ToolCall("report", args)
        await self._report(args)
        yield TurnEnd("completed")

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass


class Backend:
    def __init__(self, real: SdkBackend) -> None:
        self._real = real

    async def open(self, spec: SessionSpec) -> Any:
        return StubOrchestrator(spec) if spec.role == "orchestrator" else await self._real.open(spec)


class Clock:
    def now(self) -> float:
        import time

        return time.perf_counter()


class Rows:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.rows.append(entry)


class Screen:
    def heard(self, text: str) -> None:
        print(f"\n> {text}", file=sys.stderr)

    def reply(self, text: str) -> None:
        print(text, end="", file=sys.stderr, flush=True)

    def reply_end(self) -> None:
        print(file=sys.stderr)

    def notice(self, text: str) -> None:
        print(f"[hub] {text}", file=sys.stderr)

    def show(self, markdown: str) -> None:
        print(f"[show] {markdown[:60]!r}", file=sys.stderr)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert "ANTHROPIC_API_KEY" not in os.environ
    cli = shutil.which("claude")
    assert cli
    settings = load_settings(ROOT, os.environ)
    clock, rows = Clock(), Rows()
    hub = Hub(
        talker=replace(talker_spec(settings), model=args.model),
        orchestrator=orchestrator_spec(settings),
        backend=Backend(SdkBackend(cli)),
        clock=clock,
        output=Screen(),
        metrics=rows,
    )
    texts = LINES.read_text().splitlines()
    turn_line: list[int] = []

    async def typed() -> AsyncIterator[Line]:
        for n, text in enumerate(texts, 1):
            await hub._settled.wait()  # pyright: ignore[reportPrivateUsage]
            turn_line.append(n)
            yield Line(text, clock.now())
        await hub._settled.wait()  # pyright: ignore[reportPrivateUsage]

    await hub.start()
    try:
        await hub.run(typed())
    finally:
        await hub.close()

    turns: list[dict[str, Any]] = []
    line = 0
    for row in rows.rows:
        if row["kind"] == "user":
            line += 1
        if row["role"] != "talker" or row["kind"] == "warm_up":
            continue
        turns.append(
            {
                "line": line,
                "kind": row["kind"],
                "tools": row["tools"],
                "text_before_first_tool": None if not row["tools"] else not row["tool_before_text"],
                "first_delta_ms": row["first_delta_ms"],
                "first_sentence_ms": row["first_sentence_ms"],
                "outcome": row["outcome"],
            }
        )
    tool_turns = [t for t in turns if t["tools"]]
    result = {
        "talker_model": args.model,
        "effort": "low",
        "date": date.today().isoformat(),
        "claude_agent_sdk": version("claude-agent-sdk"),
        "stub_report_delay_s": REPORT_DELAY,
        "claude_cli": subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.split()[0],
        "tool_using_turns": len(tool_turns),
        "text_before_first_tool": sum(1 for t in tool_turns if t["text_before_first_tool"]),
        "turns": turns,
    }
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "turns"}), file=sys.stderr)


asyncio.run(main())
