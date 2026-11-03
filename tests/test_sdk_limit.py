"""How the SDK backend tells a usage limit from other failures, and reads when it resets."""

from collections.abc import AsyncIterator

import pytest
from claude_agent_sdk import RateLimitEvent, ResultMessage
from claude_agent_sdk.types import RateLimitInfo

from jarvis_hub.agents.backend import TurnEnd
from jarvis_hub.agents.sdk import SdkSession, turn_end


def result(**fields: object) -> ResultMessage:
    base: dict[str, object] = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s")
    return ResultMessage(**{**base, **fields})  # type: ignore[arg-type]


def limit_event(status: str, resets_at: int | None = 1_900_000_000, kind: str = "five_hour") -> RateLimitEvent:
    info = RateLimitInfo(status=status, resets_at=resets_at, rate_limit_type=kind)  # type: ignore[arg-type]
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")


def test_an_error_after_a_rejected_rate_limit_event_is_a_limit_with_the_events_reset_time() -> None:
    end = turn_end(result(result="You've hit your limit"), "sonnet", None, limit_event("rejected", 1_900_000_000))

    assert (end.outcome, end.resets_at) == ("limited", 1_900_000_000)


def test_a_bare_429_is_a_limit_with_no_reset_time() -> None:
    end = turn_end(result(api_error_status=429), "sonnet", None)

    assert (end.outcome, end.resets_at) == ("limited", None)


@pytest.mark.parametrize("status", [500, 529, None])
def test_other_errors_stay_failures(status: int | None) -> None:
    assert turn_end(result(api_error_status=status), "sonnet", None).outcome == "failed"


def test_a_turn_that_succeeded_is_not_a_limit_whatever_the_events_said() -> None:
    assert turn_end(result(is_error=False), "sonnet", None, limit_event("rejected")).outcome == "completed"


def test_an_interrupt_is_not_a_limit() -> None:
    end = turn_end(result(terminal_reason="aborted_streaming"), "sonnet", None, limit_event("rejected"))

    assert end.outcome == "interrupted"


async def test_the_session_keeps_the_latest_rejected_event_and_ignores_warnings() -> None:
    client = ScriptedClient(
        [limit_event("allowed_warning", 1), limit_event("rejected", 2), limit_event("allowed", 3), result()]
    )
    session = SdkSession(client, "sonnet")  # type: ignore[arg-type]

    events = [e async for e in session.send("hi")]

    assert events == [TurnEnd("limited", error="the usage limit was reached", resets_at=2)]


class ScriptedClient:
    def __init__(self, messages: list[object]) -> None:
        self._messages = messages

    async def query(self, message: str) -> None:
        pass

    async def receive_response(self) -> AsyncIterator[object]:
        for m in self._messages:
            yield m
