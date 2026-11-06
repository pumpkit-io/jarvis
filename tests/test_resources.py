from collections.abc import Callable
from pathlib import Path

import pytest

from jarvis_hub.resources import ProcessSample, sample_resources


class Clock:
    def __init__(self) -> None:
        self.time = 0.0

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.time += seconds


def snapshots(*rows: list[ProcessSample]) -> Callable[[], list[ProcessSample]]:
    remaining = iter(rows)
    return lambda: next(remaining)


def test_observer_reports_simultaneous_tree_peak_and_only_observed_interval_cpu() -> None:
    clock = Clock()
    report = sample_resources(
        10, duration_s=2, interval_s=1, now=clock.now, sleep=clock.sleep,
        snapshot=snapshots(
            [ProcessSample(10, 1, "root", 100, 5)],
            [ProcessSample(10, 1, "root", 100, 6), ProcessSample(20, 10, "child", 150, 8)],
            [ProcessSample(10, 1, "root", 200, 7), ProcessSample(20, 10, "child", 10, 8.5)],
        ),
    )
    assert report["sampled_peak_tree_rss_bytes"] == 250
    assert report["sampled_peak_root_rss_bytes"] == 200
    assert report["observed_interval_cpu_s"] == 2.5
    assert report["cpu_s_at_first_observation"] == 13
    assert report["samples"][1]["interval_cpu_percent_one_core"] == 100
    assert report["samples"][2]["interval_cpu_percent_one_core"] == 150
    assert report["startup_included"] is False
    assert report["observed_mean_cpu_percent_one_core"] == 125
    assert report["cpu_observation_window_s"] == 2


def test_known_children_survive_reparenting_but_reused_root_pid_does_not_rejoin() -> None:
    clock = Clock()
    report = sample_resources(
        10, duration_s=2, interval_s=1, now=clock.now, sleep=clock.sleep,
        snapshot=snapshots(
            [ProcessSample(10, 1, "root", 100, 0), ProcessSample(20, 10, "child", 50, 1)],
            [ProcessSample(10, 1, "unrelated", 999, 99), ProcessSample(20, 1, "child", 60, 2)],
            [ProcessSample(20, 1, "child", 70, 3), ProcessSample(30, 20, "grandchild", 20, 4)],
        ),
    )
    assert report["sampled_peak_tree_rss_bytes"] == 150
    assert report["observed_processes"] == 3
    assert [row["root_present"] for row in report["samples"]] == [True, False, False]
    assert report["samples"][1]["tree_rss_bytes"] == 60
    assert report["samples"][2]["tree_rss_bytes"] == 90


@pytest.mark.parametrize(("duration", "interval"), [
    (0, 1), (float("nan"), 1), (float("inf"), 1), (3601, 1),
    (1, 0), (1, 0.01), (1, float("nan")), (1, 2), (3600, 0.1),
])
def test_invalid_or_excessive_sampling_is_rejected_before_reading_processes(duration: float, interval: float) -> None:
    def unexpected() -> list[ProcessSample]:
        pytest.fail("invalid request must not read system processes")

    with pytest.raises(ValueError, match="duration|interval|samples"):
        sample_resources(10, duration_s=duration, interval_s=interval, snapshot=unexpected)


def test_ps_snapshot_decodes_rss_and_cumulative_cpu_without_command_lines() -> None:
    from jarvis_hub.resources import parse_process_snapshot

    rows = parse_process_snapshot(
        " 10 1 Fri Nov 6 11:22:33 2026 1024 3:04.50\n"
        " 20 10 Fri Nov 6 11:22:34 2026 2048 1-02:03:04\n"
    )
    assert rows == [
        ProcessSample(10, 1, "Fri Nov 6 11:22:33 2026", 1048576, 184.5),
        ProcessSample(20, 10, "Fri Nov 6 11:22:34 2026", 2097152, 93784),
    ]


