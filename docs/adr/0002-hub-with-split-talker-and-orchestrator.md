# A Python Hub with a split Talker and Orchestrator

Jarvis used to be one Claude Code session that talked through VoiceMode's `converse` tool and did the work as well. Every reply waited for a whole tool call before any audio played. Look-ups added model round trips before the first word. The session could not sit idle or be interrupted. We replaced it with a Hub: an always-running Python process that owns the microphone, the speaker, the Wake word, end-of-turn detection, speech-to-text, text-to-speech and Barge-in, and runs three kinds of Claude session through the Claude Agent SDK. A Talker on a fast model (Haiku by default) holds the conversation and is the only voice. An Orchestrator on a stronger model (Sonnet at medium effort by default) plans Tasks and judges results. Subagents do the work, each with a model and effort chosen for its task.

## Considered Options

- Keep `converse` as the conversation loop. Rejected: a tool call cannot stream audio sentence by sentence, cannot pause mid-sentence and cannot idle.
- One model for talking and orchestrating. Rejected: a model strong enough to judge Subagent results across repos is too slow to answer within a second.
- Let Claude Code manage Subagents as background subagents. Rejected: in headless mode their completion notifications are unreliable, so the Hub runs each Subagent as its own SDK session and owns its completion event, concurrency cap and cancellation.

## Consequences

- Every model call goes through the installed `claude` CLI under the User's Max login, and `ANTHROPIC_API_KEY` must not be set in the Hub's environment, or calls are billed to the API. If Anthropic stops letting the Agent SDK draw on subscription limits, every model call becomes an API cost and this design needs another look.
- The Talker knows nothing about the Org beyond what Announcements tell it, so every question about the repos becomes a Task.
- `AGENTS.md` stops being Jarvis's runtime prompt. The prompts move into the Hub, and opening a coding agent in this repo no longer turns it into Jarvis.
- Only Subagents skip permissions. The Orchestrator coordinates with read-only tools, which narrows ADR 0001.
