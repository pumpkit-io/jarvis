"""Reset context over the loopback protocol: a Client's command makes the Hub, and no model, forget the Conversation
and Memory on both devices, while the 30-day log, Tasks and the screens stay as they were."""

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from jarvis_hub.conversation import Conversation
from jarvis_hub.door import Door
from jarvis_hub.event_log import EventLog, EventOutput
from jarvis_hub.hub import Hub, Line
from jarvis_hub.pairing import PairingResult, PairingStore
from jarvis_hub.state import UnreadableFile
from tests.fakes import (
    Fail,
    FakeBackend,
    FakeClock,
    Gate,
    Limited,
    RecordingMetrics,
    RecordingOutput,
    StubGh,
    Text,
    Tool,
    a_summary,
    make_hub,
    until,
)
from tests.protocol_exchanges import SNAPSHOTS, command, connect, receive_conversation
from tests.test_approvals import HEAD, ask_merge
from tests.test_fold import SHORT, Voice, said, tokens
from tests.test_start_afresh import AFRESH


@dataclass
class Rig:
    hub: Hub
    backend: FakeBackend
    clock: FakeClock
    log: EventLog
    output: RecordingOutput
    metrics: RecordingMetrics
    socket: aiohttp.ClientWebSocketResponse
    phone: PairingResult
    # Every event frame the phone received, in order.
    frames: list[dict[str, Any]]

    async def send(self, id: str, kind: str, **payload: Any) -> dict[str, Any]:
        """Sends a command and returns its result, keeping the events that arrive meanwhile."""
        await self.socket.send_json(command(id, kind, **payload))
        while (frame := await receive_conversation(self.socket))["type"] != "command_result" or frame["id"] != id:
            if frame["type"] != "command_result":
                self.frames.append(frame)
        return frame

    async def reply_begun(self) -> None:
        """Waits until the phone received the first words of a reply."""
        while not any(f["type"] == "reply_delta" for f in self.frames):
            self.frames.append(await receive_conversation(self.socket))

    async def hub_reply(self, accepted: dict[str, Any]) -> dict[str, Any]:
        """The reply_end to an accepted say_text, once the phone received it."""
        request_id = accepted["result"]["request_id"]
        while not any(f["type"] == "reply_end" and f["payload"]["request_id"] == request_id for f in self.frames):
            self.frames.append(await receive_conversation(self.socket))
        return next(f for f in self.frames if f["type"] == "reply_end" and f["payload"]["request_id"] == request_id)


@asynccontextmanager
async def a_rig(root: Path, scripts: Sequence[Any] = (), *, phone: PairingResult | None = None,
                **options: Any) -> AsyncGenerator[Rig]:
    """A Hub with its Door and a connected phone; pass the `phone` of an earlier rig on `root` to restart the Hub."""
    backend_options = {name: options.pop(name) for name in ("summaries", "seed_reply", "orchestrator",
                                                                "restart_error") if name in options}
    clock = FakeClock()
    with PairingStore(root / "pairing.db") as pairing, EventLog(root / "events.db", wall=clock.now) as log:
        phone = phone or pairing.exchange(pairing.create_code("https://mac.example.ts.net"), "Phone", "iphone")
        backend = FakeBackend(clock, scripts, **backend_options)
        output, metrics = RecordingOutput(), RecordingMetrics()
        hub = make_hub(backend, EventOutput(log, output), metrics, conversation_path=root / "conversation.json",
                       **options)
        door = Door(enabled=True, port=0, health=hub.health, pairing=pairing, hub=hub, log=log, now=clock.now)
        await hub.start()
        await door.start()
        try:
            async with aiohttp.ClientSession() as session, connect(session, door, phone.token) as socket:
                assert (await receive_conversation(socket))["type"] == "replay_complete"
                yield Rig(hub, backend, clock, log, output, metrics, socket, phone, [])
        finally:
            await door.close()
            await hub.close()


