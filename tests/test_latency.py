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
