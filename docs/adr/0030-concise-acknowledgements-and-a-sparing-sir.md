# Concise acknowledgements and a sparing "sir"

The User found Jarvis wordy and too free with "sir". ADR 0015 asked for a short line specific to each request before background work, and the Talker came to restate every request. The persona asked for "sir" about once a reply, and the event log from 2026-11-19 to 2026-11-23 shows it in 203 of 208 replies, twice or more in 34.

All 34 come from one Talker turn with a tool call between two text blocks: "Certainly, sir. I'll look into it." before `delegate`, "Under way, sir. I'll report shortly." after it. The Hub joins a turn's blocks into one reply by design, and every reply's deltas add up to its final text, so nothing merges separate replies. The model writes the text after a tool as if it were a new reply, despite the rule to say nothing after `delegate`.

So:

- A simple request gets a few words saying Jarvis is on it, without restating it; a complex one gets a brief restatement. Replies are usually one or two short sentences. This narrows ADR 0015's request-specific line; speech still comes from the model, never a cached line.
- The persona asks for "sir" or "signore" in about one reply in two or three, never twice in one.
- The Hub keeps the first address in a reply and drops any later ", sir" or ", signore", across text blocks. Until the first address it passes text through untouched, so replies without one stream as before. Replaying the logged replies through it leaves one address in all but a reply that quotes the word.

The User decided this on 2026-11-23. The guard covers the repetition the prompt did not stop; how often "sir" appears overall is left to the prompt.