def an_earlier_launch(root: Path) -> None:
    """What earlier launches left: two Folds, so an active summary and the one it replaced, an entry since, and a
    damaged copy of the Conversation set aside."""
    conversation = Conversation(root / "conversation.json")
    for i, line in enumerate(("My sister is Giulia.", "My dog is Fido.")):
        conversation.add(f"<user>{line}</user>", "Noted.", tools=(), device="iPhone", kind="user", at=900.0 + i,
                         ended=900.5 + i)
        summary = a_summary(line)
        version = conversation.keep_summary(summary, made=950.0 + i)
        conversation.commit(version, summary, conversation.entries[:1], now=950.0 + i)
    conversation.add("<user>We spoke of Rome.</user>", "Rome it is.", tools=(), device="Mac", kind="user", at=960.0,
                     ended=960.5)
    (root / "conversation.unreadable.json").write_text('{"entries": "My sister is Giulia."')


async def test_reset_context_deletes_the_conversation_its_summary_and_every_old_version(tmp_path: Path) -> None:
    an_earlier_launch(tmp_path)
    assert sorted(p.name for p in (tmp_path / "memory").iterdir()) == ["summary-1.json", "summary-2.json"]

    async with a_rig(tmp_path) as rig:
        logged = rig.log.after(0)
        result = await rig.send("reset-1", "reset_context")
        retried = await rig.send("reset-1", "reset_context")

        assert result == {"version": 1, "type": "command_result", "id": "reset-1", "ok": True,
                          "result": {"status": "reset"}}
        assert retried == result
        assert not (tmp_path / "conversation.json").exists()
        assert not (tmp_path / "conversation.unreadable.json").exists()
        assert not (tmp_path / "memory").exists()
        assert [e for e in rig.log.after(0) if e.type not in SNAPSHOTS] == [
            e for e in logged if e.type not in SNAPSHOTS]
        assert rig.frames == []
    assert rig.output.notices == []


def tool_results(backend: FakeBackend, name: str) -> list[str | None]:
    return [r.result for s in backend.all_sessions("talker") for r in s.tool_results if r.name == name]


async def test_after_a_reset_the_next_turn_on_the_iphone_and_the_mac_has_no_earlier_messages(tmp_path: Path) -> None:
    an_earlier_launch(tmp_path)
    recall = [Tool("recall", {"query": "Giulia"}), Text("I do not know, sir.")]
    async with a_rig(tmp_path, [[Text("Noted.")], recall, [Text("Here, sir.")]]) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        old = rig.backend.all_sessions("talker")[0]
        rig.clock.advance(1)

        await rig.send("reset-1", "reset_context")
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2 and old.closed)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Who is my sister?"))
        await rig.hub.admit_line(Line("And from the Mac?", rig.clock.now()))
        await until(lambda: len(rig.output.replies) == 3)
        heard = [e.payload["text"] for e in rig.log.after(0) if e.type == "heard"]

    fresh = rig.backend.all_sessions("talker")[1]
    seed, from_phone, from_mac = fresh.received
    assert seed.startswith('<event source="hub" reason="reset"')
    assert "<conversation>\nNone yet.\n</conversation>" in seed and "None yet: nothing has been folded." in seed
    assert not any(word in seed for word in ("Giulia", "Fido", "Rome"))
    assert "Who is my sister?" in from_phone and "And from the Mac?" in from_mac
    [found] = tool_results(rig.backend, "recall")
    assert found is not None and "No matches." in found and "the last Reset context" in found
    assert heard == ["My sister is Giulia.", "Who is my sister?", "And from the Mac?"]
    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert [said(e.message) for e in saved.entries] == ["Who is my sister?", "And from the Mac?"]
    assert saved.summary is None


async def test_a_turn_in_flight_at_the_reset_finishes_on_screen_but_its_exchange_is_dropped(tmp_path: Path) -> None:
    release = asyncio.Event()
    recall = [Tool("recall", {"query": "Noted sir"}), Text("Hello, sir.")]
    async with a_rig(tmp_path, [[Text("Noted"), Gate(release), Text(", sir.")], recall]) as rig:
        accepted = await rig.send("say-1", "say_text", text="My sister is Giulia.")
        await rig.reply_begun()
        old = rig.backend.all_sessions("talker")[0]
        rig.clock.advance(1)

        assert (await rig.send("reset-1", "reset_context"))["ok"]
        release.set()
        assert (await rig.hub_reply(accepted))["payload"]["text"] == "Noted, sir."
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2 and old.closed)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Hello again."))

    seed = rig.backend.all_sessions("talker")[1].received[0]
    assert "Giulia" not in seed and "Noted" not in seed
    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert [said(e.message) for e in saved.entries] == ["Hello again."]
    assert rig.output.replies == ["Noted, sir.", "Hello, sir."]
    # The reply ended after the reset, but its turn began before it.
    [found] = tool_results(rig.backend, "recall")
    assert found is not None and "No matches." in found


