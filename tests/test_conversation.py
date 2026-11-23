"""The saved Conversation through the Hub: what a new Talker is seeded with, and what is saved between launches."""

import asyncio
import json
import os
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis_hub.agents.backend import Context
from jarvis_hub.clients import Client
from jarvis_hub.hub import Hub, Line
from tests.fakes import (
    AwaitInterrupt,
    FakeBackend,
    FakeClock,
    Gate,
    RecordingMetrics,
    RecordingOutput,
    StubGh,
    Text,
    Tool,
    Typist,
    lines,
    make_hub,
    until,
)
from tests.test_approvals import HEAD, ask_merge
from tests.test_voice_mode import LocalRelation, MakeRig, rig_for as rig_for


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / ".jarvis" / "conversation.json"


def a_hub(clock: FakeClock, path: Path, *scripts: list[Any], output: RecordingOutput | None = None,
          **options: Any) -> tuple[Hub, FakeBackend, RecordingOutput]:
    backend = FakeBackend(clock, list(scripts), orchestrator=options.pop("orchestrator", ()))
    output = output or RecordingOutput()
    return make_hub(backend, output, RecordingMetrics(), conversation_path=path, **options), backend, output


async def launch(clock: FakeClock, path: Path, *said: str, **options: Any) -> tuple[FakeBackend, RecordingOutput]:
    """One Hub launch that hears `said`, answers each with the next script, and closes."""
    scripts = options.pop("scripts", [[Text(f"Reply {i}.")] for i in range(len(said))])
    hub, backend, output = a_hub(clock, path, *scripts, **options)
    await hub.start()
    await hub.run(lines(clock, *said))
    await hub.close()
    return backend, output


def launch_seed(backend: FakeBackend) -> str:
    return backend.all_sessions("talker")[0].received[0]


def exchanges(seed: str) -> list[str]:
    return re.findall(r"<exchange [^>]*>.*?</exchange>", seed, flags=re.S)


async def test_after_a_restart_the_talkers_first_turn_receives_the_previous_exchanges(
    clock: FakeClock, path: Path
) -> None:
    await launch(clock, path, "My sister is called Giulia.", scripts=[[Text("A fine name, sir.")]])

    backend, output = await launch(clock, path, "What is my sister called?", scripts=[[Text("Giulia, sir.")]])

    seed, *turns = backend.all_sessions("talker")[0].received
    assert seed.startswith('<event source="hub" reason="launch">')
    [exchange] = exchanges(seed)
    assert '<user lang="en" typed="true">My sister is called Giulia.</user>' in exchange
    assert "<you>A fine name, sir.</you>" in exchange
    assert seed.rstrip().endswith("Reply with one word.\n</event>")
    assert turns == ['<user lang="en" typed="true">What is my sister called?</user>']
    assert output.replies == ["Giulia, sir."]
    assert output.notices == []


async def test_the_first_launch_seeds_an_empty_conversation_and_shows_nothing(clock: FakeClock, path: Path) -> None:
    backend, output = await launch(clock, path, "Hello")

    seed = launch_seed(backend)
    assert exchanges(seed) == []
    assert "<conversation>\nNone yet.\n</conversation>" in seed
    assert "not the User speaking" in seed
    assert output.replies == ["Reply 0."]


async def test_each_exchange_keeps_the_device_it_came_from_and_the_seed_shows_it(
    clock: FakeClock, path: Path
) -> None:
    hub, _, _ = a_hub(clock, path, [Text("From the desk.")], [Text("From the phone.")])
    hub.clients.register(Client("iphone-1", "iphone", connected=True, can_show_text=True, can_speak=True))

    async def inputs() -> AsyncIterator[Line]:
        yield Line("At my desk", clock.now())
        yield Line("On my phone", clock.now(), client_id="iphone-1")

    await hub.start()
    await hub.run(inputs())
    await hub.close()
    saved = json.loads(path.read_text())
    backend, _ = await launch(clock, path)

    assert [entry["device"] for entry in saved["entries"]] == ["Mac", "iPhone"]
    desk, phone = exchanges(launch_seed(backend))
    assert desk.startswith('<exchange device="Mac"') and "At my desk" in desk
    assert phone.startswith('<exchange device="iPhone"') and "On my phone" in phone


