# Speech stays on the Mac; the phone sends and receives audio

The iPhone Client could recognise speech on the device and speak with Apple's voices. We keep speech recognition and synthesis on the Mac only. The phone records a Talk and uploads it, the Hub transcribes it with the Parakeet model it already has loaded, and each sentence Kokoro synthesizes goes down to the phone as an audio clip to play. This keeps one voice and one persona (ADR 0015), one copy of the speech logic, and one loaded instance of each model on an 18 GB Mac.

Audio travels over HTTP uploads and one WebSocket, as uncompressed PCM. A Talk is half-duplex: an utterance goes up, then sentence clips come down, so there is no WebRTC or LiveKit transport. We revisit compression, streamed uploads or a real-time transport only if measured latency over cellular misses the target.

Each Talk is a new Spoken request, with none of the Mac's resumed-speech revisions. A Talk during a reply is always an Interruption and skips the judge of ADR 0014, since a button press cannot be a Backchannel. The wake word, voice activity detection, Smart Turn and echo cancellation play no part in a Talk.

The User decided this on 2026-11-18.

## Considered Options

- On-device recognition and Apple's built-in voices. Rejected: the phone would get a different voice, different transcripts and a second copy of the speech logic, in exchange for less bandwidth.
- WebRTC or LiveKit from the start. Rejected: nothing in push-to-talk needs a real-time duplex stream, and the Hub already produces speech sentence by sentence.
