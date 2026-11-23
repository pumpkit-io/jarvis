# Live mode streams the iPhone's microphone to the Mac's voice pipeline

The User wants to talk to Jarvis on the iPhone the way they do on the Mac: say the Wake word, speak, hear the reply, go on during the Follow-up window, and have Jarvis go idle after a silence. Everything that makes the Mac behave that way lives in the Hub: the Wake word models, voice activity and end-of-turn detection, the Barge-in judge, the voice state machine and its timeouts. The iPhone Client only records a Talk.

In Live mode the iPhone streams its microphone to the Hub over the WebSocket it already holds, as uncompressed 16 kHz mono PCM, after iOS's own echo cancellation. The Hub runs the same input pipeline on that stream as on the Mac's microphone, with no echo cancellation of its own, and both streams share the one loaded transcription model. One voice floor serves every listening Client. The Client whose Wake word opened an exchange gets its capture, its chime, its replies and its closing sound; the others listen only for the Wake word until the exchange ends. The Follow-up window and listening timeouts are the Hub's existing settings, so the phone has none of its own.

This changes ADR 0025 in one respect: while Live mode is on, audio goes up continuously instead of one Talk at a time. Recognition and synthesis stay on the Mac, a Talk still skips the Wake word, voice activity detection, Smart Turn and echo cancellation, and the floor of ADR 0024 stays the one conversation's.

The User decided this on 2026-11-23.

## Considered Options

- Wake word detection and the voice state machine on the phone, sending only the speech that follows. Rejected: it is a second copy of the speech logic that ADR 0025 kept on the Mac, and the two would drift in timing and behaviour.
- One voice floor per Client. Rejected: two exchanges could run at once and both reach the one Talker, which ADR 0024 rules out.

## Consequences

- Live mode works only while the Mac's Hub is reachable. While it is not, the phone drops its audio rather than queueing it, and listening resumes on reconnect.
- Uncompressed audio costs about 32 KB a second, roughly 115 MB an hour of Live mode on cellular. Compressing it is later work.
- Echo handling on the phone rests on iOS's echo cancellation, which has not yet been measured with the Barge-in judge.
- Keeping the WebSocket open with the screen locked uses the app's audio background mode, costs battery and shows the orange microphone indicator.
