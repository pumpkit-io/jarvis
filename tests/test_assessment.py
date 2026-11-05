"""Bounded contextual assessment while the existing Hub keeps conversing."""

import asyncio
import time

from jarvis_hub.voice.assessment import AssessmentWorker, Candidate
from tests.test_voice_mode import MakeRig, Rig, rig_for as rig_for
from tests.fakes import Text, until
from tests.test_input_pipeline import rig as input_rig


def stalled(candidate: Candidate) -> str:
    time.sleep(10)
    return "directed"


def directed(candidate: Candidate) -> str:
    return "directed"


async def test_stalled_assessment_does_not_freeze_hub_input_or_playback(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("The checks passed, sir.")])
    worker = AssessmentWorker(stalled)
    try:
        pending = asyncio.create_task(worker.assess(Candidate("c1", "t1", "stop", "", ""),
                                                   current=lambda: ("c1", "t1"), timeout_s=0.5))
        await asyncio.sleep(0)
        frames = input_rig()
        frames.pipeline.capture(True)
        frames.feed("speech", 1.0)
        frames.feed("silence", 0.6)
        assert frames.echo.frames == 160
        assert [event.text for event in frames.heard()] == ["Open a PR"]
        rig.audio.wake()
        rig.audio.say("How are the checks?")
        await until(lambda: "The checks passed, sir." in rig.player.texts)
        result = await pending
        assert result.verdict == "uncertain"
        assert result.reason == "timeout"
        assert rig.user_messages() == ['<user lang="en">How are the checks?</user>']
    finally:
        await worker.close()


async def test_stale_or_oversized_candidate_cannot_supply_an_accepted_decision() -> None:
    worker = AssessmentWorker(directed)
    try:
        stale = await worker.assess(Candidate("c1", "t1", "stop", "", ""),
                                    current=lambda: ("c2", "t2"), timeout_s=5)
        assert stale.verdict == "uncertain"
        assert stale.reason == "stale"
        oversized = await worker.assess(Candidate("c2", "t2", "x" * 6001, "", ""),
                                        current=lambda: ("c2", "t2"), timeout_s=5)
        assert oversized.reason == "context_limit"
    finally:
        await worker.close()


def invalid(candidate: Candidate) -> str:
    return "definitely directed because stop"


def unavailable(candidate: Candidate) -> str:
    raise FileNotFoundError("Local weights unavailable")


async def test_invalid_and_unavailable_recognition_leave_decision_uncertain() -> None:
    for recognize, reason in ((invalid, "invalid"), (unavailable, "unavailable")):
        worker = AssessmentWorker(recognize)
        try:
            result = await worker.assess(Candidate("c", "t", "Jarvis, stop", "", ""),
                                          current=lambda: ("c", "t"), timeout_s=5)
            assert result.verdict == "uncertain"
            assert result.reason == reason
        finally:
            await worker.close()


async def test_inflight_result_is_discarded_when_turn_changes_and_busy_work_is_not_queued() -> None:
    owner = ("c", "t")
    worker = AssessmentWorker(directed)
    try:
        pending = asyncio.create_task(worker.assess(Candidate("c", "t", "Jarvis, stop", "", ""),
                                                    current=lambda: owner, timeout_s=5))
        await asyncio.sleep(0)
        owner = ("new-c", "new-t")
        busy = await worker.assess(Candidate(*owner, "Actually, continue", "", ""),
                                   current=lambda: owner, timeout_s=5)
        assert busy.reason == "busy"
        result = await pending
        assert result.verdict == "uncertain"
        assert result.reason == "stale"
    finally:
        await worker.close()


async def test_unavailable_assessment_has_no_authority_over_playback_or_tasks(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("The first check passed. The second check passed.")])
    rig.player.hold = True
    rig.audio.wake()
    rig.audio.say("Explain the checks")
    await until(lambda: rig.state == "speaking")
    before = list(rig.player.events)
    worker = AssessmentWorker(unavailable)
    try:
        decision = await worker.assess(Candidate("c", "t", "Merge it now", "Explain the checks", ""),
                                       current=lambda: ("c", "t"), timeout_s=5)
        assert decision.verdict == "uncertain"
        rig.player.finish()
        await rig.advance(0.1)
        assert "pause" not in rig.player.events[len(before):]
        assert "stop" not in rig.player.events[len(before):]
        assert rig.backend.all_sessions("orchestrator") == []
        assert rig.user_messages() == ['<user lang="en">Explain the checks</user>']
        assert "The first check passed." in rig.player.played()
    finally:
        await worker.close()


