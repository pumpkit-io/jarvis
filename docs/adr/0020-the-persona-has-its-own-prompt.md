# The persona has its own prompt

Jarvis's character was one paragraph of `prompts/talker.md`, beside the rules for tools, events and Approvals, and it described Jarvis as J.A.R.V.I.S. to the User's Tony Stark. The User wants that character in full, without naming the films, and kept apart so that edits to the Talker's working rules never change it.

The character now lives only in `prompts/persona.md`, which the Talker's system prompt appends after `talker.md`. It covers who Jarvis is (an English butler with a chief of staff's competence), the voice, the use of "sir", the dry humour and when to turn it off, conduct (warn once, then comply), a phrasebook and a few sample lines. `talker.md` keeps only how the Talker works. In English, Jarvis now uses British spelling and idiom. The voice stays `am_liam`, because the User asked for British wording without a British voice.

The User decided this on 2026-11-16. It refines the persona of ADR 0018; the Orchestrator and Subagents are unchanged, since only the Talker speaks.

## Consequences

- The Talker's system prompt grows from about 800 to about 1,900 tokens. It is cached after the warm-up (ADR 0006), so the cost falls mostly on that hidden first turn.
- A test keeps character out of `talker.md` and the films' names out of both files.
