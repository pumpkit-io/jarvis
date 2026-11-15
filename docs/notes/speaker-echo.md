# Phantom speech on the built-in speakers

Measured on 2026-11-15 on the User's MacBook Pro, in the room the User reported silent, with the User silent throughout. With the AirPods, the User reported no phantom speech. On the built-in speakers, Jarvis heard words nobody said ("I'm just catching up." over its own "I didn't catch your meaning"), and these stopped its replies.

## Recording

`spikes/hub/speaker_echo.py record` played 82 s of Jarvis replies (Kokoro `am_liam`, ten replies with 2 s gaps) through the MacBook Pro Speakers at 50% system volume. It recorded the MacBook Pro Microphone and every block sent to the speakers, with their times. PortAudio reported 231 ms input latency and 100 ms output latency; sound reached the microphone 0.4 to 0.6 s after the program scheduled it. `analyse` replays the recording offline through `WebRtcEchoCancellation` and the runtime VAD, Wake word, STT and Barge-in detection. The recording stays under the gitignored `.jarvis/echo/`.

## Findings

Echo cancellation works. It removed 34.6 dB of Jarvis's voice using its own delay estimation. Setting `set_stream_delay_ms` never helped: 0 and 100 ms matched the default, while 200 to 500 ms fell to 22 to 26 dB. The runtime leaves it unset.

What reached the pipeline was faint, not loud echo. After cancellation, Jarvis's voice sat at -9 to +20 dB (16-bit RMS, where full scale is about 90 dB). In gaps with nothing playing, the microphone still picked up sound at 15 to 25 dB. Silero VAD called up to 1.6 s of it speech, and Parakeet transcribed it as words, including "I thought it would be funny." in one gap. It also transcribed "Yeah." from the second of silence before anything played.

The old runtime turned that into interruptions. Over the 82 s, the pipeline raised 9 to 12 Barge-ins with words ("That's the", "There you go.", "They can't do it.", "Thank you all.") and 4 to 7 of them stopped Jarvis, because three words or 650 ms of speech took the floor. None contained an interrupt word.

The User's speech is far louder. 117 VAD speech runs from the User's 80 wake takes and 30 request clips, recorded on this Mac in the earlier validation session, measured 56.7 dB at the lowest (p5 58.1, median 60.9). Phantom speech peaked at 27 dB. Today's microphone floor read 6.7 dB, against 22.6 dB then, so the gain may have changed; even 16 dB less would leave the User's quietest utterance near 41 dB.

## Changes

- A loudness gate, `JARVIS_MIN_SPEECH_DB=35`. A VAD chunk counts as speech only when it is at least 35 dB, with 300 ms of hangover for softer syllables inside speech. A Wake word hit counts only with a frame that loud in the last 1.5 s. Every turn's metric row now records `speech_db`, so the gate can be checked on another microphone, such as the AirPods.
- Interrupt-only Barge-ins. Speech over Jarvis stops it only when it holds a phrase from `lexicons/interrupt.txt`. Word counts and speech length no longer take the floor.
- An echo tail guard, `JARVIS_ECHO_TAIL_MS=300`. A Follow-up turn whose speech began before the reply finished playing, or within 300 ms after it, is dropped as the reply's own tail.

## Result

| Replay of the same recording | Barge-ins with words | Barge-ins that stopped Jarvis |
|---|---:|---:|
| Before | 9 | 5 |
| After | 1 | 0 |

The User's recordings through the full pipeline give the same results with and without the gate: Wake word "bare" 20/20, "ehi" 19/20, "hey" 20/20, "hi" 19/20, and 30/30 request clips heard.

The remaining limit: someone else talking loudly near the Mac can still form turns in the Follow-up window, since only loudness and the interrupt list separate them.
