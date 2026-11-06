"""Controlled latency reports keep their planned denominator and distinguish physical evidence."""

import copy
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from jarvis_hub.config import load_settings
from jarvis_hub.main import print_stats

from jarvis_hub.latency import latency_report


DIGEST = "a" * 64


def sample_data(count: int = 30) -> tuple[dict[str, Any], dict[str, Any], list[object]]:
    samples: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    rows: list[object] = []
    for index in range(count):
        category = "delegated_work" if index % 3 == 0 else "direct_answer"
        samples.append(dict(sample_id=f"s{index}", language="en" if index % 2 == 0 else "it", category=category))
        owner = dict(run_id="run1", turn=index + 1, request_id=f"r{index}", revision=0)
        observations.append(dict(sample_id=f"s{index}", outcome="completed", request_specific=True, **owner,
                                 physical=dict(clock_id=f"recording{index}", reviewed=True,
                                               speech_end_s=1.0, first_audio_s=2.0)))
        common = dict(time_basis="perf_counter", at="2026-11-06T10:01:00Z", request_at=float(index + 1), **owner)
        rows.extend([dict(kind="user", role="talker", outcome="completed", tools=["delegate"] if index % 3 == 0 else [],
                          wait_ms=600, first_sentence_ms=800, **common),
                     dict(kind="speech", meaningful_audio_basis="queue_estimate", first_meaningful_audio_ms=1000,
                          first_chunk_ms=900, chunk_to_audio_ms=100, **common)])
    return (dict(version=1, measurement="physical", declared_at="2026-11-06T10:00:00Z", samples=samples),
            dict(plan_sha256=DIGEST, samples=observations), rows)


def test_missing_planned_turn_blocks_latency_gate_without_shrinking_denominator() -> None:
    plan, observations, rows = sample_data()
    observations["samples"].pop()
    report = latency_report(plan, observations, rows[:-2], plan_sha256=DIGEST)
    assert report["counts"]["planned"] == 30
    assert report["counts"]["eligible"] == 30
    assert report["counts"]["missing"] == 1
    assert report["physical"]["all"]["n"] == 29
    assert report["physical"]["all"]["p50_ms"] == 1000
    assert report["physical_latency_targets"] == "unmet"




@pytest.mark.parametrize("physical", [True, False])
def test_physical_targets_require_physical_evidence_not_fast_queue_estimates(physical: bool) -> None:
    plan, observations, rows = sample_data()
    if not physical:
        for sample in observations["samples"]:
            sample.pop("physical")
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["queue_estimate"]["all"]["n"] == 30
    assert report["physical"]["all"]["n"] == (30 if physical else 0)
    assert report["physical_latency_targets"] == ("met" if physical else "unmet")
    assert report["full_ticket_acceptance"] == "not_assessed"


def test_delegated_acknowledgment_has_its_own_target_and_fractional_misses_are_not_rounded() -> None:
    plan, observations, rows = sample_data()
    for sample in observations["samples"]:
        if int(sample["sample_id"][1:]) % 3 == 0:
            sample["physical"]["first_audio_s"] = 2.5001
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["physical"]["all"]["p50_ms"] == 1000
    assert report["physical"]["delegated_work"]["p50_ms"] == pytest.approx(1500.1)
    assert report["physical_latency_targets"] == "unmet"


def test_a_failed_slow_turn_is_visible_and_cannot_be_dropped_into_a_passing_subset() -> None:
    plan, observations, rows = sample_data(31)
    observations["samples"][-1]["outcome"] = "failed"
    observations["samples"][-1]["physical"]["first_audio_s"] = 67.23
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["failed"] == 1
    assert report["counts"]["eligible"] == 31 and report["physical"]["all"]["n"] == 30
    assert report["samples"][-1]["physical_ms"] == pytest.approx(66230)
    assert report["physical_latency_targets"] == "unmet"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_bad_physical_times_are_not_zero_or_success(value: object) -> None:
    plan, observations, rows = sample_data()
    observations["samples"][0]["physical"]["first_audio_s"] = value
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["invalid"] == 1
    assert report["physical_latency_targets"] == "unmet"


@pytest.mark.parametrize("bad", ["duplicate_speech", "wrong_revision", "negative_queue", "conflicting_end", "late_plan"])
def test_ambiguous_or_mismatched_metric_events_do_not_supply_a_latency(bad: str) -> None:
    plan, observations, rows = sample_data()
    speech = cast(dict[str, Any], copy.deepcopy(rows[1]))
    if bad == "duplicate_speech":
        rows.append(speech)
    elif bad == "wrong_revision":
        speech["revision"] = 1
        rows[1] = speech
    elif bad == "negative_queue":
        speech["first_meaningful_audio_ms"] = -1
        rows[1] = speech
    elif bad == "conflicting_end":
        speech["request_at"] = 99
        rows[1] = speech
    else:
        plan["declared_at"] = "2026-11-07T00:00:00Z"
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["physical_latency_targets"] == "unmet"
    assert report["physical"]["all"]["n"] < 30


