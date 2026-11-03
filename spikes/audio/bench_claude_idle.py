"""Memory of three `claude` sessions that start, reach their prompt and never receive one, so no model is called."""

import argparse
import json
import os
import pty
import select
import signal
import statistics
import subprocess
import threading
import time
from pathlib import Path

from common import footprint_mb, rss_mb, write_result

SESSIONS = 3


def spawn(cwd: Path) -> tuple[int, int]:
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        # Markers inherited from a Claude Code session running this script change how the child session starts.
        for key in [k for k in os.environ if k.startswith(("CLAUDE_CODE", "CLAUDECODE"))]:
            del os.environ[key]
        os.execvp("claude", ["claude"])
    return pid, fd


def drain(fd: int, stop: threading.Event) -> None:
    """Read the session's screen output and discard it; a full pty would stall the session."""
    while not stop.is_set():
        if select.select([fd], [], [], 0.5)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                return


def signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def end(pid: int) -> None:
    """Stop a session and every process it started: SIGTERM first, SIGKILL for whatever is left five seconds later.

    pty.fork makes each session the leader of its own process group, which its MCP servers join.
    """
    signal_group(pid, signal.SIGTERM)
    for _ in range(50):
        if os.waitpid(pid, os.WNOHANG)[0]:
            break
        time.sleep(0.1)
    else:
        signal_group(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    signal_group(pid, signal.SIGKILL)


def idle_footprints(cwd: Path, settle_s: int) -> list[dict]:
    """Start SESSIONS sessions in `cwd`, leave them at the prompt for `settle_s` seconds and measure each one."""
    stop = threading.Event()
    sessions = []
    try:
        for _ in range(SESSIONS):
            sessions.append(spawn(cwd))
            threading.Thread(target=drain, args=(sessions[-1][1], stop), daemon=True).start()
        time.sleep(settle_s)
        return [{"footprint": footprint_mb(pid), "rss": round(rss_mb(pid))} for pid, _ in sessions]
    finally:
        # Draining continues until every session has exited: an exiting session blocks on a full pty.
        for pid, _ in sessions:
            end(pid)
        stop.set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", type=Path, required=True, help="a folder Claude Code already trusts")
    ap.add_argument("--settle-s", type=int, default=60)
    args = ap.parse_args()
    procs = idle_footprints(args.cwd.resolve(), args.settle_s)
    feet = [p["footprint"] for p in procs if p["footprint"]]
    result = {
        "claude_version": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip(),
        "sessions": SESSIONS, "settle_s": args.settle_s, "cwd": args.cwd.resolve().name, "processes": procs,
        "median_footprint_mb": statistics.median(feet) if feet else None,
        "three_idle_mb": round(sum(feet)) if len(feet) == SESSIONS else None,
    }
    print(json.dumps(result, indent=2))
    print(f"-> {write_result('claude-idle', result)}")


if __name__ == "__main__":
    main()
