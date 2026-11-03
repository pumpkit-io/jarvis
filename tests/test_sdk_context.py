"""How the SDK backend reads a turn's context use out of the CLI's reports."""

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import ModelUsage

from jarvis_hub.agents.backend import Context
from jarvis_hub.agents.sdk import turn_end


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
    end = turn_end(result(model_usage=models, usage={"input_tokens": 99_999}), "sonnet", last)
    assert end.context == Context(4_510, 1_000_000)


def test_without_a_per_model_report_there_is_no_context() -> None:
    last = {"input_tokens": 10}
    assert turn_end(result(), "sonnet", last).context is None
    assert turn_end(result(model_usage={"claude-sonnet-5-5": model_usage(200_000, 5)}), "sonnet", None).context is None


def test_the_window_falls_back_to_the_busiest_model_when_the_alias_matches_no_name() -> None:
    models: dict[str, ModelUsage] = {"claude-haiku-5-5": model_usage(200_000, 1_185), "other-big": model_usage(500_000, 9_000)}
    end = turn_end(result(model_usage=models), "sonnet", {"input_tokens": 1})
    assert end.context == Context(1, 500_000)
