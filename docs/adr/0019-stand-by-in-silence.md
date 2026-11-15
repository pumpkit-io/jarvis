# Stand by in silence when the User asks for quiet

The User wants to tell Jarvis to stop or shut up in their own words and have it go quiet: no spoken acknowledgment, just the closing sound that says only the Wake word reaches it now. Stop phrases matched only a fixed list ("that's all", "stand by", "basta così"), so "shut up" or "be quiet, please" went to the Talker as a request and got a spoken reply.

The Talker gets a `stand_by` tool. When it judges the User wants silence, in any words or language, it calls the tool as its whole reply. The Hub drops any text the turn produces after that, on screen and in speech, and in a spoken User turn it tells voice mode, which stops whatever is playing, interrupts the turn, goes idle and plays the closing sound. Recognizing the request is part of the Talker turn every input already gets, so ordinary requests pay nothing extra; a request for silence costs one Talker turn before the sound.

The fixed stop phrases stay as the path with no model call, and now play the closing sound too. `end_conversation`, where Jarvis says a farewell, still idles without it. A Barge-in while Jarvis speaks still goes through the judge of ADR 0014, since "stop" over a reply often means "wait" rather than "be quiet". News is not held unless the Talker also turns Quiet mode on.

The User decided this on 2026-11-16.

## Considered Options

- Grow the stop-phrase list. Rejected: the User wants any wording to work.
- A separate classifier on every spoken turn. Rejected: it would add latency to every request, the User's top priority.
- Hold back the reply's text until the Hub knows whether the turn stands by. Rejected: it would delay every reply's first sentence; the Hub cuts off speech when the tool call arrives instead.

## Consequences

- If the Talker speaks before calling `stand_by`, the User may hear the start of it before it is cut off.
- The Talker prompt makes `stand_by` the one exception to speaking before a tool call.
