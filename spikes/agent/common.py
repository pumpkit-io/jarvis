"""Shared setup for the Phase 0 Agent SDK spikes. Throwaway, not Hub code."""

import os
import shutil
import sys

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions

# The run must bill the Max subscription, never the API (ADR 0002).
os.environ.pop("ANTHROPIC_API_KEY", None)

INSTALLED_CLI = shutil.which("claude")
if INSTALLED_CLI is None:
    sys.exit("claude CLI not on PATH")

SCRATCH_CWD = os.path.dirname(os.path.abspath(__file__))


def options(**overrides) -> ClaudeAgentOptions:
    base = dict(
        cli_path=INSTALLED_CLI,
        cwd=SCRATCH_CWD,
        setting_sources=[],
        tools=[],
        system_prompt="You are a terse test assistant.",
        max_turns=1,
    )
    base.update(overrides)
    return ClaudeAgentOptions(**base)


def versions() -> str:
    return f"claude-agent-sdk {claude_agent_sdk.__version__}, CLI {INSTALLED_CLI}"
