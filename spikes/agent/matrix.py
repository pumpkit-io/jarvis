"""Phase 0: which effort and thinking settings each model accepts. Throwaway.

Run: env -u ANTHROPIC_API_KEY uv run python matrix.py [--runs N]
Appends N runs to results/matrix.json, so every run's raw output stays committed.
"""

import argparse
import asyncio
import json
import os

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ThinkingBlock, query

from common import SCRATCH_CWD, options, write_json

MODELS = ["haiku", "sonnet", "opus"]
EFFORTS = [None, "low", "medium", "high", "xhigh", "max"]
THINKING = [
    {"type": "disabled"},
    {"type": "adaptive"},
    {"type": "enabled", "budget_tokens": 2048},
]
PROMPT = "A train leaves at 09:47 and the trip takes 2 h 38 min. When does it arrive? Answer with the time only."


async def probe(model, effort, thinking, sem):
    stderr: list[str] = []
    async with sem:
        row = {"model": model, "effort": effort, "thinking": thinking and thinking["type"]}
        try:
            msgs = [m async for m in query(prompt=PROMPT, options=options(
                model=model, effort=effort, thinking=thinking, stderr=stderr.append))]
        except Exception as e:
            row.update(ok=False, error=repr(e)[:300], stderr=stderr[-5:])
            return row
        r = next(m for m in msgs if isinstance(m, ResultMessage))
        init = next(m.data for m in msgs if isinstance(m, SystemMessage) and m.subtype == "init")
        details = (r.usage or {}).get("output_tokens_details") or {}
        row.update(
            ok=not r.is_error,
            resolved_model=init.get("model"),
            reply=(r.result or "")[:40],
            thinking_tokens=details.get("thinking_tokens"),
            thinking_blocks=sum(isinstance(b, ThinkingBlock) for m in msgs if isinstance(m, AssistantMessage) for b in m.content),
            output_tokens=(r.usage or {}).get("output_tokens"),
            errors=r.errors,
            api_error_status=r.api_error_status,
            stderr=[s for s in stderr if s.strip()][-5:],
            duration_ms=r.duration_ms,
        )
        return row


async def one_run():
    sem = asyncio.Semaphore(4)
    jobs = []
    for model in MODELS:
        jobs += [probe(model, e, None, sem) for e in EFFORTS]
        jobs += [probe(model, None, t, sem) for t in THINKING]
        jobs.append(probe(model, "low", {"type": "disabled"}, sem))
    return await asyncio.gather(*jobs)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    n = ap.parse_args().runs
    path = os.path.join(SCRATCH_CWD, "results", "matrix.json")
    runs = json.load(open(path))["runs"] if os.path.exists(path) else []
    for _ in range(n):
        runs.append(await one_run())
        write_json("matrix.json", {"prompt": PROMPT, "runs": runs})


if __name__ == "__main__":
    asyncio.run(main())
