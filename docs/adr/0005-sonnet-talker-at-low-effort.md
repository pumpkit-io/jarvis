# Sonnet at low effort as the default Talker

ADR 0002 and the Spec gave the Talker Haiku by default. The Phase 0 latency spike (`docs/notes/phase-0-spikes.md`, `## Agent SDK`) measured both on the same 20 utterances, each at `effort="low"` with thinking disabled. Neither met the 1.0 s p50 target for the first sentence: Haiku 1.21 s, Sonnet 1.06 s. Haiku was faster on small talk alone, but it called a tool before saying anything on 9 of 10 work requests, breaking the speak-first rule, so the User would have waited about 1.75 s (p50) for "Right away, sir." Sonnet spoke first on every delegation. So the Talker defaults to Sonnet at `effort="low"` with thinking disabled. The User accepted this.

## Consequences

- Each Talker turn draws more of the Max limits than it would on Haiku.
- `JARVIS_TALKER_MODEL` still picks the model, and the Talker always runs at `effort="low"` with thinking disabled, which Haiku 5.5 needs to stop thinking at all.
- If a firmer speak-first prompt makes Haiku speak before its tool calls (rerun the spike once the Talker has `delegate`), Haiku can win back the default. Rerun on 2026-11-03 with `delegate` and the firmer prompt: Haiku spoke first on 9 and 15 of 17 tool-using turns, Sonnet on 17 of 17 twice, so Sonnet stays (`docs/notes/phase-0-spikes.md`, `## Tasks and the Orchestrator`).