@pytest.mark.parametrize("text", ["", "PRIVATE_PROCESS_ARGS", "10 1 Fri Nov 6 11:22:33 2026 -1 3:04.50"])
def test_failed_or_malformed_ps_snapshot_is_not_reported_as_zero_usage(text: str) -> None:
    from jarvis_hub.resources import parse_process_snapshot

    with pytest.raises(ValueError, match="process snapshot") as error:
        parse_process_snapshot(text)
    assert "PRIVATE_PROCESS_ARGS" not in str(error.value)


def test_cli_observes_only_harmless_process_and_writes_accounting_metadata(tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    output = tmp_path / "resources.json"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        completed = subprocess.run([
            sys.executable, "-m", "jarvis_hub.resources", "--pid", str(child.pid),
            "--duration", "0.1", "--interval", "0.1", "--workload", "synthetic", "--output", str(output),
        ], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "src"}, timeout=5)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(output.read_text())
        assert result["root_pid"] == child.pid
        assert len(result["samples"]) >= 1
        assert result["duration_s"] >= 0.1
        assert result["tools"]["ps_fields"] == "pid,ppid,lstart,rss,time"
        assert result["workload"] == "synthetic"
        assert result["startup_included"] is False
        assert "sampled RSS" in result["accounting"]["memory"]
        assert "command" not in result["samples"][0]["processes"][0]
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_excessive_process_records_fail_instead_of_growing_report_without_bound() -> None:
    clock = Clock()
    many = [ProcessSample(10, 1, "root", 100, 0)] + [
        ProcessSample(pid, 10, "child", 1, 0) for pid in range(20, 100021)
    ]
    with pytest.raises(ValueError, match="process records"):
        sample_resources(10, duration_s=1, interval_s=1, snapshot=lambda: many, now=clock.now, sleep=clock.sleep)


def test_new_and_departed_processes_make_cpu_coverage_explicit() -> None:
    clock = Clock()
    report = sample_resources(
        10, duration_s=2, interval_s=1, now=clock.now, sleep=clock.sleep,
        snapshot=snapshots(
            [ProcessSample(10, 1, "root", 100, 0)],
            [ProcessSample(10, 1, "root", 100, 0.2), ProcessSample(20, 10, "child", 100, 2)],
            [ProcessSample(10, 1, "root", 100, 0.3)],
        ),
    )
    assert report["samples"][0]["interval_membership_unchanged"] is None
    assert report["samples"][1]["interval_membership_unchanged"] is False
    assert report["samples"][2]["interval_membership_unchanged"] is False
    assert report["samples"][2]["departed_processes"] == 1
    assert report["cpu_s_at_first_observation"] == 2
    assert report["observed_interval_cpu_s"] == pytest.approx(0.3)


def test_missing_root_and_regressing_cpu_fail_without_inventing_usage() -> None:
    clock = Clock()
    with pytest.raises(ValueError, match="absent"):
        sample_resources(10, duration_s=1, interval_s=1, snapshot=lambda: [], now=clock.now, sleep=clock.sleep)
    with pytest.raises(ValueError, match="CPU counter"):
        sample_resources(
            10, duration_s=1, interval_s=1, now=clock.now, sleep=clock.sleep,
            snapshot=snapshots([ProcessSample(10, 1, "root", 100, 2)], [ProcessSample(10, 1, "root", 100, 1)]),
        )


def test_cli_failed_system_snapshot_leaves_no_report_or_private_tool_output(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    tool = tmp_path / "ps"
    tool.write_text("#!/bin/sh\nprintf 'PRIVATE_PROCESS_DETAILS\\n' >&2\nexit 1\n")
    tool.chmod(0o700)
    output = tmp_path / "result.json"
    completed = subprocess.run([
        sys.executable, "-m", "jarvis_hub.resources", "--pid", "10", "--duration", "0.1", "--interval", "0.1",
        "--workload", "synthetic", "--output", str(output),
    ], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "src", "PATH": str(tmp_path)}, timeout=5)
    assert completed.returncode == 1
    assert not output.exists()
    assert "could not sample system processes" in completed.stderr
    assert "PRIVATE_PROCESS_DETAILS" not in completed.stderr + completed.stdout
