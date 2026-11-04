"""What one turn of news is made of: its count, the news the usage limit reset taken out of it, what gets through
Quiet mode, and the whole of it kept across a restart."""

import pytest

from jarvis_hub.news import Leaf, News, fresh

R = Leaf("<reset/>", 1.0, "reset", reset=True)
E = Leaf("<event/>", 2.0, "event", details="event details")
X = Leaf("<other/>", 3.0, "other")
U = Leaf("<urgent/>", 4.0, "urgent", urgent=True)


def one(*leaves: Leaf) -> News:
    return News.joined([News.one(leaf) for leaf in leaves])


def away(*leaves: Leaf) -> News:
    return News.away(list(leaves), 0.5)


def lines(news: News | None) -> list[str] | None:
    """The summaries of the news still in `news`, in order; None when nothing is left."""
    return None if news is None else [leaf.summary for leaf in news.leaves()]


def test_a_turn_of_several_items_tells_each_in_order_and_counts_each() -> None:
    turn = one(R, E)

    assert turn.count == 2
    assert turn.message == "<reset/>\n<event/>"
    assert turn.details == "event details"
    assert turn.arrived == 1.0
    assert not turn.is_away


def test_the_away_opener_says_the_exact_count_and_lists_each_held_item_on_screen() -> None:
    opener = away(E, X)

    assert opener.is_away
    assert opener.count == 2
    assert opener.message.splitlines() == [
        '<event source="hub" away="2">2 updates while you were away; their details are on screen.</event>',
        "<event/>",
        "<other/>",
    ]
    assert opener.details == "**While you were away**\n\n- event\n- other\n\nevent details"
    assert opener.away_leaves() == (E, X)


def test_one_held_item_is_one_update() -> None:
    assert away(X).message.startswith('<event source="hub" away="1">1 update while you were away')


def test_a_turn_that_opens_with_the_away_item_is_not_the_away_item_and_keeps_its_held_news_apart() -> None:
    turn = News.joined([away(R, X), News.one(E)])

    assert not turn.is_away
    assert turn.count == 3
    assert lines(turn) == ["reset", "other", "event"]
    assert turn.away_leaves() == (R, X)
    assert turn.message.splitlines()[0].startswith('<event source="hub" away="2">')


def test_joining_one_item_is_that_item_and_nested_turns_flatten() -> None:
    assert News.joined([away(X)]) == away(X)
    assert News.joined([one(R, E), News.one(X)]) == one(R, E, X)


@pytest.mark.parametrize(
    ("news", "expected"),
    [
        pytest.param(News.one(R), None, id="reset alone"),
        pytest.param(News.one(E), ["event"], id="event alone"),
        pytest.param(one(R, E), ["event"], id="reset then event"),
        pytest.param(one(E, R), ["event"], id="event then reset"),
        pytest.param(one(R, R), None, id="only resets"),
        pytest.param(away(R, X), ["other"], id="away of reset and other"),
        pytest.param(away(R), None, id="away of only a reset"),
        pytest.param(away(X), ["other"], id="away without a reset"),
        pytest.param(News.joined([away(R, X), News.one(E)]), ["other", "event"], id="away with a reset, then event"),
        pytest.param(News.joined([away(X), one(R, E)]), ["other", "event"], id="away, reset, event"),
        pytest.param(News.joined([News.one(E), away(X, R)]), ["event", "other"], id="event, then away with a reset"),
        pytest.param(News.joined([one(R, E), News.one(X)]), ["event", "other"], id="nested, reset inside"),
        pytest.param(News.joined([News.joined([News.one(R), away(R, X)]), News.one(E)]), ["other", "event"],
                     id="nested with an away"),
    ],
)
def test_the_news_the_limit_reset_is_taken_out_and_the_rest_is_kept_and_counted(
    news: News, expected: list[str] | None
) -> None:
    kept = news.without_reset()

    assert lines(kept) == expected
    assert (kept.count if kept else 0) == len(expected or [])


def test_an_away_opener_less_its_reset_recounts_what_it_holds() -> None:
    kept = away(R, E, X).without_reset()

    assert kept == away(E, X)
    assert kept is not None and kept.message.startswith('<event source="hub" away="2">')


def test_fresh_leaves_out_only_the_reset() -> None:
    assert fresh([R, E, R, X]) == [E, X]


@pytest.mark.parametrize(
    ("news", "through", "held"),
    [
        pytest.param(one(U, E), ["urgent"], ["event"], id="needs the user, then ordinary"),
        pytest.param(one(E, U, X), ["urgent"], ["event", "other"], id="ordinary around news that needs the user"),
        pytest.param(one(E, X), None, ["event", "other"], id="all ordinary"),
        pytest.param(one(U, U), ["urgent", "urgent"], [], id="all needs the user"),
        pytest.param(News.joined([away(E, X), News.one(U)]), ["urgent"], ["event", "other"], id="away, then urgent"),
        pytest.param(away(E), None, ["event"], id="away alone"),
    ],
)
def test_quiet_mode_lets_through_only_the_news_that_needs_the_user_and_holds_each_other_item(
    news: News, through: list[str] | None, held: list[str]
) -> None:
    kept, rest = news.split()

    assert lines(kept) == through
    assert [leaf.summary for leaf in rest] == held


def shift(t: float) -> float:
    return t + 100.0


def unshift(t: float) -> float:
    return t - 100.0


@pytest.mark.parametrize(
    "news",
    [
        pytest.param(News.one(E), id="one item"),
        pytest.param(one(U, E, R), id="a turn of several, urgent and reset kept"),
        pytest.param(away(E, X), id="the away opener"),
        pytest.param(News.joined([away(E, X), News.one(U)]), id="a turn opening with the away opener"),
    ],
)
def test_the_whole_turn_survives_a_restart(news: News) -> None:
    saved = news.dump(shift)

    assert News.load(saved, unshift) == news
    assert News.load(News.load(saved, unshift).dump(shift), unshift).count == news.count


def test_a_leaf_survives_a_restart_with_its_times_on_the_wall_clock() -> None:
    saved = U.dump(shift)

    assert saved["arrived"] == 104.0
    assert Leaf.load(saved, unshift) == U


EMPTY_AWAY: list[dict[str, object]] = [{"away": [], "arrived": 1.0}]


@pytest.mark.parametrize(
    "saved",
    [
        pytest.param([], id="nothing"),
        pytest.param(EMPTY_AWAY, id="an empty away opener"),
        pytest.param([{"message": "m"}], id="a leaf missing fields"),
    ],
)
def test_a_saved_turn_that_cannot_be_whole_is_refused(saved: object) -> None:
    with pytest.raises((KeyError, TypeError, ValueError)):
        News.load(saved, unshift)


def test_ordinary_news_of_no_leaves_is_nothing() -> None:
    assert News.of([]) is None
    assert News.of([E, X]) == one(E, X)
