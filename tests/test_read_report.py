"""A Task report's details reach the Talker only when it reads them, part by part (ADR 0033)."""

import re
from pathlib import Path
from typing import Any

from jarvis_hub.details import PART_CHARS, split_parts
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.tasks import Tasks
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Step, Text, Tool, ToolFor, lines, make_hub

WEEKS = [
    f"## Week {week}\n\n" + " ".join(f"Day {day} of week {week}: send ten replies to founders who posted about launching."
                                     for day in range(1, 8))
    for week in range(1, 9)
]
PLAN = "\n\n".join(WEEKS)


def words(text: str) -> list[str]:
    return re.findall(r"[\w:]+", text)


def test_short_details_are_one_part() -> None:
    assert split_parts("The plan has no budget.") == ["The plan has no budget."]


def test_long_details_split_at_paragraphs_into_parts_that_keep_every_word_in_order() -> None:
    parts = split_parts(PLAN)

    assert len(parts) > 1
    assert all(len(part) <= PART_CHARS for part in parts)
    assert words(" ".join(parts)) == words(PLAN)
    assert all(part.startswith("## Week") for part in parts)


def test_a_paragraph_longer_than_a_part_splits_between_sentences() -> None:
    sentence = "Reply to every founder who mentions a launch this week."
    paragraph = " ".join([sentence] * 60)

    parts = split_parts(paragraph)

    assert len(parts) > 1 and all(len(part) <= PART_CHARS for part in parts)
    assert all(part.endswith(".") for part in parts)
    assert words(" ".join(parts)) == words(paragraph)


def report_in(message: str) -> str:
    found = re.search(r'report="(R\d+)"', message)
    assert found is not None, message
    return found[1]


def reading(part: int) -> ToolFor:
    return ToolFor("read_report", lambda message: {"report": report_in(message), "part": part})


async def reported(log: EventLog, report: dict[str, Any], news_turn: list[Step] | None = None,
                   output: RecordingOutput | None = None) -> FakeBackend:
    """The Orchestrator reports on T1, then the Talker's turn telling that news runs `news_turn`."""
    backend = FakeBackend(FakeClock(), [
        [Tool("delegate", {"request": "Break down the first phase of the plan"}), Text("On it.")],
        [*(news_turn or []), Text("The breakdown is ready.")],
    ], orchestrator=[[Tool("report", {"task_id": "T1", "needs_user": False, **report})]])
    hub = make_hub(backend, EventOutput(log, output or RecordingOutput()), RecordingMetrics())
    await hub.start()
    try:
        await hub.run(lines(backend.clock, "Break down the first phase of the plan"))
    finally:
        await hub.close()
    return backend


def news(backend: FakeBackend) -> str:
    return backend.all_sessions("talker")[0].received[-1]


def results(backend: FakeBackend, name: str) -> list[str]:
    return [str(r.result) for s in backend.all_sessions("talker") for r in s.tool_results if r.name == name]


def orchestrator_result(backend: FakeBackend) -> str | None:
    return backend.all_sessions("orchestrator")[0].tool_results[0].result


def read_text(read: list[str]) -> str:
    return " ".join(m[1] for text in read if (m := re.search(r">\n(.*)\n</report>", text, re.DOTALL)))


COUNT = len(split_parts(PLAN))


