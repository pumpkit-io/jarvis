"""The opt-in classifier parses whole-segment decisions without granting Task authority."""

import asyncio
import json
import time
from dataclasses import replace

import pytest

from jarvis_hub.voice.admission import ClassifierFailure, CloudAssessor
from jarvis_hub.voice.assessment import Candidate
from tests.fakes import until


class Classifier:
    provider = "fake"
    model = "fixed"

    def __init__(self, result: str = '["ignore","floor"]') -> None:
        self.result = result
        self.payloads: list[str] = []
        self.prepares = 0
        self.closes = 0
        self.gate: asyncio.Event | None = None

    async def prepare(self) -> None:
        self.prepares += 1

    async def classify(self, payload: str) -> str:
        self.payloads.append(payload)
        if self.gate is not None:
            await self.gate.wait()
        return self.result

    async def close(self) -> None:
        self.closes += 1


def candidate() -> Candidate:
    return Candidate("private-id", "private-owner", "Lena, continue. Jarvis, only API changes.",
                     "Review the repo", "", "thinking", True,
                     ("Lena, continue.", "Jarvis, only API changes."))


async def ready(backend: Classifier, notices: list[str]) -> CloudAssessor:
    assessor = CloudAssessor(backend, notice=notices.append)
    assessor.start()
    await until(lambda: assessor.ready)
    return assessor


async def test_classifier_receives_only_observed_context_and_returns_exact_segment_actions() -> None:
    backend = Classifier()
    notices: list[str] = []
    assessor = await ready(backend, notices)
    c = candidate()
    try:
        result = await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))
        assert result.segment_actions == ("ignore", "floor")
        assert result.verdict == "directed"
        assert json.loads(backend.payloads[0]) == {
            "admitted_turn": "Review the repo", "played_text": "", "phase": "thinking",
            "started_before_reply": True, "observed_segments": list(c.observed_segments),
        }
        assert "private-id" not in backend.payloads[0] and "private-owner" not in backend.payloads[0]
        assert backend.prepares == 1 and len(backend.payloads) == 1
    finally:
        await assessor.close()


@pytest.mark.parametrize("raw", ['["floor"]', '["floor","maybe"]', '{"actions":["floor","floor"]}',
                                  '```json\n["floor","floor"]\n```', '[true,"floor"]', 'x' * 2049])
async def test_invalid_output_abstains_atomically_and_does_not_retry(raw: str) -> None:
    backend = Classifier(raw)
    assessor = await ready(backend, [])
    c = candidate()
    try:
        result = await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))
        assert result.verdict == "uncertain" and result.segment_actions is None
        assert result.reason == "invalid"
        await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))
        assert len(backend.payloads) == 1
    finally:
        await assessor.close()


async def test_deadline_abstains_before_cleanup_and_does_not_queue_more_queries() -> None:
    class SlowClose(Classifier):
        async def close(self) -> None:
            await cleanup.wait()
            await super().close()
    cleanup = asyncio.Event()
    backend = SlowClose()
    backend.gate = asyncio.Event()
    assessor = await ready(backend, [])
    c = candidate()
    try:
        started = time.perf_counter()
        result = await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id), timeout_s=0.02)
        assert result.reason == "timeout" and time.perf_counter() - started < 0.2
        waiting = await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))
        assert waiting.verdict == "uncertain" and len(backend.payloads) == 1
    finally:
        cleanup.set()
        await assessor.close()


async def test_old_owner_result_does_not_disable_a_fresh_later_candidate() -> None:
    backend = Classifier()
    backend.gate = asyncio.Event()
    assessor = await ready(backend, [])
    c = candidate()
    owner = (c.candidate_id, c.turn_id)
    try:
        pending = asyncio.create_task(assessor.assess(c, current=lambda: owner))
        await until(lambda: len(backend.payloads) == 1)
        owner = ("next", "next-turn")
        backend.gate.set()
        assert (await pending).reason == "stale"
        fresh = replace(c, candidate_id=owner[0], turn_id=owner[1])
        assert (await assessor.assess(fresh, current=lambda: owner)).segment_actions == ("ignore", "floor")
        assert assessor.ready
    finally:
        await assessor.close()


async def test_a_usage_limit_is_visible_and_never_retried_automatically() -> None:
    class Limited(Classifier):
        async def classify(self, payload: str) -> str:
            self.payloads.append(payload)
            raise ClassifierFailure("rate_limited")
    backend = Limited()
    notices: list[str] = []
    assessor = await ready(backend, notices)
    c = candidate()
    try:
        assert (await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))).reason == "rate_limited"
        await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))
        assert len(backend.payloads) == 1 and not assessor.ready
        assert any("usage-limited" in notice for notice in notices)
    finally:
        await assessor.close()


async def test_context_overflow_never_sends_a_truncated_opening() -> None:
    backend = Classifier()
    assessor = await ready(backend, [])
    c = replace(candidate(), admitted_turn="opening" * 1000)
    try:
        assert (await assessor.assess(c, current=lambda: (c.candidate_id, c.turn_id))).reason == "context_limit"
        assert backend.payloads == []
    finally:
        await assessor.close()


def test_disabled_factory_does_not_inspect_cli_or_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path
    import shutil
    from jarvis_hub.voice.admission import configured_assessor
    def forbidden(_name: str) -> str:
        raise AssertionError("Disabled admission must not inspect a provider")
    monkeypatch.setattr(shutil, "which", forbidden)
    notices: list[str] = []
    assert configured_assessor("off", "sonnet", Path("/absent/prompt"), notice=notices.append) is None


async def test_stale_deadline_cleanup_rechecks_readiness_without_replaying_old_speech() -> None:
    backend = Classifier()
    backend.gate = asyncio.Event()
    assessor = await ready(backend, [])
    c = candidate()
    owner = (c.candidate_id, c.turn_id)
    try:
        pending = asyncio.create_task(assessor.assess(c, current=lambda: owner, timeout_s=0.02))
        await until(lambda: len(backend.payloads) == 1)
        owner = ("fresh", "fresh-owner")
        assert (await pending).reason == "stale"
        await until(lambda: assessor.ready)
        assert backend.prepares == 2 and len(backend.payloads) == 1
        backend.gate.set()
        fresh = replace(c, candidate_id=owner[0], turn_id=owner[1])
        assert (await assessor.assess(fresh, current=lambda: owner)).verdict == "directed"
    finally:
        await assessor.close()


async def test_real_provider_failure_is_not_hidden_by_owner_staleness() -> None:
    class Limited(Classifier):
        async def classify(self, payload: str) -> str:
            self.payloads.append(payload)
            await release.wait()
            raise ClassifierFailure("rate_limited")
    release = asyncio.Event()
    backend = Limited()
    notices: list[str] = []
    assessor = await ready(backend, notices)
    c = candidate()
    owner = c.candidate_id, c.turn_id
    try:
        pending = asyncio.create_task(assessor.assess(c, current=lambda: owner))
        await until(lambda: len(backend.payloads) == 1)
        owner = "next", "next-owner"
        release.set()
        assert (await pending).reason == "rate_limited"
        assert not assessor.ready and any("usage-limited" in notice for notice in notices)
    finally:
        await assessor.close()