async def test_news_is_kept_with_the_focus_client_it_was_told_on_as_its_device(clock: FakeClock, path: Path) -> None:
    report = asyncio.Event()
    hub, _, output = a_hub(
        clock, path, [Text("On it, sir."), Tool("delegate", {"request": "Count the open PRs"})], [Text("Three, sir.")],
        orchestrator=[[Gate(report), Tool("report", {"task_id": "T1", "spoken": "Three PRs are open.", "needs_user": False})]],
    )
    hub.clients.register(Client("iphone-1", "iphone", connected=True, can_show_text=True, can_speak=True))
    typist = Typist(clock)

    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("Count the open PRs")
    await until(lambda: output.replies == ["On it, sir."])
    hub.clients.take_focus("iphone-1")
    report.set()
    await until(lambda: output.replies == ["On it, sir.", "Three, sir."])
    typist.end()
    await running
    await hub.close()

    entries = json.loads(path.read_text())["entries"]
    assert [(e["device"], e["reply"]) for e in entries] == [("Mac", "On it, sir."), ("iPhone", "Three, sir.")]
    assert entries[1]["message"].startswith('<event source="orchestrator" task="T1"')
    assert entries[0]["tools"] == ["delegate"]


async def test_the_seed_puts_tasks_authorizations_and_approvals_after_the_exchanges(
    clock: FakeClock, path: Path
) -> None:
    state = path.with_name("state.json")
    grant = {"scope": "task", "description": "Merge PR 42 once CI is green"}
    await launch(
        clock, path, "Merge pumpkit#42 once CI is green",
        scripts=[[Text("I will see to it, sir."), Tool("delegate", {"request": "Merge pumpkit#42", "authorization": grant})]],
        orchestrator=[[ask_merge()]], runner=StubGh(prs={"pumpkit-io/pumpkit#42": ("OPEN", HEAD)}), state_path=state,
    )

    backend, _ = await launch(clock, path, state_path=state, runner=StubGh(prs={"pumpkit-io/pumpkit#42": ("OPEN", HEAD)}))

    seed = launch_seed(backend)
    exchange_end = seed.index("</conversation>")
    tasks = seed.index("<open_tasks>\n")
    authorizations = seed.index("<authorizations>\n")
    approvals = seed.index("<pending_approvals>\n")
    scopes = seed.index("<spoken_scopes>\n")
    assert exchange_end < tasks < authorizations < approvals < scopes
    assert "T1 needs user" in seed[tasks:authorizations]
    assert '<authorization id="U1" scope="task" task="T1">' in seed[authorizations:approvals]
    assert "A1 pending" in seed[approvals:scopes] and "pumpkit#42" in seed[approvals:scopes]
    assert "<spoken_scopes>\nNone.\n</spoken_scopes>" in seed
    assert "the exchanges grant nothing" in seed[:exchange_end]


