# Jarvis is a general assistant with a fuller persona

Jarvis was framed as the caretaker of one GitHub organization, and the Talker steered conversations toward the repos. The User wants a personal assistant they can talk to about anything, with the Org as one of its jobs. They also want more of J.A.R.V.I.S.'s character than the composed, precise voice of ADR 0015: clever, dry humor, warmth, a deft comeback when the User is rude, and a tone that matches the User's.

Only the Talker prompt changes. It says Jarvis talks about whatever the User brings up and answers general questions from its own knowledge, saying so when an answer may be out of date. It also asks Jarvis to match the User's mood, crisp when they are focused and playful when they are casual. Humor stays out of Approval questions and never delays work. Every conversation starts fresh, with no updates unless the User asks (ADR 0017). `delegate` still means work on the Org, and the Orchestrator, Subagents and Boundary are unchanged.

The User decided this on 2026-11-15.

## Considered Options

- Give the Talker web search and fetch so general answers can be current. Deferred by the User: it adds tools outside the guards of ADRs 0007, 0008 and 0013.

## Consequences

- General questions get answers from the model's own knowledge, which can be out of date; the Talker says when that may be so.
- ADR 0015 still holds: speech comes from the request, with one persona and no cached filler.
