"""How the SDK backend reads a turn's context use out of the CLI's reports."""

from collections.abc import AsyncIterator

from claude_agent_sdk import ResultMessage, StreamEvent, SystemMessage
from claude_agent_sdk.types import ModelUsage

from jarvis_hub.agents.backend import Context, TurnEnd
from jarvis_hub.agents.sdk import SdkSession, turn_end


def model_usage(window: int, tokens: int) -> ModelUsage:
    return {
        "inputTokens": tokens, "outputTokens": 1, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        "webSearchRequests": 0, "costUSD": 0.0, "contextWindow": window, "maxOutputTokens": 8000,
    }


def result(**fields: object) -> ResultMessage:
    base: dict[str, object] = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")
    return ResultMessage(**{**base, **fields})  # type: ignore[arg-type]


def test_context_is_the_last_requests_prompt_tokens_against_the_models_window() -> None:
    models: dict[str, ModelUsage] = {"claude-haiku-5-5": model_usage(200_000, 1_185), "claude-sonnet-5-5": model_usage(1_000_000, 9_000)}
    last = {"input_tokens": 10, "cache_read_input_tokens": 4_000, "cache_creation_input_tokens": 500}
    end = turn_end(result(model_usage=models, usage={"input_tokens": 99_999}), "sonnet", last, now=0)
    assert end.context == Context(4_510, 1_000_000)


def test_without_a_per_model_report_there_is_no_context() -> None:
    last = {"input_tokens": 10}
    assert turn_end(result(), "sonnet", last, now=0).context is None
    assert turn_end(result(model_usage={"claude-sonnet-5-5": model_usage(200_000, 5)}), "sonnet", None, now=0).context is None


def test_an_alias_or_a_1m_variant_finds_its_full_id_beside_the_clis_haiku() -> None:
    models: dict[str, ModelUsage] = {"claude-haiku-5-5": model_usage(200_000, 9_000), "claude-sonnet-5-5": model_usage(1_000_000, 5)}
    for name in ("sonnet", "sonnet[1m]", "claude-sonnet-5-5", "claude-sonnet-5-5[1m]"):
        assert turn_end(result(model_usage=models), name, {"input_tokens": 1}, now=0).context == Context(1, 1_000_000), name


def test_a_model_name_that_matches_no_entry_gives_no_context_and_says_why() -> None:
    models: dict[str, ModelUsage] = {"claude-haiku-5-5": model_usage(200_000, 1_185), "other-big": model_usage(500_000, 9_000)}
    end = turn_end(result(model_usage=models), "sonnet", {"input_tokens": 1}, now=0)
    assert end.context is None
    assert end.no_context is not None and "sonnet" in end.no_context and "claude-haiku-5-5" in end.no_context


def test_an_alias_that_matches_two_entries_gives_no_context() -> None:
    models: dict[str, ModelUsage] = {"claude-sonnet-5-5": model_usage(1_000_000, 5), "claude-sonnet-4-6": model_usage(200_000, 5)}
    end = turn_end(result(model_usage=models), "sonnet", {"input_tokens": 1}, now=0)
    assert end.context is None and end.no_context is not None


async def test_the_session_matches_the_model_the_cli_reports_it_is_running() -> None:
    models: dict[str, ModelUsage] = {"claude-haiku-5-5": model_usage(200_000, 9_000), "claude-opus-5-5": model_usage(1_000_000, 5)}
    start = StreamEvent(uuid="u", session_id="s", event={"type": "message_start", "message": {"usage": {"input_tokens": 7}}})
    client = ScriptedClient([SystemMessage("init", {"model": "claude-opus-5-5"}), start, result(model_usage=models)])
    session = SdkSession(client, "default")  # type: ignore[arg-type]

    events = [e async for e in session.send("hi")]

    assert events[-1] == TurnEnd("completed", context=Context(7, 1_000_000))


class ScriptedClient:
    def __init__(self, messages: list[object]) -> None:
        self._messages = messages

    async def query(self, message: str) -> None:
        pass

    async def receive_response(self) -> AsyncIterator[object]:
        for m in self._messages:
            yield m