def sometimes_stalled(candidate: Candidate) -> str:
    if candidate.transcript == "stall":
        time.sleep(10)
    return "directed"


async def test_timed_out_process_is_replaced_for_the_next_candidate() -> None:
    worker = AssessmentWorker(sometimes_stalled)
    try:
        first = await worker.assess(Candidate("c", "t", "stall", "", ""),
                                    current=lambda: ("c", "t"), timeout_s=0.1)
        assert first.reason == "timeout"
        second = await worker.assess(Candidate("c2", "t", "Jarvis, stop", "", ""),
                                     current=lambda: ("c2", "t"), timeout_s=5)
        assert second.verdict == "directed"
    finally:
        await worker.close()


async def test_contextual_verdicts_are_only_evidence_until_hub_policy_adopts_them(rig_for: MakeRig) -> None:
    rig: Rig = await rig_for([Text("The checks are still running, sir.")])
    rig.player.hold = True
    rig.audio.wake()
    rig.audio.say("Explain the checks")
    await until(lambda: rig.state == "speaking")
    worker = AssessmentWorker(directed)
    before = list(rig.player.events)
    try:
        assessment = await worker.assess(Candidate("c", "t", "Jarvis, merge it now", "Explain the checks", ""),
                                         current=lambda: ("c", "t"), timeout_s=5)
        assert assessment.verdict == "directed"
        await rig.settle()
        assert rig.player.events == before
        assert rig.backend.all_sessions("orchestrator") == []
        assert rig.user_messages() == ['<user lang="en">Explain the checks</user>']
        rig.player.finish()
        await rig.advance(0.1)
        assert "The checks are still running, sir." in rig.player.played()
    finally:
        await worker.close()


async def test_relation_contract_preserves_frozen_phase_without_admitting_work() -> None:
    candidate = Candidate('candidate-1', 'turn-1:revision-0', 'about the scheduler', 'Open an issue', '',
                          phase='thinking', started_before_reply=True)
    worker = AssessmentWorker(continue_when_thinking, verdicts=('stop_reply', 'amend', 'new_request', 'unrelated', 'uncertain'))
    try:
        result = await worker.assess(candidate, current=lambda: ('candidate-1', 'turn-1:revision-0'))
        assert result.verdict == 'amend'
    finally:
        await worker.close()


def continue_when_thinking(candidate: Candidate) -> str:
    return 'amend' if candidate.phase == 'thinking' and candidate.started_before_reply else 'uncertain'


def slow_prepare() -> None:
    time.sleep(0.15)


async def test_background_readiness_does_not_spend_the_candidate_deadline_on_cold_load() -> None:
    worker = AssessmentWorker(directed, prepare=slow_prepare)
    worker.start(readiness_s=5)
    try:
        candidate = Candidate('c', 't', 'Jarvis, stop', '', '')
        warming = await worker.assess(candidate, current=lambda: ('c', 't'), timeout_s=0.01)
        assert warming.reason == 'warming'
        await until(lambda: worker.ready, timeout=5)
        result = await worker.assess(candidate, current=lambda: ('c', 't'), timeout_s=0.5)
        assert result.verdict == 'directed'
    finally:
        await worker.close()


async def test_failed_readiness_is_bounded_and_does_not_restart_on_each_candidate() -> None:
    worker = AssessmentWorker(directed, prepare=slow_prepare)
    worker.start(readiness_s=0.01)
    try:
        await until(lambda: not worker.warming, timeout=5)
        for index in range(3):
            candidate = Candidate(str(index), 't', 'Jarvis, stop', '', '')
            result = await worker.assess(candidate, current=lambda: (str(index), 't'))
            assert result.reason == 'readiness_failed'
        assert not worker.ready
    finally:
        await worker.close()


async def test_a_prepared_worker_timeout_does_not_trigger_repeated_cold_restarts() -> None:
    worker = AssessmentWorker(sometimes_stalled)
    worker.start(readiness_s=5)
    try:
        await until(lambda: worker.ready, timeout=5)
        first = await worker.assess(Candidate('c', 't', 'stall', '', ''),
                                    current=lambda: ('c', 't'), timeout_s=0.02)
        assert first.reason == 'timeout'
        second = await worker.assess(Candidate('c2', 't', 'Jarvis, stop', '', ''),
                                     current=lambda: ('c2', 't'), timeout_s=0.02)
        assert second.reason == 'readiness_failed'
    finally:
        await worker.close()
