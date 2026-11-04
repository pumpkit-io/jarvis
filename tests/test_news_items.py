"""What a merged turn of news is made of, and how the news the usage limit reset is taken out of it."""

# pyright: reportPrivateUsage=false

import pytest

from jarvis_hub.hub import Hub, _leaves, _merged, _TalkerInput


def news(text: str, *, reset: bool = False) -> _TalkerInput:
    return _TalkerInput(f"<{text}/>", "event", 0.0, news=text, reset=reset)


R, E, X = news("reset", reset=True), news("event"), news("other")


def away(*parts: _TalkerInput) -> _TalkerInput:
    return Hub._away_item(list(parts), 1.0)


def told(item: _TalkerInput | None) -> list[str] | None:
    """The news lines still in `item`, in order; None when nothing is left."""
    return None if item is None else [leaf.news or "" for leaf in _leaves(item)]


def test_a_merged_turn_is_none_of_its_items_so_it_carries_neither_flag() -> None:
    merged = _merged([R, E])

    assert (merged.reset, merged.away) == (False, False)
    assert merged.members == (R, E)
    assert merged.count == 2
    assert merged.news == "reset\n- event"


def test_a_merged_turn_with_an_away_item_is_not_away_and_keeps_the_away_parts_for_the_record() -> None:
    merged = _merged([away(R, X), E])

    assert merged.away is False
    assert told(merged) == ["reset", "other", "event"]


def test_merging_one_item_is_that_item() -> None:
    assert _merged([R]) is R


def test_nested_merges_flatten_into_one_list_of_members() -> None:
    assert _merged([_merged([R, E]), X]).members == (R, E, X)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param(R, None, id="reset alone"),
        pytest.param(E, ["event"], id="event alone"),
        pytest.param(_merged([R, E]), ["event"], id="reset then event"),
        pytest.param(_merged([E, R]), ["event"], id="event then reset"),
        pytest.param(_merged([R, R]), None, id="only resets"),
        pytest.param(away(R, X), ["other"], id="away of reset and other"),
        pytest.param(away(R), None, id="away of only a reset"),
        pytest.param(away(X), ["other"], id="away without a reset"),
        pytest.param(_merged([away(R, X), E]), ["other", "event"], id="away with a reset, then event"),
        pytest.param(_merged([away(X), R, E]), ["other", "event"], id="away without a reset, reset, event"),
        pytest.param(_merged([E, away(X, R)]), ["event", "other"], id="event, then away with a reset"),
        pytest.param(_merged([_merged([R, E]), X]), ["event", "other"], id="nested, reset inside"),
        pytest.param(_merged([_merged([E, X]), R]), ["event", "other"], id="nested, reset outside"),
        pytest.param(_merged([_merged([R, away(R, X)]), E]), ["other", "event"], id="nested with an away"),
    ],
)
def test_the_news_the_limit_reset_is_taken_out_of_any_item_and_the_rest_is_kept(
    item: _TalkerInput, expected: list[str] | None
) -> None:
    kept = Hub._without_reset(item)

    assert told(kept) == expected
    assert (kept.count if kept else 0) == len(expected or [])
