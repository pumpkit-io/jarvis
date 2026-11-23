"""Keeps the User's form of address, "sir" or "signore", to one per Talker reply."""

import re

# ", sir." or "Sir, …"; a title such as "Sir Walter" is not an address. The text's end stands for a block's end.
_ADDRESS = re.compile(r",\s*(?:sir|signore)(?=[.,;:!?…]|$)|\b(?:sir|signore)(?=,)", re.IGNORECASE)
_VOCATIVE = re.compile(r",\s*(?:sir|signore)(?=[.,;:!?…]|$)", re.IGNORECASE)
# The tail of a delta that later deltas may still turn into an address.
_OPEN = re.compile(r"(?:,\s*|\b)s(?:i(?:r|g(?:n(?:o(?:r(?:e)?)?)?)?)?)?$|,\s*$", re.IGNORECASE)


class AddressGuard:
    """One per reply. The Talker writes text before and after a tool call as if they were two replies, each with its
    own "sir"; the guard keeps the first and drops every later ", sir", whichever block it is in. Until the first
    address it passes deltas through as they come."""

    def __init__(self) -> None:
        self._said = ""
        self._held = ""
        self._used = False

    def feed(self, text: str) -> str:
        """Returns the settled text, holding back a tail that may still become a second address."""
        self._held += text
        return self._release(final=False)

    def end(self) -> str:
        """Returns the held text at a block's end."""
        return self._release(final=True)

    def _release(self, *, final: bool) -> str:
        out = ""
        if not self._used:
            whole = self._said + self._held
            # A ", sir" at the text's end counts only at a block's end: later deltas may turn it into "sirloin".
            first = next(
                (m for m in _ADDRESS.finditer(whole) if m.end() >= len(self._said) and (final or m.end() < len(whole))),
                None,
            )
            if first is None:
                out, self._held = self._held, ""
                self._said += out
                return out
            self._used, self._said = True, ""
            taken = first.end() - (len(whole) - len(self._held))
            out, self._held = self._held[:taken], self._held[taken:]
        tail = None if final else _OPEN.search(self._held)
        cut = tail.start() if tail else len(self._held)
        ready, self._held = self._held[:cut], self._held[cut:]
        return out + _VOCATIVE.sub("", ready)