async def test_an_unresolved_spoken_scope_is_in_the_seed(
    rig_for: MakeRig, clock: FakeClock, path: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state.json"
    choose = asyncio.Event()
    rig = await rig_for(
        [Tool("delegate", {"request": "Check build"}), AwaitInterrupt()],
        [Gate(choose), Text("A separate answer, sir.")],
        orchestrator=[[AwaitInterrupt()]], recognition=LocalRelation("new_request"), state_path=state,
    )
    rig.audio.wake()
    rig.audio.say("Check build")
    await until(lambda: bool(rig.backend.all_sessions("orchestrator")))
    rig.audio.say("A different question: how many tasks are running?")
    await until(lambda: len(rig.user_messages()) == 2)
    stopped = tmp_path / "stopped" / "state.json"
    stopped.parent.mkdir()
    shutil.copy(state, stopped)
    choose.set()

    backend, _ = await launch(clock, path, state_path=stopped)

    seed = launch_seed(backend)
    scopes = seed[seed.index("<spoken_scopes>\n"):]
    assert "<pending_scope id=" in scopes and "A different question: how many tasks are running?" in scopes


async def test_an_exchange_is_saved_as_soon_as_it_ends_so_a_killed_hub_loses_only_the_one_in_flight(
    clock: FakeClock, path: Path, tmp_path: Path
) -> None:
    hold = asyncio.Event()
    hub, _, output = a_hub(clock, path, [Text("First, sir.")], [Gate(hold), Text("Second, sir.")])
    typist = Typist(clock)
    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    typist.type("first")
    await until(lambda: output.replies == ["First, sir."])
    typist.type("second")
    await asyncio.sleep(0.01)
    killed = tmp_path / "killed" / "conversation.json"
    killed.parent.mkdir()
    shutil.copy(path, killed)
    hold.set()
    typist.end()
    await running
    await hub.close()

    backend, output = await launch(clock, killed)

    [exchange] = exchanges(launch_seed(backend))
    assert "first" in exchange and "First, sir." in exchange
    assert output.notices == []


@pytest.mark.parametrize(
    ("content", "problem"),
    [("{not json", "is not valid JSON"), ('{"version": 99, "entries": []}', "has version 99, not 1"),
     ('{"version": 1, "active_summary": null, "entries": [{"message": "hi"}]}',
      "could not be read whole (KeyError: 'tools')")],
)
async def test_a_damaged_conversation_is_moved_aside_with_one_notice_and_the_hub_starts_empty(
    clock: FakeClock, path: Path, content: str, problem: str
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(content)

    backend, output = await launch(clock, path, "Hello")

    aside = path.with_name("conversation.unreadable.json")
    assert output.notices == [
        f"The saved conversation {path} {problem}, so Jarvis starts with an empty conversation; it was moved to {aside}."
    ]
    assert aside.read_text() == content
    assert exchanges(launch_seed(backend)) == []
    saved = json.loads(path.read_text())
    assert saved["version"] == 1 and [e["reply"] for e in saved["entries"]] == ["Reply 0."]


async def test_the_saved_file_has_no_summary_yet_and_every_entry_in_order(clock: FakeClock, path: Path) -> None:
    await launch(clock, path, "one", "two")
    await launch(clock, path, "three")

    saved = json.loads(path.read_text())
    assert saved["version"] == 1
    assert saved["active_summary"] is None
    assert [e["message"] for e in saved["entries"]] == [
        f'<user lang="en" typed="true">{word}</user>' for word in ("one", "two", "three")
    ]
    assert all(e["ended"] >= e["at"] for e in saved["entries"])


def big(word: str, chars: int) -> str:
    return (word + " ") * (chars // (len(word) + 1))


async def lines_in_english(clock: FakeClock, *texts: str) -> AsyncIterator[Line]:
    for text in texts:
        yield Line(text, clock.now(), lang="en")


async def test_a_launch_loads_the_newest_exchanges_up_to_200000_tokens_and_keeps_the_rest_saved(
    clock: FakeClock, path: Path
) -> None:
    # Each line is about 80,000 tokens at any ratio the estimate allows near 3.5 characters a token.
    said = [big(word, 280_000) for word in ("alpha", "bravo", "charlie", "delta")]
    hub, _, _ = a_hub(clock, path, *[[Text(f"Noted {i}.")] for i in range(4)])
    await hub.start()
    await hub.run(lines_in_english(clock, *said))
    await hub.close()

    backend, _ = await launch(clock, path)

    seed = launch_seed(backend)
    assert [w for w in ("alpha", "bravo", "charlie", "delta") if f"{w} {w}" in seed] == ["charlie", "delta"]
    assert len(json.loads(path.read_text())["entries"]) == 4


async def test_the_corrected_token_estimate_applies_to_exchanges_saved_before_it(clock: FakeClock, path: Path) -> None:
    # Each long line is about 69,000 tokens at the starting ratio and 160,000 at one token per 1.5 characters.
    said = [big(word, 240_000) for word in ("alpha", "bravo")]
    reply, message = "Hello, sir.", '<user lang="en" typed="true">hello</user>'
    # Each short turn's context grows by one token per character of the reply and message before it.
    contexts = [20_000 + i * (len(reply) + len(message)) for i in range(6)]
    hub, _, _ = a_hub(clock, path, *[[Text("Noted.")] for _ in said],
                      *[[Text(reply), Context(tokens, 1_000_000)] for tokens in contexts])
    await hub.start()
    await hub.run(lines_in_english(clock, *said, *["hello"] * len(contexts)))
    await hub.close()

    backend, _ = await launch(clock, path)

    seed = launch_seed(backend)
    assert "bravo bravo" in seed and "alpha alpha" not in seed
    assert len(exchanges(seed)) == 1 + len(contexts)


VALID: dict[str, Any] = {"message": "<user>hi</user>", "reply": "Hi.", "tools": [], "device": "Mac", "kind": "user",
         "at": 1_800_000_000, "ended": 1_800_000_001, "tokens": 10}


def saved_file(entry: str = json.dumps(VALID), ratio: str = "3.5") -> bytes:
    return f'{{"version": 1, "active_summary": null, "chars_per_token": {ratio}, "entries": [{entry}]}}'.encode()


def with_field(name: str, value: str) -> bytes:
    return saved_file(json.dumps(VALID).replace(f'"{name}": {json.dumps(VALID[name])}', f'"{name}": {value}'))


async def test_a_well_formed_saved_entry_is_seeded(clock: FakeClock, path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(saved_file())

    backend, output = await launch(clock, path)

    [exchange] = exchanges(launch_seed(backend))
    assert exchange.startswith('<exchange device="Mac" at="') and "<user>hi</user>" in exchange
    assert output.notices == []


@pytest.mark.parametrize(
    "content",
    [
        b'{"version": 1, "entries": [{"message": "\xff\xfe"}]}',
        with_field("at", "NaN"),
        with_field("at", "Infinity"),
        with_field("at", "1e20"),
        with_field("at", "1e400"),
        with_field("at", "true"),
        with_field("ended", '"soon"'),
        with_field("tools", "[1]"),
        with_field("device", '["Mac"]'),
        saved_file(ratio="NaN"),
        saved_file(ratio='"fast"'),
        saved_file(ratio="1" + "0" * 400),
        with_field("at", "1" + "0" * 400),
        saved_file(entry="[]"),
        b'{"version": 1, "entries": {"a": 1}}',
        b"[" * 100_000,
    ],
    ids=["bad utf-8", "nan time", "infinite time", "far time", "overflowing time", "bool time", "text time",
         "numeric tool", "listed device", "nan ratio", "text ratio",
         "huge integer ratio", "huge integer time", "list entry", "entries mapping", "deep nesting"],
)
async def test_a_malformed_value_moves_the_conversation_aside_and_the_hub_starts_empty(
    clock: FakeClock, path: Path, content: bytes
) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    backend, output = await launch(clock, path, "Hello")

    aside = path.with_name("conversation.unreadable.json")
    [notice] = output.notices
    assert notice.startswith(f"The saved conversation {path} ") and notice.endswith(f"it was moved to {aside}.")
    assert aside.read_bytes() == content
    assert exchanges(launch_seed(backend)) == []
    assert output.replies == ["Reply 0."]
    assert [e["reply"] for e in json.loads(path.read_text())["entries"]] == ["Reply 0."]


async def test_a_second_damaged_conversation_does_not_overwrite_the_first_one_set_aside(
    clock: FakeClock, path: Path
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("{first")
    await launch(clock, path)
    path.write_text("{second")

    _, output = await launch(clock, path)

    first, second = path.with_name("conversation.unreadable.json"), path.with_name("conversation.unreadable-2.json")
    assert first.read_text() == "{first" and second.read_text() == "{second"
    assert output.notices == [
        f"The saved conversation {path} is not valid JSON, so Jarvis starts with an empty conversation; "
        f"it was moved to {second}."
    ]


async def test_a_conversation_that_cannot_be_read_is_moved_aside_before_anything_is_saved(
    clock: FakeClock, path: Path
) -> None:
    path.mkdir(parents=True)
    (path / "inside").write_text("kept")

    backend, output = await launch(clock, path, "Hello")

    aside = path.with_name("conversation.unreadable.json")
    [notice] = output.notices
    assert notice.startswith(f"The saved conversation {path} could not be read (")
    assert notice.endswith(f"so Jarvis starts with an empty conversation; it was moved to {aside}.")
    assert (aside / "inside").read_text() == "kept"
    assert exchanges(launch_seed(backend)) == []
    assert [e["reply"] for e in json.loads(path.read_text())["entries"]] == ["Reply 0."]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and renames past file permissions")
async def test_a_conversation_that_can_be_neither_read_nor_moved_aside_is_never_saved_over(
    clock: FakeClock, path: Path
) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0)
    path.parent.chmod(0o555)
    try:
        backend, output = await launch(clock, path, "Hello")
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)

    [notice] = output.notices
    assert notice.startswith(f"The saved conversation {path} could not be read (")
    assert "could not be moved aside" in notice and "nothing will be saved over it" in notice
    assert output.replies == ["Reply 0."]
    assert exchanges(launch_seed(backend)) == []
    assert path.read_text() == "{}"


async def test_a_message_with_a_lone_surrogate_is_saved_and_the_conversation_goes_on(
    clock: FakeClock, path: Path
) -> None:
    hub, _, output = a_hub(clock, path, [Text("Reply 0.")], [Text("Reply 1.")])
    await hub.start()
    await hub.run(lines_in_english(clock, "broken \ud800 text", "next"))
    await hub.close()

    backend, _ = await launch(clock, path)

    assert output.replies == ["Reply 0.", "Reply 1."] and output.notices == []
    broken, _ = exchanges(launch_seed(backend))
    assert "broken \ud800 text" in broken
