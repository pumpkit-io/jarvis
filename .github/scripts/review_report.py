"""Convert the action's terminal result into the only publishable review body."""

import argparse
import json
from pathlib import Path
import re
import sys


class InvalidReview(ValueError):
    pass


def final_report(execution: str, head: str) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise InvalidReview("expected head must be a full commit SHA")
    if not execution:
        raise InvalidReview("the action did not supply an execution file")
    try:
        with Path(execution).open("rb") as source:
            encoded = source.read(20_000_001)
        if len(encoded) > 20_000_000:
            raise InvalidReview("execution artifact exceeds 20 MB")
        messages = json.loads(encoded)
    except (OSError, ValueError, UnicodeError) as error:
        if isinstance(error, InvalidReview):
            raise
        raise InvalidReview("execution artifact is missing or invalid JSON") from None
    if not isinstance(messages, list) or not messages or any(not isinstance(item, dict) for item in messages):
        raise InvalidReview("execution artifact must be a nonempty JSON event array")
    results = [item for item in messages if item.get("type") == "result"]
    if len(results) != 1 or messages[-1] is not results[0]:
        raise InvalidReview("exactly one terminal result is required")
    result = results[0]
    if result.get("subtype") != "success" or result.get("is_error") is not False:
        raise InvalidReview("the terminal result did not complete successfully")
    body = result.get("result")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 60_000:
        raise InvalidReview("terminal result must contain a nonempty Markdown report under 60 KB")
    lines = body.strip().splitlines()
    expected_heading = f"## sdlc review of {head}"
    heading_matches = [index for index, line in enumerate(lines) if line == expected_heading]
    prefix = lines[:heading_matches[0]] if len(heading_matches) == 1 else lines
    forbidden_prefix = any(
        re.match(r"^\s*(?:#{1,6}(?:\s|$)|`{3,}|~{3,}|Verdict:)", line)
        or re.fullmatch(r"\s*(?:=+|-+)\s*", line)
        for line in prefix
    )
    if len(heading_matches) != 1 or sum(line.strip() == expected_heading for line in lines) != 1 or forbidden_prefix:
        diagnostics = {
            "expected_heading_elsewhere": expected_heading in lines[1:],
            "first_line_heading_like": bool(re.match(r"^#{1,6}\s+", lines[0])),
            "outer_fence": bool(re.match(r"^(?:`{3,}|~{3,})", lines[0])),
        }
        summary = " ".join(f"{name}={str(value).lower()}" for name, value in diagnostics.items())
        axes = sum(section in lines for section in ("## Standards", "## Spec", "## Correctness"))
        raise InvalidReview(f"review heading does not match the expected PR head; {summary} axes_present={axes}")
    positions: list[int] = []
    for section in ("## Standards", "## Spec", "## Correctness"):
        matches = [index for index, line in enumerate(lines) if line == section]
        if len(matches) != 1:
            raise InvalidReview("one Standards, Spec and Correctness section is required")
        positions.append(matches[0])
    if positions != sorted(positions):
        raise InvalidReview("review axes must appear in Standards, Spec, Correctness order")
    verdicts = [line for line in lines if line.lstrip().startswith("Verdict:")]
    if len(verdicts) != 1 or verdicts[0] != lines[-1]:
        raise InvalidReview("exactly one final verdict line is required")
    verdict = lines[-1]
    if verdict != "Verdict: pass" and not re.fullmatch(r"Verdict: blocking \([1-9][0-9]* hard findings\)", verdict):
        raise InvalidReview("final verdict must be pass or blocking with a positive hard-finding count")
    for start, end in zip(positions, positions[1:] + [len(lines) - 1]):
        if not any(line.strip() for line in lines[start + 1:end]):
            raise InvalidReview("review axes must contain findings or an explicit no-findings statement")
    rendered = body.strip()
    if heading_matches[0] > 0:
        original_lines = rendered.splitlines(keepends=True)
        rendered = expected_heading + "\n\n" + "".join(original_lines[:heading_matches[0]] + original_lines[heading_matches[0] + 1:])
    return rendered.rstrip() + "\n", "pass" if verdict == "Verdict: pass" else "blocking"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-file", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        args.report.unlink(missing_ok=True)
        report, verdict = final_report(args.execution_file, args.head)
        args.report.write_text(report, encoding="utf-8")
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"verdict={verdict}\n")
    except InvalidReview as error:
        print(f"Review output rejected: {error}", file=sys.stderr)
        return 1
    except UnicodeError:
        print("Review output rejected: terminal report contains invalid Unicode", file=sys.stderr)
        return 1
    except OSError:
        print("Review output rejected: report or verdict output could not be written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
