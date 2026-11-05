import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
REPORT = f"""## sdlc review of {HEAD}

## Standards
No findings.

## Spec
No findings.

## Correctness
No findings.

Verdict: pass
"""


def test_successful_action_emits_report_without_agent_file_write(tmp_path: Path) -> None:
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps([
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "intermediate private trace"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": REPORT},
    ]))
    report, output = tmp_path / "review.md", tmp_path / "github-output"
    completed = subprocess.run([
        sys.executable, str(ROOT / ".github/scripts/review_report.py"),
        "--execution-file", str(execution), "--head", HEAD,
        "--report", str(report), "--github-output", str(output),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert report.read_text() == REPORT
    assert output.read_text() == "verdict=pass\n"
    assert "private trace" not in completed.stdout + completed.stderr + report.read_text()


def test_blocking_report_and_verdict_are_emitted_without_failing_conversion(tmp_path: Path) -> None:
    body = REPORT.replace("No findings.", "One likely bug.", 1).replace("Verdict: pass", "Verdict: blocking (1 hard findings)")
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": body}]))
    report, output = tmp_path / "review.md", tmp_path / "github-output"
    completed = subprocess.run([
        sys.executable, str(ROOT / ".github/scripts/review_report.py"),
        "--execution-file", str(execution), "--head", HEAD,
        "--report", str(report), "--github-output", str(output),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert report.read_text() == body
    assert output.read_text() == "verdict=blocking\n"


@pytest.mark.parametrize("artifact", [
    "not JSON: PRIVATE_TRACE",
    "[]",
    json.dumps({"type": "result", "result": REPORT}),
    json.dumps([{"type": "assistant", "message": {"content": [{"type": "text", "text": REPORT}]}}]),
    json.dumps([{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": REPORT}]),
    json.dumps([{"type": "result", "subtype": "success", "is_error": True, "result": REPORT}]),
    json.dumps([{"type": "result", "subtype": "success", "result": REPORT}]),
    json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": None}]),
    *[json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": body}]) for body in [
        "", "\ud800", REPORT.replace(HEAD, "b" * 40), REPORT.replace("## Spec\nNo findings.\n", ""),
        REPORT.replace("## Spec\nNo findings.", "## Spec"),
        REPORT.replace("## Spec", "## Standards"),
        REPORT.replace("Verdict: pass", "Verdict: pass\nVerdict: blocking (1 hard findings)"),
        REPORT + "Trailing text\n", REPORT.replace("Verdict: pass", "Verdict: blocking (0 hard findings)"),
        REPORT.replace("Verdict: pass", "Verdict: unclear"),
    ]],
    json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": REPORT}] * 2),
    json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": REPORT}, {"type": "assistant"}]),
])
def test_invalid_execution_never_publishes_or_reuses_a_stale_report(tmp_path: Path, artifact: str) -> None:
    execution = tmp_path / "execution.json"
    execution.write_text(artifact)
    report, output = tmp_path / "review.md", tmp_path / "github-output"
    report.write_text(REPORT)
    completed = subprocess.run([
        sys.executable, str(ROOT / ".github/scripts/review_report.py"),
        "--execution-file", str(execution), "--head", HEAD,
        "--report", str(report), "--github-output", str(output),
    ], capture_output=True, text=True)
    assert completed.returncode != 0
    assert not report.exists()
    assert not output.exists()
    assert "Review output rejected:" in completed.stderr
    assert "PRIVATE_TRACE" not in completed.stdout + completed.stderr
    assert "Traceback" not in completed.stderr


def test_missing_execution_file_fails_with_a_nonsecret_error(tmp_path: Path) -> None:
    report = tmp_path / "review.md"
    completed = subprocess.run([
        sys.executable, str(ROOT / ".github/scripts/review_report.py"),
        "--execution-file", "", "--head", HEAD,
        "--report", str(report), "--github-output", str(tmp_path / "output"),
    ], capture_output=True, text=True)
    assert completed.returncode != 0
    assert not report.exists()
    assert completed.stderr.startswith("Review output rejected:")
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(("body", "diagnostics"), [
    ("PRIVATE_SECRET\n" + REPORT,
     "expected_heading_elsewhere=true first_line_heading_like=false outer_fence=false axes_present=3"),
    (REPORT.replace(HEAD, "b" * 40),
     "expected_heading_elsewhere=false first_line_heading_like=true outer_fence=false axes_present=3"),
    ("PRIVATE_SECRET\nNothing here.\n",
     "expected_heading_elsewhere=false first_line_heading_like=false outer_fence=false axes_present=0"),
    ("```markdown\n" + REPORT + "```\n",
     "expected_heading_elsewhere=true first_line_heading_like=false outer_fence=true axes_present=3"),
])
def test_heading_rejection_exposes_only_safe_format_diagnostics(tmp_path: Path, body: str, diagnostics: str) -> None:
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": body}]))
    report, output = tmp_path / "review.md", tmp_path / "github-output"
    completed = subprocess.run([
        sys.executable, str(ROOT / ".github/scripts/review_report.py"),
        "--execution-file", str(execution), "--head", HEAD,
        "--report", str(report), "--github-output", str(output),
    ], capture_output=True, text=True)
    assert completed.returncode == 1
    assert not report.exists() and not output.exists()
    assert completed.stderr == f"Review output rejected: review heading does not match the expected PR head; {diagnostics}\n"
    assert "PRIVATE_SECRET" not in completed.stdout + completed.stderr
    assert HEAD not in completed.stdout + completed.stderr
    assert "b" * 40 not in completed.stdout + completed.stderr
