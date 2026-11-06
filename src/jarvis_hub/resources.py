"""External process-tree observations; importing this module never initializes the Hub or audio."""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any

@dataclass(frozen=True)
class ProcessSample:
    pid: int
    ppid: int
    started: str
    rss_bytes: int
    cpu_s: float

    @property
    def identity(self) -> tuple[int, str]:
        return self.pid, self.started


def sample_resources(
    pid: int, *, duration_s: float, interval_s: float,
    snapshot: Callable[[], list[ProcessSample]],
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not math.isfinite(duration_s) or not 0.1 <= duration_s <= 3600:
        raise ValueError("duration must be between 0.1 and 3600 seconds")
    if not math.isfinite(interval_s) or not 0.1 <= interval_s <= duration_s:
        raise ValueError("interval must be at least 0.1 seconds and no longer than duration")
    if math.ceil(duration_s / interval_s) + 1 > 10_001:
        raise ValueError("requested observation exceeds 10001 samples")
    if pid <= 0:
        raise ValueError("PID must be positive")
    began = now()
    known: set[tuple[int, str]] = set()
    previous: dict[tuple[int, str], ProcessSample] = {}
    root: tuple[int, str] | None = None
    previous_at = began
    samples: list[dict[str, Any]] = []
    first_cpu = interval_cpu = 0.0
    process_records = 0
    while True:
        sample_began = now()
        processes = snapshot()
        at = now()
        if root is None:
            match = next((process for process in processes if process.pid == pid), None)
            if match is None:
                raise ValueError("the requested Hub PID is absent from the initial snapshot")
            root = match.identity
            known.add(root)
        selected = {process.identity: process for process in processes if process.identity in known}
        while True:
            parents = {process.pid for process in selected.values()}
            descendants = {process.identity: process for process in processes if process.ppid in parents}
            additional = descendants.keys() - selected.keys()
            if not additional:
                break
            selected.update(descendants)
        process_records += len(selected)
        if process_records > 100_000:
            raise ValueError("observation exceeds 100000 process records; shorten duration or increase interval")
        introduced = selected.keys() - known
        new_cpu = sum(selected[identity].cpu_s for identity in introduced)
        if not samples:
            new_cpu += sum(process.cpu_s for process in selected.values() if process.identity == root)
        first_cpu += new_cpu
        known.update(selected)
        shared = selected.keys() & previous.keys()
        if any(selected[identity].cpu_s < previous[identity].cpu_s for identity in shared):
            raise ValueError("CPU counter regressed for an unchanged process identity; retry the observation")
        cpu_delta = sum(selected[identity].cpu_s - previous[identity].cpu_s for identity in shared)
        interval_cpu += cpu_delta
        elapsed = at - previous_at
        root_rss = selected[root].rss_bytes if root in selected else 0
        child_rss = sum(process.rss_bytes for identity, process in selected.items() if identity != root)
        samples.append({
            "elapsed_s": at - began, "snapshot_duration_s": at - sample_began,
            "root_present": root in selected,
            "root_rss_bytes": root_rss, "children_rss_bytes": child_rss,
            "tree_rss_bytes": root_rss + child_rss,
            "interval_s": elapsed if samples else None,
            "interval_membership_unchanged": selected.keys() == previous.keys() if samples else None,
            "interval_cpu_s": cpu_delta if samples else None,
            "interval_cpu_percent_one_core": 100 * cpu_delta / elapsed if samples and elapsed > 0 else None,
            "new_process_cpu_s_at_first_observation": new_cpu,
            "departed_processes": len(previous.keys() - selected.keys()),
            "processes": [asdict(process) for process in selected.values()],
        })
        previous, previous_at = selected, at
        remaining = duration_s - (at - began)
        if remaining <= 0:
            break
        sleep(min(remaining, max(0.0, interval_s - (at - sample_began))))
    cpu_window = previous_at - began - samples[0]["elapsed_s"]
    return {
        "schema": "jarvis-resource-observation-v1", "root_pid": pid,
        "duration_s": previous_at - began, "requested_duration_s": duration_s,
        "requested_interval_s": interval_s, "startup_included": False,
        "sampled_peak_tree_rss_bytes": max(sample["tree_rss_bytes"] for sample in samples),
        "sampled_peak_root_rss_bytes": max(sample["root_rss_bytes"] for sample in samples),
        "observed_interval_cpu_s": interval_cpu, "cpu_s_at_first_observation": first_cpu,
        "cpu_observation_window_s": cpu_window,
        "observed_mean_cpu_percent_one_core": 100 * interval_cpu / cpu_window if cpu_window > 0 else None,
        "observed_processes": len(known), "samples": samples,
    }


def parse_process_snapshot(text: str) -> list[ProcessSample]:
    """Parse only numeric accounting and start identity, never commands or arguments."""
    rows: list[ProcessSample] = []
    try:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 9:
                raise ValueError
            pid, ppid = int(fields[0]), int(fields[1])
            rss = int(fields[7]) * 1024
            cpu = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", fields[8])
            if cpu is None or pid <= 0 or ppid < 0 or rss < 0:
                raise ValueError
            days, hours, minutes, seconds = (float(value or 0) for value in cpu.groups())
            if seconds >= 60 or (hours and minutes >= 60):
                raise ValueError
            rows.append(ProcessSample(pid, ppid, " ".join(fields[2:7]), rss,
                                      days * 86400 + hours * 3600 + minutes * 60 + seconds))
        if not rows or len({row.pid for row in rows}) != len(rows):
            raise ValueError
    except ValueError:
        raise ValueError("invalid or empty process snapshot; verify the system ps supports pid,ppid,lstart,rss,time") from None
    return rows


PS_FIELDS = "pid,ppid,lstart,rss,time"


def system_snapshot() -> list[ProcessSample]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,rss=,time="],
            capture_output=True, text=True, timeout=5, check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("could not sample system processes; verify ps is available and permitted") from None
    return parse_process_snapshot(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="actual Hub PID, not the uv or shell parent")
    parser.add_argument("--duration", type=float, default=60, help="seconds, 0.1 to 3600")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds, at least 0.1; at most 10001 samples")
    parser.add_argument("--workload", required=True, choices=("idle", "interactive", "synthetic"))
    parser.add_argument("--output", type=Path, required=True, help="new local JSON path, normally under .jarvis/resources")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output already exists; choose a new run path")
        started = datetime.now(timezone.utc).isoformat()
        report = sample_resources(args.pid, duration_s=args.duration, interval_s=args.interval, snapshot=system_snapshot)
        report.update({
            "started_at": started, "workload": args.workload,
            "tools": {"python": platform.python_version(), "os": platform.platform(),
                      "machine": platform.machine(), "ps": shutil.which("ps"), "ps_fields": PS_FIELDS},
            "accounting": {
                "memory": "simultaneous sampled RSS sum; shared pages may be counted repeatedly; not phys_footprint or an audio budget verdict",
                "cpu": "lower bound: successive observations of the same process identity only; 100 percent is one core; first-seen lifetime CPU is separate",
                "lifecycle": "known descendants remain after reparenting; processes that start and exit between samples are unobserved; ps traversal is not atomic",
                "identity": "PID plus ps lstart (one-second resolution); same-second PID reuse cannot be distinguished",
                "excluded": "startup before attachment, unobserved child lifetimes, Metal/cache and physical-footprint totals",
            },
        })
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
    except (ValueError, OSError) as error:
        print(f"Resource observation failed: {error}", file=sys.stderr)
        return 1
    print(f"Resource observation written to {args.output}; sampled RSS only, startup excluded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
