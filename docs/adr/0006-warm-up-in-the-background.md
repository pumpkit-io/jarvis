# The Talker warms up in the background at launch

Spec story 1 asks Jarvis to start without any model call. The Hub makes one: at launch it opens the Talker session and sends a short `<event source="hub">` message whose reply is never shown, so the User's first turn finds the Talker's prompt in the cache. The warm-up runs in the background. The prompt appears at once, and a turn typed before the warm-up ends waits for it rather than going first, because one session takes one turn at a time and dropping a warm-up already sent would need an interrupt.

Measured on 2026-11-03 (`docs/notes/talker-in-the-hub.json`). With a cold cache, the first turn's first delta took 4.24 s and 1.16 s without a warm-up, and 0.73 s and 0.84 s after one. Opening the session and warming it took 1.70 s and 1.75 s in two launches. A line typed at launch waits that long: its first sentence came 2.72 s and 2.79 s after the line arrived, against 1.03 s and 1.05 s for a line typed 4 s after launch.

## Consequences

- Each launch spends one model call of the Max limits, about 1.3k input tokens with a one-word reply, even if the User never speaks. No model call follows while the Hub is idle.
- Story 1 now holds as "ready at once, without waiting on a model call".
