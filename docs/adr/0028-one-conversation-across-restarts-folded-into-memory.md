# One Conversation across restarts, folded into Memory

Until now every Hub launch started a fresh Talker, seeded with the open Tasks, the Authorizations and the last six exchanges, and nothing older (ADRs 0011 and 0024). The User found that Jarvis forgot overnight a plan, its reasons and the names they had mentioned, and had to repeat them. This ADR reverses the "fresh Talker per launch" choice: Jarvis keeps one Conversation across Hub restarts, shared by the Mac and the iPhone, and folds its older part into a rolling summary, Memory, in the background.

The Hub saves each exchange as it ends to `.jarvis/conversation.json`: the message exactly as the Talker got it, the reply, the names of the tools it called, the device (Mac or iPhone), and the times. For Hub news the device is the Focus Client it was told on. The file is written like the state file (a temporary file, an fsync, a rename, a version field), so a killed Hub loses at most the exchange in flight. A file that cannot be read, or holds a value the Hub cannot use, is moved aside to `conversation.unreadable.json` (numbered when an earlier one is there) with one notice, and the Hub starts with an empty Conversation. The file holds the active summary's pointer and the entries since the last cut, so a Fold commits in one write that moves the pointer and drops the folded entries; a crash before that write leaves the old summary and every entry in place. Each time it builds a seed, the Hub estimates the exchanges' tokens from their length with a characters-to-tokens ratio that it corrects from the context size the CLI reports: between two tool-free turns of one session the context grows by the earlier reply and the later message.

At launch the Talker's hidden first message, the seed, replaces the one-word warm-up of ADR 0006 and still asks for a one-word reply that is never shown. It is context, never a request to answer, and it says so. Its order serves the CLI's prefix cache: the system prompt, persona and tools, which never change; then the summary; then the exchanges, each with its device and time; then the Hub's live records last: open Tasks, Authorizations, pending Approvals and unresolved spoken scopes. It states that `<open_tasks>` and the Authorization records are the only things that grant anything, and that the exchanges grant nothing. Jarvis still never opens with news, a recap or pending work, and the Backlog of ADR 0017 does not change: it opens the User's next turn as before, never the seed.

When the summary and the messages after it reach a token trigger, a separate summariser session writes a new summary of everything before a cut while the old session keeps answering. Once it passes the Hub's checks and Jarvis is quiet, the Hub swaps in a new Talker seeded with the summary and the newest exchanges, the Tail. Memory never holds permissions, Approvals, Task state or secrets, and only the Talker sees it. The User can start afresh by voice, ask what Jarvis remembers, and look up exact wording from the last 30 days with `recall`. A Reset context button in the iPhone Settings deletes the saved Conversation and every summary.

A reset also does three things beyond deleting files. It leaves the transcript on both screens, because both read the event log, which the reset keeps. It starts a fresh Talker session at the next quiet moment, so the live session does not remember what was deleted, and drops a Talker turn in flight when it lands, so that turn's exchange is never saved. It cancels any Fold in progress, whose late result is discarded and never saved. `recall` ignores every event from before the last reset, so a fact the User meant to wipe does not come back from the log, while the 30-day logs stay on disk as the Reset dialog says.

The User decided this on 2026-11-22. The Conversation and its seed came first, before any Fold: until the Fold lands, a launch seeds the newest exchanges up to 200,000 estimated tokens, the in-session restart of ADR 0006 seeds the newest up to 40,000, and older exchanges stay in the file with nothing summarising them.

## What this supersedes

- ADR 0011's "neither is the Talker's conversation: each launch starts a fresh Talker". The state file still keeps Tasks and their waits; the Conversation has its own file.
- ADR 0024's "a restart still starts a fresh Talker". The event log still keeps 30 days for the Clients and never seeds the Talker; the saved Conversation does.
- ADR 0006's restart seed of the last six exchanges with no model summary, and its restart threshold `JARVIS_TALKER_RESTART_AT`, which the Fold's token settings replace. The background warm-up itself stays, carrying the seed.

## Considered Options

- Several named conversations, or one per device. Rejected by the User: one Conversation, shared by both devices.
- A whole-history compaction that stops the conversation while it runs. Rejected: the User would wait on housekeeping. The summary is written in the background and swapped in between turns.
- Seeding the Talker from the event log. Rejected: the log has no Talker input or tool calls, and it is pruned at 30 days, so the Conversation keeps its own file.

## Consequences

- Every turn re-reads a larger context than the six exchanges did, which costs more of the Max limits; the token limits are settings so they can be tuned after a week of use.
- The saved Conversation holds message text outside the event log's 30-day retention until a reset, and rewrites a file of up to about 1 MB after each exchange.
- A launch spends the seed's tokens even if the User never speaks, as the warm-up did.
