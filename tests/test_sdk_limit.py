"""How the SDK backend tells a usage limit from other failures, and reads when it resets."""

import time
from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk import RateLimitEvent, ResultMessage
from claude_agent_sdk.types import RateLimitInfo

from jarvis_hub.agents.backend import TurnEnd
from jarvis_hub.agents.sdk import SdkSession, turn_end


def result(**fields: object) -> ResultMessage:
    base: dict[str, object] = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s")
    return ResultMessage(**{**base, **fields})  # type: ignore[arg-type]


def limit_event(
    status: str, resets_at: int | None = 1_900_000_000, kind: str = "five_hour", overage: str | None = None
) -> RateLimitEvent:
    info = RateLimitInfo(
        status=status,  # type: ignore[arg-type]
        resets_at=resets_at,
        rate_limit_type=kind,  # type: ignore[arg-type]
        overage_status=overage,  # type: ignore[arg-type]
    )
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")


def rejected(resets_at: int = 1_900_000_000) -> list[RateLimitInfo]:
    return [limit_event("rejected", resets_at).rate_limit_info]


def test_an_error_after_a_rejected_rate_limit_event_is_a_limit_with_the_events_reset_time() -> None:
    end = turn_end(result(result="You've hit your limit"), "sonnet", None, rejected(1_900_000_000))

    assert (end.outcome, end.resets_at) == ("limited", 1_900_000_000)


def test_a_bare_429_is_a_limit_with_no_reset_time() -> None:
    end = turn_end(result(api_error_status=429), "sonnet", None)

    assert (end.outcome, end.resets_at) == ("limited", None)


@pytest.mark.parametrize("status", [500, 529, None])
def test_other_errors_stay_failures(status: int | None) -> None:
    assert turn_end(result(api_error_status=status), "sonnet", None).outcome == "failed"


def test_a_turn_that_succeeded_is_not_a_limit_whatever_the_events_said() -> None:
    assert turn_end(result(is_error=False), "sonnet", None, rejected()).outcome == "completed"


def test_an_interrupt_is_not_a_limit() -> None:
    end = turn_end(result(terminal_reason="aborted_streaming"), "sonnet", None, rejected())

    assert end.outcome == "interrupted"


async def test_the_session_keeps_the_latest_rejected_event_and_ignores_warnings() -> None:
    client = ScriptedClient([limit_event("allowed_warning", 1), limit_event("rejected", 2), result()])
    session = SdkSession(client, "sonnet")  # type: ignore[arg-type]

    events = [e async for e in session.send("hi")]

    assert events == [TurnEnd("limited", error="the usage limit was reached", resets_at=2)]


@pytest.mark.parametrize(
    ("turns", "outcome"),
    [
        ([[limit_event("rejected"), result(api_error_status=429)]], "limited"),
        ([[limit_event("rejected", overage="allowed"), result(api_error_status=529)]], "failed"),
        ([[limit_event("rejected", overage="allowed_warning"), result(api_error_status=500)]], "failed"),
        ([[limit_event("rejected", overage="allowed"), result(api_error_status=429)]], "limited"),
        ([[limit_event("allowed_warning"), result(api_error_status=529)]], "failed"),
        ([[limit_event("rejected"), result(is_error=False)], [result(api_error_status=529)]], "failed"),
        ([[limit_event("rejected"), limit_event("allowed"), result(api_error_status=529)]], "failed"),
    ],
    ids=[
        "rejected and 429",
        "rejected with overage allowed and 529",
        "rejected with overage warning and 500",
        "rejected with overage allowed and 429",
        "a warning and 529",
        "a rejected event in an earlier turn",
        "a window rejected then allowed again",
    ],
)
async def test_what_counts_as_the_usage_limit(turns: list[list[object]], outcome: str) -> None:
    session = SdkSession(ScriptedClient(*turns), "sonnet")  # type: ignore[arg-type]

    ends = [[e async for e in session.send("hi")][-1] for _ in turns]

    assert isinstance(ends[-1], TurnEnd) and ends[-1].outcome == outcome


async def test_the_reset_is_the_latest_among_the_windows_still_rejected() -> None:
    client = ScriptedClient(
        [
            limit_event("rejected", 5_000, "seven_day"),
            limit_event("rejected", 2_000, "five_hour"),
            limit_event("rejected", 9_000, "seven_day_opus"),
            limit_event("allowed", 9_500, "seven_day_opus"),
            result(),
        ]
    )
    session = SdkSession(client, "sonnet")  # type: ignore[arg-type]

    events = [e async for e in session.send("hi")]

    assert events == [TurnEnd("limited", error="the usage limit was reached", resets_at=5_000)]


async def test_a_window_with_no_reset_time_counts_as_the_fallback_wait_so_an_earlier_known_reset_is_not_taken() -> None:
    soon = int(time.time()) + 60
    client = ScriptedClient(
        [limit_event("rejected", soon, "five_hour"), limit_event("rejected", None, "seven_day"), result()]
    )
    session = SdkSession(client, "sonnet")  # type: ignore[arg-type]

    events = [e async for e in session.send("hi")]

    assert events == [TurnEnd("limited", error="the usage limit was reached", resets_at=None)]


def test_a_known_reset_later_than_the_fallback_wait_wins_over_a_window_with_none() -> None:
    late = int(time.time()) + 7200
    windows = [limit_event("rejected", late).rate_limit_info, limit_event("rejected", None, "seven_day").rate_limit_info]

    assert turn_end(result(), "sonnet", None, windows).resets_at == late


class ScriptedClient:
    """Each `send` gets the next turn's messages."""

    def __init__(self, *turns: list[object]) -> None:
        self._turns = list(turns)

    async def query(self, message: str) -> None:
        pass

    async def receive_response(self) -> AsyncIterator[object]:
        for m in self._turns.pop(0):
            yield m