async def test_the_talker_hears_one_sentence_and_reads_long_details_part_by_part(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite3") as log:
        backend = await reported(log, {"spoken": "The first phase is eight weeks of outreach.",
                                       "details_markdown": PLAN}, [reading(part) for part in range(1, COUNT + 1)])

    report = report_in(news(backend))
    assert f'details="on screen" report="{report}" parts="{COUNT}"' in news(backend)
    assert "Week 1" not in news(backend)
    read = results(backend, "read_report")
    assert f'<report id="{report}" task="T1" part="1" of="{COUNT}">' in read[0]
    assert f"Read part 2 of {COUNT}" in read[0]
    assert "last part" in read[-1]
    assert words(read_text(read)) == words(PLAN)


async def test_read_report_defaults_to_the_first_part_and_explains_bad_arguments(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite3") as log:
        backend = await reported(log, {"spoken": "Done.", "details_markdown": "One short paragraph."}, [
            ToolFor("read_report", lambda message: {"report": report_in(message).lower()}),
            reading(2),
            Tool("read_report", {"report": "R999999"}),
            Tool("read_report", {"report": "the plan"}),
            ToolFor("read_report", lambda message: {"report": report_in(message), "part": 0}),
        ])

    report = report_in(news(backend))
    assert f'report="{report}" parts="1"' in news(backend)
    first, beyond, unknown, garbled, zero = results(backend, "read_report")
    assert "One short paragraph." in first and "last part" in first
    assert beyond == f"{report} has 1 part."
    assert unknown == "No report R999999 with details in the last 30 days."
    assert "such as R12" in garbled
    assert zero == "part is a whole number from 1."


async def test_a_report_without_details_names_no_report_to_read(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite3") as log:
        backend = await reported(log, {"spoken": "Nothing more to say."})

    assert "report=" not in news(backend) and "details=" not in news(backend)


async def test_reading_aloud_never_repeats_a_secret_the_screen_showed(tmp_path: Path) -> None:
    secret = "ghp_" + "a1B2" * 9
    with EventLog(tmp_path / "events.sqlite3") as log:
        backend = await reported(log, {"spoken": "Token found.", "details_markdown": f"The token is {secret}."},
                                 [reading(1)])

    [read] = results(backend, "read_report")
    assert secret not in read and "The token is" in read


async def test_status_names_the_last_reports_details_and_recall_points_at_them(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite3") as log:
        backend = await reported(log, {"spoken": "The first phase is eight weeks of outreach.", "details_markdown": PLAN},
                                 [Tool("status"), Tool("recall", {"query": "founders launching"})])

    report = report_in(news(backend))
    [status] = results(backend, "status")
    [recalled] = results(backend, "recall")
    assert f"(details {report})" in status
    assert f'task="T1" report="{report}"' in recalled


async def test_an_attached_file_becomes_the_details_without_the_orchestrator_retyping_it(tmp_path: Path) -> None:
    plan = tmp_path / "Marketing plan.md"
    plan.write_text(PLAN)
    output = RecordingOutput()
    with EventLog(tmp_path / "events.sqlite3") as log:
        backend = await reported(log, {"spoken": "Here is the plan.", "details_markdown": "From your Downloads.",
                                       "details_file": str(plan)}, [reading(part) for part in range(1, COUNT + 2)],
                                 output)

    assert orchestrator_result(backend) == "Reported to the User."
    [shown] = [text for kind, text in output.log if kind == "show"]
    assert shown == f"From your Downloads.\n\n**Marketing plan.md**\n\n{PLAN}"
    assert words(read_text(results(backend, "read_report"))) == words(shown.replace("**", ""))


async def test_a_code_file_is_attached_in_a_fence(tmp_path: Path) -> None:
    source = tmp_path / "plan.py"
    source.write_text("WEEKS = 8\n")
    output = RecordingOutput()
    with EventLog(tmp_path / "events.sqlite3") as log:
        await reported(log, {"spoken": "Here it is.", "details_file": str(source)}, output=output)

    [shown] = [text for kind, text in output.log if kind == "show"]
    assert shown == "**plan.py**\n\n```py\nWEEKS = 8\n```"


async def test_a_file_the_hub_cannot_attach_is_refused_with_what_to_do(tmp_path: Path) -> None:
    binary = tmp_path / "plan.pdf"
    binary.write_bytes(b"%PDF-1.7\n\xff\xfe\x00\x81")
    huge = tmp_path / "huge.md"
    huge.write_text("word " * 60_000)
    refused: list[str | None] = []
    for path in ("plan.md", str(tmp_path / "missing.md"), str(binary), str(huge)):
        with EventLog(tmp_path / f"events-{len(refused)}.sqlite3") as log:
            refused.append(orchestrator_result(await reported(log, {"spoken": "Here.", "details_file": path})))

    relative, missing, unreadable, too_big = refused
    assert relative == "details_file must be an absolute path."
    assert missing == f"There is no file at {tmp_path / 'missing.md'}."
    assert unreadable == f"{binary} is not a text file: put what it says in details_markdown."
    assert too_big is not None and too_big.startswith(f"{huge} is too long to attach")


def test_a_restart_keeps_the_id_of_an_open_tasks_last_details_and_loads_older_saves() -> None:
    tasks = Tasks()
    task = tasks.create("Break down the plan", 0.0)
    task.report("The breakdown is ready.", needs_user=False, live=True, details="R12")
    [saved] = tasks.dump(lambda at: at)
    older = {key: value for key, value in saved.items() if key != "last_details"}

    [restored, before] = Tasks.load([saved, {**older, "id": "T2"}], lambda at: at)

    assert (restored.last_details, before.last_details) == ("R12", None)