async def test_a_reset_cancels_a_fold_whose_summary_is_being_written(tmp_path: Path) -> None:
    late = asyncio.Event()
    async with a_rig(tmp_path, [[Text("Noted."), tokens(200_000)]], memory=SHORT,
                     summaries=[[Gate(late), Text(a_summary("The User's sister is Giulia."))]]) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        await until(lambda: len(rig.backend.all_sessions("summariser")) == 1)

        await rig.send("reset-1", "reset_context")
        [summariser] = rig.backend.all_sessions("summariser")
        await until(lambda: summariser.closed and len(rig.backend.all_sessions("talker")) == 2)
        late.set()
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Hello again."))

    assert not (tmp_path / "memory").exists()
    assert [r["outcome"] for r in rig.metrics.records if r.get("role") == "summariser"] == ["cancelled"]
    assert "Giulia" not in rig.backend.all_sessions("talker")[1].received[0]
    assert json.loads((tmp_path / "conversation.json").read_text())["active_summary"] is None


async def test_a_fold_result_arriving_after_the_reset_is_discarded_and_never_saved(tmp_path: Path) -> None:
    seeding = asyncio.Event()
    async with a_rig(tmp_path, [[Text("Noted."), tokens(200_000)]], memory=SHORT,
                     summaries=[[Text(a_summary("The User's sister is Giulia."))]],
                     seed_reply=[Gate(seeding), Text("Ready.")]) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        # The summary is kept as its own version and a new Talker is reading it when the reset comes.
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2)
        assert (tmp_path / "memory" / "summary-1.json").exists()

        await rig.send("reset-1", "reset_context")
        seeding.set()
        await until(lambda: len(rig.backend.all_sessions("talker")) == 3)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Hello again."))
        launched, folded, fresh = rig.backend.all_sessions("talker")
        assert launched.closed and folded.closed and not fresh.closed

    assert [len(s.received) for s in (launched, folded)] == [2, 1]
    assert fresh.received[0].startswith('<event source="hub" reason="reset"') and "Giulia" not in fresh.received[0]
    assert not (tmp_path / "memory").exists()
    saved = json.loads((tmp_path / "conversation.json").read_text())
    assert saved["active_summary"] is None and [said(e["message"]) for e in saved["entries"]] == ["Hello again."]
    assert rig.output.notices == []


async def test_a_reset_ends_the_wait_after_a_failed_fold(tmp_path: Path) -> None:
    turns = [[Text("Noted."), tokens(200_000)], [Text("Hello, sir."), tokens(200_000)]]
    async with a_rig(tmp_path, turns, memory=SHORT, summaries=[[Fail("overloaded")]] * 2) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        await until(lambda: len(rig.backend.all_sessions("summariser")) == 2
                    and all(s.closed for s in rig.backend.all_sessions("summariser")))

        await rig.send("reset-1", "reset_context")
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Hello again."))
        await until(lambda: len(rig.backend.all_sessions("summariser")) == 3)

    assert "Hello again." in rig.backend.all_sessions("summariser")[2].received[0]
    assert rig.output.notices == []


