# Confirm interruption intent before pausing

Jarvis continues speaking while it assesses overlapping speech and pauses or stops only when there is strong evidence of an Interruption directed to it. Uncertain speech, Backchannels and conversations between people leave playback uninterrupted. Anyone may clearly address Jarvis without voice enrollment; identifying a speaker and determining whom they address are separate concerns.

The previous pause-first policy disrupted playback after detected speech before understanding it. Phrase lists, duration and word counts could then cancel the reply without establishing that the speaker addressed Jarvis. Improving only that later decision would preserve the unwanted pauses. We accept some extra delay in recognizing genuine interruptions to avoid disrupting replies for unrelated speech. This applies to replies and Announcements.

Jarvis also favors preserving a complete thought over replying at the first possible pause. The existing end-of-speech-to-first-audio targets, p50 at most 1500 ms and p90 at most 2500 ms, remain goals for clearly completed turns, measured using request-specific speech rather than cached holding lines. Premature turn endings must be measured separately so lower apparent latency cannot conceal fragmented requests.

Wake phrases during playback also require context, because someone may quote or discuss them. A clearly addressed request such as "Jarvis, stop" should take the fastest reliable path. Context can also establish an Interruption without a Wake word; requiring one for every correction would prevent natural conversation. Idle Wake word detection remains separate.

Evaluate local recognition first. A cloud assessment would need evidence of a meaningful accuracy and latency benefit, and a decision about sending candidate background speech outside the machine. This decision does not authorize a new paid service or cloud processing of unrelated conversations. Model and engine selection remain evidence-led implementation choices.

The User accepted these behavior choices on 2026-11-05. They replace the glossary's previous unconditional wake interruption and pause-first Barge-in definitions. ADR 0002's internal role separation and ADR 0010's quiet-time Announcement queue remain; the new interruption policy also applies once an Announcement plays. These decisions describe intended behavior, not a claim that the runtime already implements it.
