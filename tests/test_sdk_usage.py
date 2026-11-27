"""How the SDK backend reads the plan's usage from the CLI's get_usage reply."""

from datetime import UTC, datetime

from jarvis_hub.agents.backend import PlanUsage, UsageWindow
from jarvis_hub.agents.sdk import plan_usage

RESETS = datetime(2026, 11, 27, 15, 5, tzinfo=UTC)


def test_the_five_hour_and_weekly_windows_come_from_the_rate_limits() -> None:
    reply = {
        "subscription_type": "max",
        "rate_limits_available": True,
        "rate_limits": {
            "five_hour": {"utilization": 23.4, "resets_at": "2026-11-27T15:05:00.000Z"},
            "seven_day": {"utilization": 41, "resets_at": None},
            "seven_day_opus": {"utilization": 90, "resets_at": "2026-11-30T08:00:00Z"},
        },
        "behaviors": None,
    }

    assert plan_usage(reply) == PlanUsage(UsageWindow(23.4, RESETS.timestamp()), UsageWindow(41.0))


def test_a_reply_without_usable_windows_reads_as_unknown() -> None:
    assert plan_usage({"rate_limits_available": False, "rate_limits": None}) is None
    assert plan_usage({"rate_limits": {"five_hour": None, "seven_day": {"utilization": "a lot"}}}) is None
    assert plan_usage({}) is None


def test_a_garbled_reset_time_keeps_the_percentage() -> None:
    reply = {"rate_limits": {"five_hour": {"utilization": 7, "resets_at": "soon"}}}

    assert plan_usage(reply) == PlanUsage(UsageWindow(7.0), None)
