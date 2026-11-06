"""Offline controlled latency evidence; queue estimates never substitute for physical observations."""

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
import math
import re
from typing import Any, cast

from jarvis_hub.stats import percentile

ELIGIBLE = ("direct_answer", "delegated_work")
CATEGORIES = (*ELIGIBLE, "typed", "announcement", "warmup", "chime", "cached", "other")


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Latency input must contain JSON objects.")
    return cast(dict[str, Any], value)


def _items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Latency samples must be an array.")
    return [_object(item) for item in cast(list[object], value)]


def _id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("Latency identifiers must be nonempty opaque labels.")
    return value


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("Timing values must be finite nonnegative numbers.")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("Timing values must be finite nonnegative numbers.") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("Timing values must be finite nonnegative numbers.")
    return number


def _integer(value: object, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError("Turn and revision identifiers must be valid integers.")
    return value


def _wall(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A timezone-aware declaration and metric time are required.")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("A timezone-aware declaration and metric time are required.") from None
    if result.tzinfo is None:
        raise ValueError("A timezone-aware declaration and metric time are required.")
    return result


def _key(row: dict[str, Any]) -> tuple[str, int, str | None, int | None]:
    owner, revision = row.get("request_id"), row.get("revision")
    if (owner is None) != (revision is None):
        raise ValueError("Request identity and revision must be supplied together.")
    return (_id(row.get("run_id")), _integer(row.get("turn"), 1),
            None if owner is None else _id(owner), None if revision is None else _integer(revision))


def _optional(row: dict[str, Any], name: str) -> float | None:
    value = row.get(name)
    return None if value is None else _number(value)


def _difference(end: float | None, start: float | None) -> float | None:
    return None if end is None or start is None else _number(end - start)


def _physical(observation: dict[str, Any]) -> float | None:
    physical = observation.get("physical")
    if physical is None:
        return None
    physical = _object(physical)
    _id(physical.get("clock_id"))
    if physical.get("reviewed") is not True:
        raise ValueError("Physical observations need human review.")
    return _number((_number(physical.get("first_audio_s")) - _number(physical.get("speech_end_s"))) * 1000)


def _summary(values: list[float]) -> dict[str, int | float | None]:
    return {"n": len(values), "p50_ms": percentile(values, 50), "p90_ms": percentile(values, 90)}


def latency_report(plan_data: object, observation_data: object, rows: Iterable[object], *, plan_sha256: str) -> dict[str, Any]:
    plan, observations = _object(plan_data), _object(observation_data)
    if plan.get("version") != 1 or isinstance(plan.get("version"), bool):
        raise ValueError("Unsupported latency plan version.")
    declared = _wall(plan.get("declared_at"))
    if plan.get("measurement") not in ("physical", "queue_estimate", "synthetic"):
        raise ValueError("Declare physical, queue_estimate or synthetic measurement in the frozen plan.")
    if observations.get("plan_sha256") != plan_sha256 or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise ValueError("Observations do not reference the frozen plan hash.")
    samples: dict[str, dict[str, Any]] = {}
    for sample in _items(plan.get("samples")):
        sid = _id(sample.get("sample_id"))
        if sid in samples or "eligible" in sample:
            raise ValueError("Duplicate sample or post-hoc eligibility override.")
        category = sample.get("category")
        if (category not in CATEGORIES or sample.get("language") not in ("en", "it", None)
                or (sample.get("language") is None and category in ELIGIBLE)):
            raise ValueError("Every planned request needs its category and EN/IT language.")
        if category == "other" and sample.get("exclusion_reason") not in ("setup", "exploratory", "ineligible_source"):
            raise ValueError("Other exclusions need a predeclared reason category.")
        samples[sid] = sample
    if not samples:
        raise ValueError("The latency plan has no samples.")
    bindings: dict[str, dict[str, Any]] = {}
    keys: set[tuple[str, int, str | None, int | None]] = set()
    for observation in _items(observations.get("samples")):
        sid = _id(observation.get("sample_id"))
        if sid not in samples or sid in bindings:
            raise ValueError("Unknown or duplicate observed sample.")
        if observation.get("outcome") not in ("completed", "failed", "not_attempted"):
            raise ValueError("Observed outcomes must be completed, failed or not_attempted.")
        if observation["outcome"] == "failed" and observation.get("failure_reason", "unknown") not in (
            "capture_failure", "backend_failure", "no_audio", "interrupted", "superseded", "not_request_specific", "unknown"
        ):
            raise ValueError("Failure reasons must use a nonsecret diagnostic category.")
        fields = ("run_id", "turn", "request_id", "revision")
        has_key = any(field in observation for field in fields)
        if has_key:
            key = _key(observation)
            if key in keys or observation["outcome"] == "not_attempted":
                raise ValueError("Duplicate correlation or a not-attempted sample bound to a turn.")
            if samples[sid]["category"] in ELIGIBLE and key[2] is None:
                raise ValueError("Eligible speech needs request identity and revision.")
            keys.add(key)
        elif observation["outcome"] == "completed" and samples[sid]["category"] in ELIGIBLE:
            raise ValueError("Completed observations need exact run/turn/request/revision correlation.")
        bindings[sid] = observation
    metrics: dict[tuple[str, int, str | None, int | None], dict[str, list[dict[str, Any]]]] = {}
    input_turns: list[dict[str, Any]] = []
    inventory: Counter[str] = Counter()
    invalid_rows = 0
    for raw in rows:
        if not isinstance(raw, dict):
            invalid_rows += 1
            continue
        row = dict(cast(dict[str, Any], raw))
        kind = row.get("kind")
        known = ("user", "speech", "speech_stop", "turn", "capture", "recognition", "spoken_relation",
                 "playback_reference", "warmup", "event", "task", "bargein", "voice", "usage_limit")
        inventory[kind if isinstance(kind, str) and kind in known else "other"] += 1
        names = [name for name in row if name != "at" and (name.endswith("_ms") or name.endswith("_at"))]
        try:
            for name in names:
                _optional(row, name)
        except ValueError:
            invalid_rows += 1
            row["invalid_timing"] = True
        if kind == "turn":
            input_turns.append(row)
        if kind not in ("user", "speech", "speech_stop"):
            continue
        try:
            key = _key(row)
        except ValueError:
            invalid_rows += 1
            continue
        metrics.setdefault(key, {}).setdefault(kind, []).append(row)
    runs = {key[0] for key in keys}
    unbound = sum(len(entries.get("user", [])) + len(entries.get("speech", [])) + len(entries.get("speech_stop", []))
                  for key, entries in metrics.items() if key[0] in runs and key not in keys)
    ambiguous = sum(any(len(events) > 1 for events in entries.values()) for key, entries in metrics.items() if key[0] in runs)
    results: list[dict[str, Any]] = []
    counts: Counter[str] = Counter(planned=len(samples))
    excluded_observations: Counter[str] = Counter()
    invalid_observations = 0
    for sid, sample in samples.items():
        category = sample["category"]
        observation = bindings.get(sid)
        result: dict[str, Any] = dict(sample_id=sid, language=sample.get("language"), category=category,
                                     status="missing", reported_outcome=observation["outcome"] if observation else "missing",
                                     queue_ms=None, physical_ms=None, components={})
        result["correlation"] = (dict(zip(("run_id", "turn", "request_id", "revision"), _key(observation)))
                                 if observation is not None and "run_id" in observation else None)
        results.append(result)
        if category not in ELIGIBLE:
            counts["excluded"] += 1
            result["status"] = "excluded"
            excluded_observations[observation["outcome"] if observation else "missing"] += 1
            if observation is not None:
                try:
                    _physical(observation)
                except ValueError:
                    invalid_observations += 1
                    result["reason"] = "invalid_excluded_annotation"
            continue
        counts["eligible"] += 1
        if observation is None:
            counts["missing"] += 1
            continue
        outcome = observation["outcome"]
        try:
            result["physical_ms"] = _physical(observation)
        except ValueError:
            result["status"] = "invalid"
            result["reason"] = "invalid_physical_annotation"
            counts["invalid"] += 1
            invalid_observations += 1
            continue
        if outcome != "completed":
            result["status"] = outcome
            if outcome == "failed":
                result["reason"] = observation.get("failure_reason", "unknown")
            counts[outcome] += 1
            continue
        try:
            key = _key(observation)
            entries = metrics.get(key, {})
            users, speeches, stopped = entries.get("user", []), entries.get("speech", []), entries.get("speech_stop", [])
            if len(users) > 1 or len(speeches) > 1 or len(stopped) > 1:
                raise ValueError("ambiguous_events")
            if not users:
                result["status"] = "missing"
                result["reason"] = "missing_user_result"
                counts["missing"] += 1
                continue
            user = users[0]
            if user.get("outcome") not in ("completed", "failed", "interrupted", "limited") or user.get("invalid_timing"):
                raise ValueError("invalid_user_result")
            if stopped or user["outcome"] != "completed" or observation.get("request_specific") is False:
                result["status"] = "failed"
                result["reason"] = "failed_superseded_or_not_request_specific"
                counts["failed"] += 1
                continue
            if not speeches:
                result["status"] = "missing"
                result["reason"] = "missing_generated_audio"
                counts["missing"] += 1
                continue
            speech = speeches[0]
            if speech.get("invalid_timing"):
                raise ValueError("invalid_speech_timing")
            if observation.get("request_specific") is not True:
                raise ValueError("missing_request_specific_review")
            if user.get("role") != "talker" or user.get("time_basis") != "perf_counter" or speech.get("time_basis") != "perf_counter":
                raise ValueError("unverified_clock_or_role")
            if _wall(user.get("at")) < declared:
                raise ValueError("plan_declared_after_result")
            request_at = _number(user.get("request_at"))
            if _number(speech.get("request_at")) != request_at:
                raise ValueError("conflicting_request_end")
            tools = user.get("tools")
            if not isinstance(tools, list):
                raise ValueError("invalid_tool_metadata")
            tool_names = cast(list[object], tools)
            if any(not isinstance(name, str) for name in tool_names) or ("delegate" in tool_names) != (category == "delegated_work"):
                raise ValueError("delegated_subset_mismatch")
            if speech.get("meaningful_audio_basis") != "queue_estimate":
                raise ValueError("unknown_audio_basis")
            queue = _optional(speech, "first_meaningful_audio_ms")
            if queue is None:
                result["status"] = "missing"
                result["reason"] = "missing_generated_audio"
                counts["missing"] += 1
                continue
            wait, sentence = _optional(user, "wait_ms"), _optional(user, "first_sentence_ms")
            chunk = _optional(speech, "first_chunk_ms")
            components = dict(hub_wait_ms=wait, generation_to_sentence_ms=_difference(sentence, wait),
                              sentence_to_chunk_ms=_difference(chunk, sentence),
                              chunk_to_audio_ms=_optional(speech, "chunk_to_audio_ms"),
                              capture_backlog_ms=None, contextual_assessment_ms=None)
            matches = [row for row in input_turns if row.get("run_id") == key[0] and row.get("request_at") == request_at]
            if len(matches) > 1 or any(row.get("invalid_timing") for row in matches):
                raise ValueError("ambiguous_input_turn")
            components.update(endpoint_wait_ms=_optional(matches[0], "end_wait_ms") if matches else None,
                              stt_ms=_optional(matches[0], "stt_ms") if matches else None)
            result.update(status="completed", queue_ms=queue, physical_ms=result["physical_ms"], components=components)
            counts["completed"] += 1
        except ValueError:
            result["status"] = "invalid"
            result["reason"] = "invalid_or_ambiguous_evidence"
            counts["invalid"] += 1
    groups: dict[str, dict[str, dict[str, int | float | None]]] = {}
    for basis, name in (("queue_ms", "queue_estimate"), ("physical_ms", "physical")):
        groups[name] = {}
        for group in ("all", "en", "it", *ELIGIBLE):
            values = [cast(float, row[basis]) for row in results if row["status"] == "completed" and row[basis] is not None
                      and (group == "all" or row["language"] == group or row["category"] == group)]
            groups[name][group] = _summary(values)
    physical = groups["physical"]
    all_physical, delegated = physical["all"], physical["delegated_work"]
    covered = all(physical[group]["n"] for group in ("en", "it", *ELIGIBLE))
    complete = counts["eligible"] >= 30 and counts["completed"] == counts["eligible"] == all_physical["n"]
    met = plan["measurement"] == "physical" and complete and covered and not invalid_rows and not invalid_observations and not ambiguous and not unbound and (
        cast(float, all_physical["p50_ms"]) <= 1500 and cast(float, all_physical["p90_ms"]) <= 2500
        and cast(float, delegated["p50_ms"]) <= 1500)
    return dict(plan_sha256=plan_sha256, measurement=plan["measurement"], counts={name: counts[name] for name in
                ("planned", "eligible", "excluded", "completed", "failed", "missing", "not_attempted", "invalid")},
                excluded_observations=dict(excluded_observations),
                reported_outcomes=dict(Counter(observation["outcome"] for observation in bindings.values())),
                metric_inventory=dict(inventory),
                invalid_metric_rows=invalid_rows, invalid_observations=invalid_observations,
                ambiguous_metric_groups=ambiguous, unbound_response_events=unbound, samples=results, **groups,
                component_availability={name: dict(
                    available=sum(row["components"].get(name) is not None for row in results if row["category"] in ELIGIBLE),
                    unavailable=counts["eligible"] - sum(row["components"].get(name) is not None for row in results if row["category"] in ELIGIBLE))
                    for name in ("hub_wait_ms", "generation_to_sentence_ms", "sentence_to_chunk_ms", "chunk_to_audio_ms",
                                 "endpoint_wait_ms", "stt_ms", "capture_backlog_ms", "contextual_assessment_ms")},
                physical_latency_targets="met" if met else "unmet", full_ticket_acceptance="not_assessed",
                historical_baseline=dict(source="b575016:docs/notes/phase-0-spikes.md:345",
                    fixed_prescribed=dict(n=10, reported_p50_ms=5455, reported_p90_ms=5877, includes_recognition_errors=True),
                    all_exchanges=dict(n=13, reported_p50_ms=5473, reported_p90_ms=7712,
                                       definition="Initial blank, ten prescribed prompts, extra Italian correction and closing."),
                    separate_failure=dict(reported_total_ms=66230, approximate_stt_interval_ms=61880,
                                          definition="Extra Italian correction outside the fixed prescribed sample."),
                    alignment="Speech end estimated by subtracting the 1000ms silence tail; not sample-accurate physical evidence."),
                component_definitions=dict(
                    hub_wait_ms="Speech end to Talker start, including endpoint/STT/delivery; not isolated queue time.",
                    generation_to_sentence_ms="Per-turn first sentence minus Talker-start wait, in the same process clock.",
                    sentence_to_chunk_ms="Per-turn first TTS chunk minus first sentence; includes scheduling/inference.",
                    chunk_to_audio_ms="First TTS chunk to estimated queue playback, not physical DAC output.",
                    endpoint_wait_ms="Input pipeline endpoint wait after its speech-end estimate.",
                    stt_ms="Recorded transcription processing interval, when exactly correlated.",
                    capture_backlog_ms="Unavailable per turn; capture summaries are not attributed by guesswork.",
                    contextual_assessment_ms="Unavailable without unambiguous accepted-revision correlation."),
                percentile_method="nearest rank: ceil(p/100*n), one-based; completed valid samples only",
                limitations=["Queue estimates are not physical ADC/DAC measurements.",
                             "Physical timestamps and request-specific review are supplied human evidence, not independently certified here.",
                             "Archive the plan hash before collection; a hash binds content, not its claimed chronology.",
                             "Capture backlog and contextual assessment are unavailable per turn when unambiguously correlated evidence is absent.",
                             "Component intervals are per turn; no component medians are subtracted."])
