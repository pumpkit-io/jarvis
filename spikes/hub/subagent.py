"""One real writing Subagent through the Hub, on a scratch branch it commits to and never pushes.

The Talker and the Orchestrator are stubs, so the run costs only the Subagent's turns: the Talker delegates the one
request, the Orchestrator spawns the Subagent below and reports its completion event. Clean up the worktree and
branch afterwards with the sdlc `worktree.sh finish`.

    env -u ANTHROPIC_API_KEY -u VIRTUAL_ENV uv run python spikes/hub/subagent.py --repo animate-pixel-art
"""

import argparse
import asyncio
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jarvis_hub.agents.backend import AgentEvent, SessionSpec, TextDelta, ToolCall, TurnEnd
from jarvis_hub.agents.sdk import SdkBackend
from jarvis_hub.config import load_settings
from jarvis_hub.hub import Hub, Line
from jarvis_hub.main import JsonlMetrics, subagent_setup
from jarvis_hub.orchestrator import orchestrator_spec
from jarvis_hub.talker import talker_spec

ROOT = Path(__file__).resolve().parents[2]
BRIEF = (
    "This is a harmless check of the Hub's Subagents. Create `jarvis-scratch.txt` at the repo root with the single "
    "line `Scratch check by a Jarvis Subagent; safe to delete.`, and commit it on your branch with the message "
    "`chore: add a scratch file for the Jarvis Subagent check`. Then stop: do not push, do not open a PR, and change "
    "nothing else. Report the commit you made."
)


class StubSession:
    def __init__(self, spec: SessionSpec) -> None:
        self._tools = {t.name: t.handler for t in spec.tools}
        self._role = spec.role
        self.results: list[tuple[str, str]] = []

    async def send(self, message: str) -> AsyncIterator[AgentEvent]:
        call: tuple[str, dict[str, Any]] | None = None
        if self._role == "talker" and message.startswith("<user"):
            call = ("delegate", {"request": "Add a scratch file in the repo and commit it, without pushing."})
        elif self._role == "orchestrator" and message.startswith("<task"):
            call = ("spawn_subagent", {**SPAWN, "brief": BRIEF})
        elif self._role == "orchestrator":
            task_id = message.split('task="')[1].split('"')[0]
            call = ("report", {"task_id": task_id, "spoken": "The scratch check ended.", "details_markdown": message})
        if call is not None:
            yield ToolCall(*call)
            self.results.append((call[0], await self._tools[call[0]](call[1])))
        yield TextDelta("OK.")
        yield TurnEnd("completed")

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        pass


class Backend:
    def __init__(self, real: SdkBackend) -> None:
        self._real = real
        self.stubs: list[StubSession] = []

    async def open(self, spec: SessionSpec) -> Any:
        if spec.role == "subagent":
            return await self._real.open(spec)
        stub = StubSession(spec)
        self.stubs.append(stub)
        return stub


class Clock:
    def now(self) -> float:
        return time.perf_counter()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class Screen:
    def heard(self, text: str) -> None:
        print(f"> {text}")

    def reply(self, text: str) -> None:
        pass

    def reply_end(self) -> None:
        pass

    def notice(self, text: str) -> None:
        print(f"[hub] {text}")

    def show(self, markdown: str) -> None:
        print(f"[screen]\n{markdown}")


SPAWN: dict[str, Any] = {}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--effort", default="low")
    args = parser.parse_args()
    assert "ANTHROPIC_API_KEY" not in os.environ
    cli = shutil.which("claude")
    assert cli
    SPAWN.update(
        repo=args.repo, title="Jarvis scratch check", model=args.model, effort=args.effort, writes=True, branch_kind="chore"
    )
    settings = load_settings(ROOT, os.environ)
    backend = Backend(SdkBackend(cli))
    hub = Hub(
        talker=talker_spec(settings),
        orchestrator=orchestrator_spec(settings),
        backend=backend,
        clock=Clock(),
        output=Screen(),
        metrics=JsonlMetrics(settings.runtime_dir / "metrics.jsonl"),
        subagents=subagent_setup(settings),
    )

    async def typed() -> AsyncIterator[Line]:
        yield Line("Add a scratch file and commit it.", time.perf_counter())

    started = time.perf_counter()
    try:
        await hub.start()
        await hub.run(typed())
    finally:
        await hub.close()
    for stub in backend.stubs:
        for name, result in stub.results:
            print(f"[{name}] {result}")
    print(json.dumps({"seconds": round(time.perf_counter() - started, 1)}))


if __name__ == "__main__":
    asyncio.run(main())
