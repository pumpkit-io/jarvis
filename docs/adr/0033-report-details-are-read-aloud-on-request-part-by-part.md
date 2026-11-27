# Report details are read aloud on request, part by part

On 2026-11-27 the User, unable to look at the screen, asked Jarvis to read out a breakdown of a marketing plan. The Orchestrator reported it four times, with 2,000 to 7,500 characters of details each, and the Talker never heard them. Its event carried only the one-sentence `spoken` and `details="on screen"`, which its prompt read as "already visible". `recall` returned 500-character excerpts. So it read out a fragment and promised the rest.

Putting the details in the event would fix that turn and cost every other one: most reports are glanced at, never heard, and every event stays in the Talker's Conversation until the next Fold. So:

- A Report with details gets an id, such as R12: its seq in the event log, which already keeps the details 30 days for `recall`. Task ids start again at T1 each launch; seqs never repeat.
- The Talker's event gains `report="R12" parts="3"`, and still says one sentence. Status and `recall` matches name the same id.
- The Talker's `read_report` tool returns one part at a time: about 1,500 characters, two minutes of speech, cut at the coarsest break that fits (paragraph, line, sentence, word), with each heading kept with what follows it. The Talker reads a part when the User wants to hear the details or cannot look, then offers the next. Only the parts asked for enter the Conversation.
- What the Talker reads has secrets removed, as `recall` does (ADR 0028); the screen shows the details as reported.
- The Orchestrator's `report` takes `details_file`, an absolute path to a text file of at most 200,000 characters, attached whole after `details_markdown`. A long file then reaches the screen and the Talker without the Orchestrator copying it out through its own output.

A Hub without an event log, as some tests run, shows details and has nothing for `read_report` to read. The Talker gets no file access of its own: files reach it through a Task, as everything in the Org does.
