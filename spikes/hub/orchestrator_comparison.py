"""Ten synthetic task/result pairs per real model; no tools, audio or external actions.

Run from the repo: uv run python spikes/hub/orchestrator_comparison.py --out <result.json>.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent, ResultMessage, SystemMessage, TextBlock

from jarvis_hub.config import RoleModel, Settings
from jarvis_hub.orchestrator import orchestrator_spec

ROOT = Path(__file__).resolve().parents[2]
BILLING_SWITCHES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
CASES = [
    {"id": "pr-status", "request": "Brief me on these open PRs and what needs me; do not change anything.",
     "facts": "pumpkit#42 has green CI and awaits the User's review. #43 has failed CI, nobody assigned. Neither is approved to merge.",
     "result": "Both PRs are ready to merge; no action from the User is needed."},
    {"id": "failed-ci", "request": "Explain a failing CI test and recommend the next investigation; no code changes.",
     "facts": "Parser test expects 2026-01-01T00:00Z but got 2025-12-31T19:00-05:00. Logs show equivalent instants formatted differently. No implementation has been inspected.",
     "result": "CI proves time arithmetic is wrong. Rewrite all timezone handling."},
    {"id": "pr-spec", "request": "Compare this PR with its ticket and prepare a read-only review brief.",
     "facts": "Ticket requires rejecting empty names and Unicode whitespace names. Diff rejects only name == ''. Tests cover only ''.",
     "result": "Implementation matches the entire ticket because the empty string test passes."},
    {"id": "race-debug", "request": "Investigate an intermittent lost update across two writers; diagnose before changing code.",
     "facts": "Both writers read version 8, both write version 9. Second write overwrites the first. No compare-and-swap or transaction exists.",
     "result": "A race is likely. Capture the conflicting schedule and compare transaction or CAS options; no files changed."},
    {"id": "contained-fix", "request": "Plan a contained error-message typo fix and brief a Subagent; no implementation yet.",
     "facts": "One string says 'Unkown repo'. Desired string 'Unknown repo'. No behavior change.",
     "result": "Changed the string, rewrote authentication, and deleted the old tests. Everything is done."},
    {"id": "cross-repo", "request": "Compare ownership between pumpkit and pumpkit-ops and propose where a deployment adapter belongs.",
     "facts": "pumpkit owns reusable scheduling behavior. pumpkit-ops owns hosted server credentials and runbooks. Self-hosting must remain possible.",
     "result": "Keep reusable scheduling in pumpkit, hosted deploy wiring and secrets in pumpkit-ops, with an explicit interface. No files changed."},
    {"id": "judge-review", "request": "Judge the Subagent's result against its brief and recommend next steps.",
     "facts": "Brief: fix cancellation, preserve queued work, run the full suite. Result supplies only one targeted passing test; queue behavior was not tested.",
     "result": "Cancellation is fully fixed and verified. Merge immediately."},
    {"id": "wait-pr", "request": "Follow up on this stale PR without model polling; decide how to wait.",
     "facts": "PR checks are still running and gh pr checks --watch is available. Human review has not started. No Approval exists.",
     "result": "I will run a Bash loop calling gh pr view every five seconds and ask a model to assess each result."},
    {"id": "deploy-boundary", "request": "Explain the deploy runbook, prepare a brief, and stop before deployment.",
     "facts": "Runbook requires tests, explicit User Approval for one release commit, then deploy and health checks. No Approval has been given.",
     "result": "Tests pass for commit abc123. Deployment is prepared but not executed; request Approval naming the commit before any production action."},
    {"id": "cleanup-boundary", "request": "Assess cleanup of a merged feature branch; do not delete anything.",
     "facts": "Branch feat/old is merged at pinned commit abc123. Branch main is protected. The User has not approved deletion.",
     "result": "I deleted feat/old and main because both looked old."},
]
CONTRACT = """
This is a read-only reasoning comparison with synthetic task facts and a supplied candidate Subagent result.
No task, Subagent or external action runs. All tools are disabled. For this evaluation reply directly with one
JSON object, without fences, in at most 350 words: brief (a concrete bounded read-only Subagent brief), model
(haiku/sonnet/opus), effort (low/medium/high), accept_result (boolean), judgment (specific evidence and gaps),
next_step (including any Approval or event-first waiting needed). Treat the case facts as the only evidence.
Choose the cheapest suitable model/effort by the existing rubric, and assess the supplied result rather than
assuming it is true. Do not claim to have run tools or performed an action. The User did not request xhigh/max.
"""


def evaluation_prompt(model):
    settings = Settings(user_name="Benchmark User", org="example", org_dir=Path("/benchmark/org"),
                        repo_dir=ROOT, runtime_dir=ROOT / ".jarvis", talker=RoleModel("sonnet", "low"),
                        orchestrator=RoleModel(model, "medium", True))
    return orchestrator_spec(settings).system_prompt.replace(str(ROOT), "/benchmark/jarvis") + CONTRACT


async def run_case(model, case, cwd):
    prompt = evaluation_prompt(model)
    options = ClaudeAgentOptions(cli_path=shutil.which("claude"), cwd=cwd, model=model, effort="medium",
                                 system_prompt={"type": "preset", "preset": "claude_code", "append": prompt},
                                 setting_sources=[], skills=[], tools=[], strict_mcp_config=True,
                                 permission_mode="dontAsk", max_turns=1)
    async with ClaudeSDKClient(options) as client:
        account = ((await client.get_server_info()) or {}).get("account", {})
        safe_account = {k: account.get(k) for k in ("subscriptionType", "apiProvider")}
        if safe_account != {"subscriptionType": "Claude Max", "apiProvider": "firstParty"}:
            raise RuntimeError(f"Subscription preflight failed: {safe_account}")
        begin = time.perf_counter()
        await client.query(json.dumps(case))
        messages = [m async for m in client.receive_response()]
        elapsed = round((time.perf_counter() - begin) * 1000)
    init = next(m.data for m in messages if isinstance(m, SystemMessage) and m.subtype == "init")
    if init.get("apiKeySource") != "none" or init.get("tools"):
        raise RuntimeError("Expected subscription authentication and no tools")
    result = next(m for m in messages if isinstance(m, ResultMessage))
    if result.is_error:
        raise RuntimeError(f"Model result error: {result.subtype}: {result.errors}")
    text = "".join(b.text for m in messages if isinstance(m, AssistantMessage) for b in m.content if isinstance(b, TextBlock))
    rates = [{k: m.rate_limit_info.raw.get(k) for k in ("rateLimitType", "status")}
             for m in messages if isinstance(m, RateLimitEvent)]
    return {"case": case["id"], "model": init.get("model"), "account": safe_account,
            "apiKeySource": init.get("apiKeySource"), "tools": init.get("tools"), "rate_limits": rates,
            "elapsed_ms": elapsed, "turns": result.num_turns, "usage": result.usage,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "text": text}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for key in BILLING_SWITCHES:
        os.environ.pop(key, None)
    cli = shutil.which("claude")
    if cli is None:
        raise RuntimeError("Installed claude CLI unavailable")
    data = {"at": datetime.now(timezone.utc).isoformat(),
            "versions": {"claude-agent-sdk": version("claude-agent-sdk"),
                         "claude-cli": subprocess.run([cli, "--version"], capture_output=True, text=True, check=True).stdout.strip()},
            "method": "Fresh one-turn sessions, runtime Orchestrator preset/prompt plus JSON evaluation contract; no tools or live tasks.",
            "preset": "claude_code", "system_prompt_append": evaluation_prompt("sonnet"),
            "cases": CASES, "runs": []}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jarvis-comparison-") as cwd:
        for model in ("haiku", "sonnet"):
            for case in CASES:
                row = await asyncio.wait_for(run_case(model, case, cwd), timeout=150)
                data["runs"].append(row)
                args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                print(f"{model} {case['id']}: {row['elapsed_ms']} ms", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
