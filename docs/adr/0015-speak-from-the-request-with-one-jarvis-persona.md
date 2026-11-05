# Speak from the request with one Jarvis persona

Jarvis generates conversational speech from the actual request or situation, including greetings, acknowledgments and farewells. Remove timer-injected holding lines and cached conversational sentences. When work belongs in the background, Jarvis first gives a short response specific to what the User asked, then begins that work. Direct answers should begin with useful content rather than an added acknowledgment.

We accept that a slow model response can leave silence rather than conceal it with predefined speech. When the model is unavailable, operational status is shown on screen, with a nonverbal cue if needed. Usage-limit state, reset information and recovery behavior still need to reach that status display. Required Approval wording retains its existing exactness rules; it is a Boundary requirement, not optional conversational filler.

The glossary already defines Jarvis as the whole system. Its spoken responses describe what Jarvis is doing, using its own voice, without narrating handoffs to the Talker, Orchestrator or Subagents. Internal roles and handoffs remain as established in ADR 0002. ADR 0005's speak-before-tools behavior remains for work requests, with request-specific wording in place of generic examples.

The User accepted these choices on 2026-11-05. Removing generated filler and measuring the first meaningful response make latency regressions visible rather than rewarding an unrelated cached sentence. The runtime and prompt changes belong to implementation after this design interview.