@pytest.mark.parametrize("bad", ["duplicate_plan", "duplicate_binding", "duplicate_correlation", "wrong_hash", "eligibility_override"])
def test_invalid_frozen_plan_or_binding_is_rejected_without_printing_data(bad: str) -> None:
    plan, observations, rows = sample_data()
    if bad == "duplicate_plan":
        plan["samples"].append(plan["samples"][0])
    elif bad == "duplicate_binding":
        observations["samples"].append(observations["samples"][0])
    elif bad == "duplicate_correlation":
        observations["samples"][1].update({key: observations["samples"][0][key] for key in ("run_id", "turn", "request_id", "revision")})
    elif bad == "wrong_hash":
        observations["plan_sha256"] = "b" * 64
    else:
        plan["samples"][0]["eligible"] = False
    with pytest.raises(ValueError):
        latency_report(plan, observations, rows, plan_sha256=DIGEST)




def test_stats_cli_path_is_offline_hash_bound_and_cannot_filter_away_planned_turns(tmp_path: Path) -> None:
    plan, observations, rows = sample_data()
    plan_file, observation_file, metric_file = (tmp_path / name for name in ("plan.json", "observations.json", "metrics.jsonl"))
    encoded = json.dumps(plan).encode()
    plan_file.write_bytes(encoded)
    observations["plan_sha256"] = hashlib.sha256(encoded).hexdigest()
    observation_file.write_text(json.dumps(observations))
    metric_file.write_text("".join(json.dumps(row) + "\n" for row in rows))
    settings = load_settings(tmp_path, {})
    output = io.StringIO()
    print_stats(settings, None, None, output, latency_plan=plan_file, latency_observations=observation_file,
                metrics_path=metric_file)
    assert json.loads(output.getvalue())["physical_latency_targets"] == "met"
    with pytest.raises(ValueError):
        print_stats(settings, "24h", None, io.StringIO(), latency_plan=plan_file,
                    latency_observations=observation_file, metrics_path=metric_file)


def test_excluded_categories_and_not_attempted_are_counted_without_entering_percentiles() -> None:
    plan, observations, rows = sample_data()
    for category in ("typed", "announcement", "warmup", "chime", "cached"):
        plan["samples"].append(dict(sample_id=category, category=category, language=None))
        observations["samples"].append(dict(sample_id=category, outcome="not_attempted"))
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["planned"] == 35 and report["counts"]["eligible"] == 30
    assert report["counts"]["excluded"] == 5
    assert report["excluded_observations"]["not_attempted"] == 5
    assert report["physical"]["all"]["n"] == 30


def test_synthetic_run_cannot_claim_the_physical_gate_even_with_annotation_shaped_data() -> None:
    plan, observations, rows = sample_data()
    plan["measurement"] = "synthetic"
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["physical_latency_targets"] == "unmet"


def test_stopped_generation_is_a_failure_even_when_no_generated_audio_was_logged() -> None:
    plan, observations, rows = sample_data()
    user = cast(dict[str, Any], rows[0])
    user["outcome"] = "interrupted"
    stopped = copy.deepcopy(cast(dict[str, Any], rows[1]))
    stopped["kind"] = "speech_stop"
    rows[1] = stopped
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["failed"] == 1 and report["counts"]["missing"] == 0
    assert report["physical_latency_targets"] == "unmet"


def test_malformed_tool_metadata_cannot_claim_delegation_or_a_latency_pass() -> None:
    plan, observations, rows = sample_data()
    cast(dict[str, Any], rows[2])["tools"] = [{"private": "PRIVATE_SENTINEL"}]
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["invalid"] == 1 and report["physical_latency_targets"] == "unmet"
    assert "PRIVATE_SENTINEL" not in json.dumps(report)


def chronology_data() -> tuple[dict[str, Any], dict[str, Any], list[object]]:
    plan, observations, rows = sample_data()
    user, speech = cast(dict[str, Any], rows[0]), cast(dict[str, Any], rows[1])
    user.update(started_at=1.6, completed_at=2.1, duration_ms=500, total_ms=1100, first_delta_ms=700)
    speech.update(started_at=1.65, first_audio_ms=1000)
    rows.append(dict(kind="turn", run_id="run1", time_basis="perf_counter", at="2026-11-06T10:00:59Z",
                     request_at=1.0, capture_start_at=.5, endpoint_at=1.4,
                     stt_started_at=1.4, stt_completed_at=1.5, stt_ms=100, end_wait_ms=400))
    return plan, observations, rows


