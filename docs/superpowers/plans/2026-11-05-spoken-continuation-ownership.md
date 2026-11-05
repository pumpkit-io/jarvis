# Spoken continuation ownership (#38)

The accepted #26 behavior and reordered #38 ticket authorize this implementation. Preserve the Hub's authority, runtime Boundary, quiet-time news and typed input. Do not admit a thinking-phase microphone candidate through a conversational/cloud call merely to determine whom it addresses.

1. Extend the existing bounded local assessment contract with explicit continuation/correction/new_request/unrelated/uncertain outcomes and optional frozen logical phase/onset context. Keep the old directedness experiment compatible and uncertain fail-closed. Test the existing worker seam with fake recognizers.
2. Stamp captured turns with a capture epoch, candidate sequence and onset. Preserve a turn begun while thinking across logical reply startup; invalidate it on wake/replacement/shutdown. Keep existing input/Hub seams and explicit retention limits. Test late input and first-audio races through injected frames.
3. Keep an admitted spoken request's identity and revision separate from speech-end timing. Local relation decisions determine whether ordered speech extends it, corrects it or starts a new request. Unrelated/uncertain speech cannot stop generation or reach the Talker. Script those decisions in the existing Hub/VoiceMode fixture; production uses only a separately validated local recognizer.
4. Stop and drain the obsolete spoken generation/audio before posting its accepted revision. Guard obsolete tool calls and reuse a request's Task identity when an already-started Task is amended, rather than duplicating work. Preserve wake/typed/news boundaries and reject stale decisions after ownership changes.
5. Cover EN/IT, queued candidates, bounded input, delayed transcription and shutdown; update diagnostics and ownership guidance. Run full pytest/Pyright and targeted adapter/Hub tests before reporting implementation complete. Physical and real conversational-model checks remain unrun.

The semantic exploration runs separately on frozen local data. A passing mechanical fake-adapter suite does not establish recognition accuracy or physical voice acceptance.
