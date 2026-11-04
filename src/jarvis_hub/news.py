"""The news the Hub tells the User: each piece as posted, and what one Talker turn tells of it.

A `Leaf` is one piece of news: one event for the Talker, one line on screen, one of the "N updates". A `News` is
what one turn tells, in order: leaves, and "while you were away" openers holding the leaves Quiet mode held. Every
rule that tells leaves apart (the reset, what gets through Quiet mode, the count) lives here, and so does the one
saved form, so a restart brings back exactly what was waiting.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from jarvis_hub.state import Fields, TimeShift


def away_message(count: int) -> str:
    updates = "update" if count == 1 else "updates"
    text = f"{count} {updates} while you were away; their details are on screen."
    return f'<event source="hub" away="{count}">{text}</event>'


@dataclass(frozen=True)
class Leaf:
    """One piece of news as posted."""

    message: str
    arrived: float
    # Its line in the "while you were away" list.
    summary: str
    details: str | None = None
    # It needs the User and differs from the last such news on its Task, so it gets through Quiet mode.
    urgent: bool = False
    # The news that the usage limit reset, which a new hit makes untrue before it is told.
    reset: bool = False

    def dump(self, to_wall: TimeShift) -> dict[str, Any]:
        return _LEAF.dump(self, to_wall)

    @staticmethod
    def load(saved: dict[str, Any], from_wall: TimeShift) -> "Leaf":
        """KeyError, TypeError or ValueError when `saved` is not a whole leaf."""
        return Leaf(**_LEAF.load(saved, from_wall))


_LEAF = Fields(("message", "arrived", "summary", "details", "urgent", "reset"), times=frozenset({"arrived"}))


def fresh(leaves: Iterable[Leaf]) -> list[Leaf]:
    """`leaves` less the news that the limit reset."""
    return [leaf for leaf in leaves if not leaf.reset]


@dataclass(frozen=True)
class _Away:
    """The "while you were away" opener: the exact count, then each held leaf."""

    leaves: tuple[Leaf, ...]
    arrived: float

    @property
    def message(self) -> str:
        return "\n".join([away_message(len(self.leaves)), *(leaf.message for leaf in self.leaves)])

    @property
    def details(self) -> str:
        listed = "\n".join(f"- {leaf.summary}" for leaf in self.leaves)
        held = (leaf.details for leaf in self.leaves if leaf.details)
        return "\n\n".join([f"**While you were away**\n\n{listed}", *held])


_Group = Leaf | _Away


@dataclass(frozen=True)
class News:
    """What one Talker turn tells, in order; never empty."""

    groups: tuple[_Group, ...]

    @staticmethod
    def one(leaf: Leaf) -> "News":
        return News((leaf,))

    @staticmethod
    def of(leaves: Iterable[Leaf]) -> "News | None":
        """`leaves` as one turn of ordinary news; None when there are none."""
        groups = tuple(leaves)
        return News(groups) if groups else None

    @staticmethod
    def away(held: Iterable[Leaf], arrived: float) -> "News":
        """The opener for what Quiet mode held, arriving now: `held` must not be empty."""
        leaves = tuple(held)
        if not leaves:
            raise ValueError("an away opener holds at least one leaf")
        return News((_Away(leaves, arrived),))

    @staticmethod
    def joined(items: Iterable["News"]) -> "News":
        """`items` as one turn, in order."""
        return News(tuple(group for item in items for group in item.groups))

    @property
    def arrived(self) -> float:
        return self.groups[0].arrived

    @property
    def message(self) -> str:
        return "\n".join(group.message for group in self.groups)

    @property
    def details(self) -> str | None:
        return "\n\n".join(group.details for group in self.groups if group.details) or None

    @property
    def count(self) -> int:
        """How many pieces of news this turn tells: each is one posted item, and one of the "N updates"."""
        return len(self.leaves())

    @property
    def is_away(self) -> bool:
        """Whether this is the away opener alone, which the User's next spoken turn opens with."""
        return len(self.groups) == 1 and isinstance(self.groups[0], _Away)

    def leaves(self) -> tuple[Leaf, ...]:
        return tuple(leaf for group in self.groups for leaf in _leaves(group))

    def away_leaves(self) -> tuple[Leaf, ...]:
        """The leaves Quiet mode held, which the state file keeps until the User has heard them."""
        return tuple(leaf for group in self.groups if isinstance(group, _Away) for leaf in group.leaves)

    def without_reset(self) -> "News | None":
        """This turn less the news that the limit reset, which is told once or never; None when nothing is left.
        An away opener that loses some is recounted."""
        groups: list[_Group] = []
        for group in self.groups:
            if isinstance(group, _Away):
                if kept := fresh(group.leaves):
                    groups.append(_Away(tuple(kept), group.arrived))
            elif not group.reset:
                groups.append(group)
        return News(tuple(groups)) if groups else None

    def split(self) -> "tuple[News | None, list[Leaf]]":
        """What gets through Quiet mode, as one turn, and every other leaf for it to hold. An away opener is held
        whole: it is news Quiet mode held already."""
        through: list[_Group] = []
        held: list[Leaf] = []
        for group in self.groups:
            if isinstance(group, Leaf) and group.urgent:
                through.append(group)
            else:
                held += _leaves(group)
        return (News(tuple(through)) if through else None), held

    def dump(self, to_wall: TimeShift) -> list[dict[str, Any]]:
        """The whole turn, for the state file."""
        return [
            {"away": [leaf.dump(to_wall) for leaf in group.leaves], "arrived": to_wall(group.arrived)}
            if isinstance(group, _Away)
            else group.dump(to_wall)
            for group in self.groups
        ]

    @staticmethod
    def load(saved: object, from_wall: TimeShift) -> "News":
        """KeyError, TypeError or ValueError when `saved` is not a whole turn."""
        if not isinstance(saved, list) or not saved:
            raise ValueError(f"{saved!r} is not a turn of news")
        groups: list[_Group] = []
        for record in cast(list[dict[str, Any]], saved):
            if "away" in record:
                held = [Leaf.load(leaf, from_wall) for leaf in record["away"]]
                groups += News.away(held, from_wall(float(record["arrived"]))).groups
            else:
                groups.append(Leaf.load(record, from_wall))
        return News(tuple(groups))


def _leaves(group: _Group) -> tuple[Leaf, ...]:
    return group.leaves if isinstance(group, _Away) else (group,)