@pytest.mark.parametrize("bad", ["audio_before_chunk", "backward_stt", "bad_speech_wall", "backward_hub",
                                    "backward_capture", "backward_endpoint", "stt_before_endpoint", "wrong_input_clock",
                                    "missing_input_clock", "bad_input_wall", "bad_stop_wall", "inconsistent_stt",
                                    "inconsistent_wait", "inconsistent_chunk", "chunk_before_reply_start", "partial_hub_pair", "stop_before_reply_start"])
def test_relevant_metric_chronology_invalidates_sample_and_physical_gate(bad: str) -> None:
    plan, observations, rows = chronology_data()
    user, speech, capture = cast(dict[str, Any], rows[0]), cast(dict[str, Any], rows[1]), cast(dict[str, Any], rows[-1])
    if bad == "audio_before_chunk": speech["first_meaningful_audio_ms"] = 100
    elif bad == "backward_stt": capture.update(stt_started_at=100, stt_completed_at=90)
    elif bad == "bad_speech_wall": speech["at"] = "not-a-clock"
    elif bad == "backward_hub": user["completed_at"] = 1.5
    elif bad == "backward_capture": capture["capture_start_at"] = 1.1
    elif bad == "backward_endpoint": capture["endpoint_at"] = .9
    elif bad == "stt_before_endpoint": capture["stt_started_at"] = 1.3
    elif bad == "wrong_input_clock": capture["time_basis"] = "wall"
    elif bad == "missing_input_clock": capture.pop("time_basis")
    elif bad == "bad_input_wall": capture["at"] = "PRIVATE_SENTINEL"
    elif bad == "bad_stop_wall": rows.append(dict(speech, kind="speech_stop", at="PRIVATE_SENTINEL"))
    elif bad == "inconsistent_stt": capture["stt_ms"] = 99 - .01
    elif bad == "inconsistent_wait": user["wait_ms"] = 590
    elif bad == "inconsistent_chunk": speech["chunk_to_audio_ms"] = 110
    elif bad == "chunk_before_reply_start": speech["started_at"] = 1.95
    elif bad == "partial_hub_pair": user.pop("started_at")
    else: rows.append(dict(speech, kind="speech_stop", stopped_at=1.6))
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["eligible"] == 30
    assert report["counts"]["invalid"] == 1
    assert report["invalid_metric_rows"] >= 1
    assert report["physical"]["all"]["n"] == 29
    assert report["physical_latency_targets"] == "unmet"
    assert "PRIVATE_SENTINEL" not in json.dumps(report)


def test_valid_streaming_and_independent_millisecond_rounding_are_preserved() -> None:
    plan, observations, rows = chronology_data()
    speech = cast(dict[str, Any], rows[1])
    speech.update(first_chunk_ms=900, first_meaningful_audio_ms=1001, first_audio_ms=1001, chunk_to_audio_ms=100)
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["physical_latency_targets"] == "met"
    assert report["invalid_metric_rows"] == report["counts"]["invalid"] == 0
    assert report["samples"][0]["queue_ms"] == 1001


@pytest.mark.parametrize("missing", ["stt_started_at", "stt_completed_at"])
def test_partial_stt_timestamp_pair_is_invalid(missing: str) -> None:
    plan, observations, rows = chronology_data()
    cast(dict[str, Any], rows[-1]).pop(missing)
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["invalid"] == report["invalid_metric_rows"] == 1
    assert report["physical_latency_targets"] == "unmet"


def test_missing_input_component_pairs_remain_unavailable_not_invented() -> None:
    plan, observations, rows = chronology_data()
    capture = cast(dict[str, Any], rows[-1])
    capture.pop("stt_started_at"); capture.pop("stt_completed_at")
    capture.pop("endpoint_at")
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["samples"][0]["components"]["stt_ms"] is None
    assert report["samples"][0]["components"]["endpoint_wait_ms"] is None
    assert report["counts"]["invalid"] == 0


def test_canceled_future_queue_estimate_is_a_failed_sample_not_invalid_evidence() -> None:
    plan, observations, rows = chronology_data()
    speech = cast(dict[str, Any], rows[1])
    rows.append(dict(speech, kind="speech_stop", stopped_at=1.7))
    report = latency_report(plan, observations, rows, plan_sha256=DIGEST)
    assert report["counts"]["failed"] == 1
    assert report["counts"]["invalid"] == report["invalid_metric_rows"] == 0
    assert report["samples"][0]["reason"] == "failed_superseded_or_not_request_specific"
    assert report["physical"]["all"]["n"] == 29
    assert report["physical_latency_targets"] == "unmet"
