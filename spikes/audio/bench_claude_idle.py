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


def end(pid: int) -> None:
    """Stop a session: SIGTERM first, SIGKILL if it is still there five seconds later."""
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", type=Path, required=True, help="a folder Claude Code already trusts")
    ap.add_argument("--settle-s", type=int, default=60)
    args = ap.parse_args()
    stop = threading.Event()
    sessions = [spawn(args.cwd.resolve()) for _ in range(SESSIONS)]
    for _, fd in sessions:
        threading.Thread(target=drain, args=(fd, stop), daemon=True).start()
    try:
        time.sleep(args.settle_s)
        procs = [{"footprint": footprint_mb(pid), "rss": round(rss_mb(pid))} for pid, _ in sessions]
    finally:
        # Draining continues until every session has exited: an exiting session blocks on a full pty.
        for pid, _ in sessions:
            end(pid)
        stop.set()
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
