# Command recognition evidence

Spec26 #36 remains open. The default Parakeet recognizer is unchanged. The Talker now explicitly asks about uncertain action words, negation, repositories and PR numbers before consequential work; it must not rescue an unclear transcript into a guessed command or Approval. This is a prompt policy change. Its effectiveness with the real Talker still needs evaluation; fake-agent tests cannot establish model compliance or repair recognizer mistakes.

## Compared candidates

The development screen used twelve authored EN/IT commands, with79 reference words after the audited orthographic normalization. The same existing synthesized WAVs were used across candidates. References have not been independently checked against the rendered audio, so the following are script-relative retention metrics, not acoustic ground truth or resulting-action accuracy.

Parakeet greedy and beam3 each retained all critical terms on11/12 cases and literal targets on12/12, with1/79 edits. Whisper large-v3-turbo, with or without fixed repository vocabulary, retained11/12 critical sets and12/12 targets with2/79 edits. Apple SpeechTranscriber.transcription also retained11/12 critical sets and12/12 targets with2/79 edits. Neither a larger beam nor these alternatives demonstrated a command-level development gain. Italian CI becoming C remained unresolved.

A separate DictationTranscriber.shortDictation paired screen used an empty context and29 generic repository/tool terms. Each arm retained6/12 critical sets and10/12 targets, with15/79 edits and no declared-negation loss. One raw transcript differed; no scored gain followed. The collector initially discarded nonfinal-at-emission spans and produced invalid empty outputs. The corrected result used preserved spans after successful file finalization and stream closure, without repeating inference. Both the invalid output and correction remain in local evidence.

Normalization applies number rendering, generic PR-plus-number spacing and apostrophe boundaries identically to references and hypotheses. It does not equate CI/C, phonetic guesses, action words or negations. Earlier scoring that treated PR17 and PR17 with a space differently is superseded by this audit. No spelling rescue is applied to runtime transcripts.

## Original-recording check

A focused four-clip reproduction reused the original English/Italian cancellation and technical-command recordings, with one numeric-PR control. Parakeet greedy and beam3 retained1/4 scripted critical sets, without a beam repair of the motivating cancellation cases. Apple SpeechTranscriber, run once with the known sample language, retained2/4: both cancellation scripts improved, the technical case remained incorrect and the numeric-PR control regressed. It had3/13 script-relative edits versus Parakeet4/13. Original scripts still lack independent acoustic adjudication. No personal transcript or audio is published here.

Apple file recognition/finalization on those four clips took median152.6ms, range82.8–206.6ms. This excludes ordinary Hub capture, endpoint, language selection and admission. Its native-client peak RSS excludes Apple's model service. No stack resource or meaningful-audio latency target follows. The mixed result and unresolved automatic language selection prevent selecting it as the runtime command recognizer.

## Remaining checks

1. Independently check reference wording against the original recordings and synthetic development audio without changing references to match hypotheses. Preserve disagreements and adjudication history locally.
2. Freeze a candidate and command-level scorer before opening the fresh24-case EN/IT heldout set. That set remains unused; the development screen and four motivating clips are spent evidence.
3. Evaluate action/target preservation and clarification through the real Talker and existing tools, including negation, cancellation, numeric targets, unclear repo names and noise. Never treat a lexical score as a successful Task action.
4. If transcripts change, revalidate contextual-admission cases with the resulting transcripts. Preserve exact Approvals, scope resolution and tool guards.
5. Compare physical end-to-end latency and whole-stack resources only after selecting a supported configuration. Independent spoken references and physical tests await an awake session.

The overnight experiments used saved files only, with no microphone, speaker or cloud recognition. Default fake tests spend no subscription quota. Raw audio, hypotheses, scripts derived from personal recordings and detailed logs remain local. This evidence does not close #36 or Spec26.