async def test_a_reset_whose_outcome_a_hub_restart_lost_is_never_run_again_and_a_new_one_resets(tmp_path: Path) -> None:
    an_earlier_launch(tmp_path)
    async with a_rig(tmp_path) as rig:
        # The Hub stops after reserving the command and before its result.
        rig.log.reserve(rig.phone.client.id, "reset-1", "reset_context", {})

    async with a_rig(tmp_path, phone=rig.phone) as restarted:
        unknown = await restarted.send("reset-1", "reset_context")
        assert unknown["ok"] is False and unknown["error"]["code"] == "outcome_unknown"
        assert (tmp_path / "conversation.json").exists() and (tmp_path / "memory").exists()
        assert len(restarted.backend.all_sessions("talker")) == 1

        assert (await restarted.send("reset-2", "reset_context"))["result"] == {"status": "reset"}
        assert (await restarted.send("reset-3", "reset_context"))["result"] == {"status": "reset"}
        assert await restarted.send("reset-1", "reset_context") == unknown
        assert not (tmp_path / "conversation.json").exists() and not (tmp_path / "memory").exists()


async def test_a_reset_context_with_a_payload_is_refused_and_resets_nothing(tmp_path: Path) -> None:
    an_earlier_launch(tmp_path)
    async with a_rig(tmp_path) as rig:
        refused = await rig.send("reset-1", "reset_context", everything=True)

    assert refused["error"]["code"] == "invalid_payload"
    assert (tmp_path / "conversation.json").exists() and (tmp_path / "memory").exists()


async def test_a_reset_leaves_tasks_authorizations_and_approvals_to_the_new_talker(tmp_path: Path) -> None:
    grant = {"scope": "task", "description": "Merge PR 42 once CI is green"}
    status = [Text("Let me look."), Tool("status")]
    async with a_rig(
        tmp_path,
        [[Text("I will see to it, sir."), Tool("delegate", {"request": "Merge pumpkit#42", "authorization": grant})],
         [Text("Shall I merge it?")], status, status],
        orchestrator=[[ask_merge()]], runner=StubGh(prs={"pumpkit-io/pumpkit#42": ("OPEN", HEAD)}),
        state_path=tmp_path / "state.json",
    ) as rig:
        await rig.send("presence-1", "set_presence", can_show_text=True)
        await rig.send("say-1", "say_text", text="Merge pumpkit#42 once CI is green")
        await until(lambda: len(rig.output.replies) == 2)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="What is running?"))
        await rig.send("reset-1", "reset_context")
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2)
        await rig.hub_reply(await rig.send("say-3", "say_text", text="What is running now?"))

    before, after = tool_results(rig.backend, "status")
    assert before == after
    assert before is not None and "A1" in before and "U1" in before and "T1" in before
    records = rig.backend.all_sessions("talker")[1].received[0].split("</conversation>")[1]
    assert "T1 needs user" in records and '<authorization id="U1"' in records and "A1 pending" in records


async def test_a_line_waiting_when_the_reset_comes_goes_to_the_new_talker(tmp_path: Path) -> None:
    release = asyncio.Event()
    async with a_rig(tmp_path, [[Text("Noted"), Gate(release), Text(", sir.")], [Text("Hello, sir.")]]) as rig:
        accepted = await rig.send("say-1", "say_text", text="My sister is Giulia.")
        await rig.reply_begun()
        await rig.hub.admit_line(Line("Who is my sister?", rig.clock.now()))

        await rig.send("reset-1", "reset_context")
        release.set()
        await rig.hub_reply(accepted)
        await until(lambda: len(rig.output.replies) == 2)

    old, fresh = rig.backend.all_sessions("talker")
    assert len(old.received) == 2
    assert "Giulia" not in fresh.received[0] and "Who is my sister?" in fresh.received[1]


async def test_a_new_talker_that_cannot_start_after_the_reset_is_told_once_and_tried_after_each_turn(
    tmp_path: Path
) -> None:
    async with a_rig(tmp_path, [[Text("One.")], [Text("Two.")]], restart_error=RuntimeError("CLI missing")) as rig:
        await rig.send("reset-1", "reset_context")
        await until(lambda: rig.backend.talker_opens == 2)
        await rig.hub_reply(await rig.send("say-1", "say_text", text="Hello."))
        await until(lambda: rig.backend.talker_opens == 3)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Again."))

    [notice] = rig.output.notices
    assert notice.startswith("Could not start a new Talker after the reset") and notice.endswith("CLI missing")
    assert rig.output.replies == ["One.", "Two."]
    # The old session still remembers what the reset deleted, so nothing it says is saved.
    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert saved.entries == ()


