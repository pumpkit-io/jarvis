#!/usr/bin/env python3
"""Claude Code hook for the Phase 0 baseline: logs a timestamp for every tool call. Throwaway."""

import json
import os
import sys
import time

payload = json.load(sys.stdin)
log = os.environ.get("JARVIS_BASELINE_LOG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks.jsonl")
with open(log, "a") as f:
    f.write(json.dumps({
        "t": time.time(),
        "event": payload.get("hook_event_name"),
        "tool": payload.get("tool_name"),
        "session": payload.get("session_id"),
    }) + "\n")
