import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jarvis_hub.agents.backend import PlanUsage, UsageWindow
from jarvis_hub.conversation import Conversation
from jarvis_hub.live import USAGE_EVERY, USAGE_STALE, LiveContext
from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, Text, Typist, lines, make_hub, until
from tests.test_uploaded_talk import line_headers, rig

# Friday 27 November 2026, 13:05 UTC.
FRIDAY = datetime(2026, 11, 27, 13, 5, tzinfo=UTC).timestamp()


async def test_a_mac_line_carries_the_macs_local_time_and_zone() -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text("Five past two, sir.")]])
    hub = make_hub(backend, RecordingOutput(), RecordingMetrics(), wall=lambda: FRIDAY,
                   live=LiveContext(mac_zone=lambda: "Europe/Rome"))

    await hub.start()
    await hub.run(lines(clock, "What time is it?"))
    await hub.close()

    assert backend.sessions[0].received[-1] == (
        "<now>2026-11-27 14:05 Friday, Europe/Rome (UTC+01:00)</now>\n"
        '<user lang="en" typed="true">What time is it?</user>'
    )


@pytest.mark.parametrize(("header", "now"), [
    ({"X-Jarvis-Time-Zone": "Asia/Tokyo"}, "<now>2026-11-27 22:05 Friday, Asia/Tokyo (UTC+09:00)</now>"),
    ({"X-Jarvis-Time-Zone": "../../etc/passwd"}, "<now>2026-11-27 14:05 Friday, Europe/Rome (UTC+01:00)</now>"),
    ({}, "<now>2026-11-27 14:05 Friday, Europe/Rome (UTC+01:00)</now>"),
])
async def test_a_phone_line_carries_the_zone_the_phone_reports_else_the_macs(
    tmp_path: Path, header: dict[str, str], now: str
) -> None:
    async with rig(tmp_path, wall=lambda: FRIDAY, live=LiveContext(mac_zone=lambda: "Europe/Rome")) as (
        session, _socket, door, _hub, _log, backend, _asr, phone, _clock, _pairing
    ):
        async with session.post(f"http://127.0.0.1:{door.port}/v1/lines", headers={**line_headers(phone.token), **header},
                                json={"version": 1, "text": "Che ore sono?"}) as response:
            assert response.status == 200
        await until(lambda: len(backend.session("talker").received) == 2)
        assert backend.session("talker").received[-1] == f'{now}\n<user lang="it" typed="true">Che ore sono?</user>'


async def test_the_saved_conversation_keeps_the_message_without_its_live_context(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text("Five past two, sir.")]])
    hub = make_hub(backend, RecordingOutput(), RecordingMetrics(), wall=lambda: FRIDAY,
                   conversation_path=tmp_path / "conversation.json", live=LiveContext(mac_zone=lambda: "Europe/Rome"))

    await hub.start()
    await hub.run(lines(clock, "What time is it?"))
    await hub.close()

    saved = Conversation(tmp_path / "conversation.json")
    saved.load()
    assert [entry.message for entry in saved.entries] == ['<user lang="en" typed="true">What time is it?</user>']


# Resets at 16:05 on Friday and at 09:00 on Monday 30 November, Rome time.
USAGE = PlanUsage(UsageWindow(23.4, FRIDAY + 2 * 3600), UsageWindow(41.0, datetime(2026, 11, 30, 8, tzinfo=UTC).timestamp()))
ROME_USAGE = '<claude_usage five_hour="23% used, resets 16:05" weekly="41% used, resets Monday 09:00"/>'


async def test_a_line_carries_the_plans_usage_once_until_it_changes() -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text("Plenty left.")], [Text("Noted.")]])
    backend.plan_usage = USAGE
    hub = make_hub(backend, RecordingOutput(), RecordingMetrics(), wall=lambda: FRIDAY,
                   live=LiveContext(mac_zone=lambda: "Europe/Rome"))

    await hub.start()
    await hub.run(lines(clock, "How much usage is left?", "Thanks"))
    await hub.close()

    assert backend.sessions[0].received[-2:] == [
        f"<now>2026-11-27 14:05 Friday, Europe/Rome (UTC+01:00)</now>\n{ROME_USAGE}\n"
        '<user lang="en" typed="true">How much usage is left?</user>',
        "<now>2026-11-27 14:05 Friday, Europe/Rome (UTC+01:00)</now>\n"
        '<user lang="en" typed="true">Thanks</user>',
    ]


async def talk(
    backend: FakeBackend, typist: Typist, text: str, *, usage: PlanUsage | None = None, after: float = 0.0
) -> str:
    """What the Talker gets for `text`, typed `after` seconds on, once the Hub has read `usage` by then."""
    if after:
        reads = backend.usage_reads
        backend.plan_usage = usage
        backend.clock.advance(after)
        await until(lambda: backend.usage_reads > reads)
    received = len(backend.session("talker").received)
    typist.type(text)
    await until(lambda: len(backend.session("talker").received) > received)
    return backend.session("talker").received[-1].split("\n<user")[0]


async def test_the_usage_is_told_again_once_a_window_moves_five_points_and_stops_once_stale() -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text("Noted.")]] * 4)
    backend.plan_usage = USAGE
    hub = make_hub(backend, RecordingOutput(), RecordingMetrics(), wall=lambda: FRIDAY + clock.now() - 1000,
                   live=LiveContext(mac_zone=lambda: "Europe/Rome"))
    typist = Typist(clock)
    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await until(lambda: backend.usage_reads == 1)

    first = await talk(backend, typist, "One")
    slight = await talk(backend, typist, "Two", usage=replace(USAGE, five_hour=UsageWindow(27.0, FRIDAY + 2 * 3600)),
                        after=USAGE_EVERY)
    moved = await talk(backend, typist, "Three", usage=replace(USAGE, five_hour=UsageWindow(28.6, FRIDAY + 2 * 3600)),
                       after=USAGE_EVERY)
    stale = await talk(backend, typist, "Four", usage=None, after=USAGE_STALE)
    typist.end()
    await running
    await hub.close()

    assert ROME_USAGE in first
    assert "<claude_usage" not in slight
    assert '<claude_usage five_hour="29% used, resets 16:05" weekly="41% used, resets Monday 09:00"/>' in moved
    assert "<claude_usage" not in stale


async def test_a_new_talker_session_is_told_the_usage_again() -> None:
    clock = FakeClock()
    backend = FakeBackend(clock, [[Text("Noted.")]] * 2)
    backend.plan_usage = USAGE
    hub = make_hub(backend, RecordingOutput(), RecordingMetrics(), wall=lambda: FRIDAY,
                   live=LiveContext(mac_zone=lambda: "Europe/Rome"))
    typist = Typist(clock)
    await hub.start()
    running = asyncio.create_task(hub.run(typist.lines()))
    await until(lambda: backend.usage_reads == 1)

    typist.type("One")
    await until(lambda: len(backend.session("talker").received) == 2)
    hub.reset_context()
    typist.type("Two")
    await until(lambda: len(talkers := backend.all_sessions("talker")) == 2 and len(talkers[1].received) == 2)
    typist.end()
    await running
    await hub.close()

    assert ROME_USAGE in backend.all_sessions("talker")[1].received[-1]