async def test_a_new_talker_seeded_before_a_later_reset_is_never_swapped_in(tmp_path: Path) -> None:
    seeding = asyncio.Event()
    async with a_rig(tmp_path, [[Text("Hello, sir.")]], seed_reply=[Gate(seeding), Text("Ready.")]) as rig:
        await rig.send("reset-1", "reset_context")
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2)
        await rig.send("reset-2", "reset_context")
        rig.backend.seed_reply = [Fail("overloaded")]
        seeding.set()
        await until(lambda: len(rig.backend.all_sessions("talker")) == 3)
        await rig.hub_reply(await rig.send("say-1", "say_text", text="Hello."))
        await until(lambda: len(rig.backend.all_sessions("talker")) == 4)

    launched, first, second, retry = rig.backend.all_sessions("talker")
    assert first.closed and len(first.received) == 1
    assert second.closed and "Hello." in launched.received[-1]
    # The failed seed is tried again after the old session's turn.
    assert retry.received[0].startswith('<event source="hub" reason="reset"')
    [notice] = rig.output.notices
    assert notice.startswith("Could not start a new Talker after the reset") and notice.endswith("overloaded")
    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert saved.entries == ()


async def test_a_new_talker_the_usage_limit_stopped_starts_when_the_limit_resets_and_takes_the_next_line(
    tmp_path: Path
) -> None:
    async with a_rig(tmp_path, [[Text("Noted.")], [Text("Hello, sir.")]], seed_reply=[Limited(3600)]) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        await rig.send("reset-1", "reset_context")
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2 and rig.backend.all_sessions("talker")[1].closed)
        rig.backend.seed_reply = [Text("Ready.")]
        rig.clock.advance(3600)
        await until(lambda: len(rig.backend.all_sessions("talker")) == 3)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Who is my sister?"))

    old, _, fresh = rig.backend.all_sessions("talker")
    assert len(old.received) == 2
    assert "Who is my sister?" in fresh.received[1]
    assert not any(n.startswith("Could not start a new Talker") for n in rig.output.notices)
    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert [said(e.message) for e in saved.entries] == ["Who is my sister?"]


async def test_a_reset_whose_time_could_not_be_stored_still_deletes_the_conversation_before_the_new_talker_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    an_earlier_launch(tmp_path)

    def broken(at: float, **_: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    async with a_rig(tmp_path, [[Text("Hello, sir.")]]) as rig:
        monkeypatch.setattr(rig.log, "set_reset_time", broken)
        result = await rig.send("reset-1", "reset_context")
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2)
        await rig.hub_reply(await rig.send("say-1", "say_text", text="Hello again."))

    assert result["ok"] is False
    assert not (tmp_path / "memory").exists() and not (tmp_path / "conversation.unreadable.json").exists()
    seed = rig.backend.all_sessions("talker")[1].received[0]
    assert not any(word in seed for word in ("Giulia", "Fido", "Rome"))
    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert [said(e.message) for e in saved.entries] == ["Hello again."]


async def test_a_reset_that_cannot_delete_a_file_is_reset_failed_and_still_stores_its_time(tmp_path: Path) -> None:
    an_earlier_launch(tmp_path)
    (tmp_path / "conversation.unreadable-2.json").mkdir()

    async with a_rig(tmp_path, [[Text("Hello, sir.")]]) as rig:
        result = await rig.send("reset-1", "reset_context")
        stored = rig.log.reset_time

    assert result["ok"] is False and result["error"]["code"] == "reset_failed"
    assert stored is not None
    assert not (tmp_path / "memory").exists() and not (tmp_path / "conversation.json").exists()


