# News reaches the Talker only while the voice is quiet

The Spec's announcement policy says news waits while the User is listened to, thought about or answered. The first version let news reach the Talker at once and held its speech in voice mode: the reply streamed, its audio waited in a list, and the list played once the User's exchange ended. Four review rounds each found a new race in that machinery. Two items of unheard news and a wake lost the first, and a second wake's interrupt landed on held news under way instead of on the User's turn, cutting news the User never heard.

So in voice mode the Hub keeps Orchestrator and Subagent events in a queue of its own, in arrival order, and starts an event turn on the Talker only once Jarvis is idle with nothing playing, the same `quiet` gate the Talker restart waits on. A User line always goes first. Just before the news turn starts, the Hub claims the voice with `VoiceMode.announce()`, which moves an idle machine to `announce` at once, and nothing awaits between the claim and the turn's start. A Wake word processed before the claim makes it fail and the news waits; one processed after it interrupts the news turn. News, once started, is a normal Announcement: a wake cuts it off and it is not said again, which story 30 accepts. Text mode is unchanged: events go to the Talker in arrival order with the User's lines.

## Considered Options

- Keep holding the speech and fix each race. Rejected: every fix added a state (held lists, re-holding news already playing, a slot for the news under way) and the next review found the next race.
- Also let news start in the Follow-up window with nothing playing. Rejected: the User may speak there without the Wake word, and a news turn begun there would talk over a follow-up said in the second before its first audio. News waits out the window instead, 6 s at most.

## Consequences

- News comes a little later: after the User's exchange and its Follow-up window, and after each earlier Announcement's own window (4 s).
- A Talker restart due at the same quiet moment runs first, and the news goes to the new session.
- The speech no longer holds anything. It still drops, leaving it on screen, the answer to a turn heard before the latest Wake word, and `end_conversation` still acts only on the reply to the User's own spoken turn.
- Ticket #15 builds the full Announcement queue and Quiet mode on this queue.