async def test_a_reset_that_can_neither_delete_a_file_nor_store_its_time_is_reset_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    an_earlier_launch(tmp_path)
    (tmp_path / "conversation.unreadable-2.json").mkdir()

    def broken(at: float, **_: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    async with a_rig(tmp_path, [[Text("Hello, sir.")]]) as rig:
        monkeypatch.setattr(rig.log, "set_reset_time", broken)
        result = await rig.send("reset-1", "reset_context")

    assert result["ok"] is False and result["error"]["code"] == "reset_failed"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and renames past file permissions")
def test_a_reset_that_cannot_delete_one_file_deletes_the_others_and_saves_over_the_one_never_set_aside(
    tmp_path: Path
) -> None:
    an_earlier_launch(tmp_path)
    path = tmp_path / "conversation.json"
    (tmp_path / "conversation.unreadable-2.json").mkdir()
    conversation = Conversation(path)
    path.chmod(0)
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(UnreadableFile, match="nothing will be saved over it"):
            conversation.load()
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)

    with pytest.raises(OSError):
        conversation.reset()
    conversation.add("<user>Hello again.</user>", "Hello, sir.", tools=(), device="Mac", kind="user", at=990.0,
                     ended=990.5)

    assert not (tmp_path / "memory").exists() and not (tmp_path / "conversation.unreadable.json").exists()
    saved = Conversation(path)
    saved.load()
    assert [said(e.message) for e in saved.entries] == ["Hello again."] and saved.summary is None


async def test_a_ceiling_restart_seeding_when_the_reset_lands_is_never_swapped_in_or_told(tmp_path: Path) -> None:
    seeding = asyncio.Event()
    async with a_rig(tmp_path, [[Text("Noted."), tokens(300_000)], [Text("Hello, sir.")]],
                     seed_reply=[Gate(seeding), Text("Ready.")]) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        await until(lambda: len(rig.backend.all_sessions("talker")) == 2)

        await rig.send("reset-1", "reset_context")
        seeding.set()
        await until(lambda: len(rig.backend.all_sessions("talker")) == 3)
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Hello again."))

    _, restarted, fresh = rig.backend.all_sessions("talker")
    assert restarted.received[0].startswith('<event source="hub" reason="restart"')
    assert restarted.closed and len(restarted.received) == 1
    assert fresh.received[0].startswith('<event source="hub" reason="reset"') and "Giulia" not in fresh.received[0]
    assert "Hello again." in fresh.received[1]
    assert rig.output.notices == []


async def test_a_ceiling_restart_waiting_for_a_quiet_voice_when_the_reset_lands_opens_no_session(
    tmp_path: Path
) -> None:
    voice = Voice()
    async with a_rig(tmp_path, [[Text("Noted."), tokens(300_000)], [Text("Hello, sir.")]], floor=voice) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        await rig.send("reset-1", "reset_context")
        voice.silent.set()
        await until(lambda: any('reason="reset"' in s.received[0] for s in rig.backend.all_sessions("talker")
                                if s.received))
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Hello again."))

    _, fresh = rig.backend.all_sessions("talker")
    assert fresh.received[0].startswith('<event source="hub" reason="reset"') and "Giulia" not in fresh.received[0]
    assert "Hello again." in fresh.received[1]
    assert rig.output.notices == []


async def test_a_reset_cancels_a_start_afresh_without_its_failure_notice(tmp_path: Path) -> None:
    late = asyncio.Event()
    async with a_rig(tmp_path, [[Text("Noted.")], AFRESH, [Text("Hello, sir.")]],
                     summaries=[[Gate(late), Text(a_summary("The User's sister is Giulia."))]]) as rig:
        await rig.hub_reply(await rig.send("say-1", "say_text", text="My sister is Giulia."))
        await rig.hub_reply(await rig.send("say-2", "say_text", text="Let's start afresh."))
        await until(lambda: len(rig.backend.all_sessions("summariser")) == 1)

        await rig.send("reset-1", "reset_context")
        [summariser] = rig.backend.all_sessions("summariser")
        await until(lambda: summariser.closed and len(rig.backend.all_sessions("talker")) == 2)
        late.set()
        await rig.hub_reply(await rig.send("say-3", "say_text", text="Hello again."))

    assert rig.output.notices == []
    assert not (tmp_path / "memory").exists()
    assert "Giulia" not in rig.backend.all_sessions("talker")[1].received[0]
    saved = json.loads((tmp_path / "conversation.json").read_text())
    assert saved["active_summary"] is None and [said(e["message"]) for e in saved["entries"]] == ["Hello again."]
