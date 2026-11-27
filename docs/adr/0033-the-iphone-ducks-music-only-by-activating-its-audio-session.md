# The iPhone ducks music only by activating its audio session

ADR 0032 settled that music is ducked under Jarvis, never paused. On a real iPhone 15 Pro (iOS 26.7.3), a Talk's press made music stop for a fraction of a second before it came back ducked, and its release did the same before music returned to full volume. A first fix held a `playAndRecord` session active while the app was in the foreground and switched only its options on a press and a release: `mixWithOthers` in between, `duckOthers` while recording. On the same iPhone that was worse. On a press music fell to near silence and then climbed back to the ducked level; on a release it stopped and stayed stopped until the User resumed it in the music app.

The cause is how iOS ties ducking to activation. Apple documents `duckOthers` this way: "Ducking begins when you activate your app's audio session and ends when you deactivate the session." Changing the options of a session that is already active is not a documented way to start or end a duck; `setCategory` on an active session only "results in an immediate change", and the device showed what that change does to music. The release behaves as if clearing the option interrupted the music app; this is inferred from the device report and Apple's documentation, not traced on the device. An interrupted app resumes only when the session that interrupted it deactivates with `notifyOthersOnDeactivation`, and the held session never deactivated while the app was in the foreground, so music never came back. An app has no other public way to lower another app's audio: system ducking through an activated session is the only one.

`NativeTalkAudioSession` therefore has one lifecycle for every use. A Talk, a reply and Live mode each set their category and options while the session is inactive and then activate it, which starts the duck. When the use ends, after its microphone or speech output has stopped, the session deactivates with `notifyOthersOnDeactivation`, which ends the duck and lets any app the session interrupted resume. Nothing changes the options of an active session, and nothing holds the session between uses. Every option set is mixable: `mixWithOthers` is written out next to `duckOthers` and `interruptSpokenAudioAndMixWithOthers`, which already imply it. While Live mode is on, a Talk and a reply use its active session and make no session call of their own. The options themselves are ADR 0032's.

## Considered Options

- Hold the session active and switch only `duckOthers` on a press and a release (the first fix). Rejected: undocumented, and on the device it ducked through silence and stopped music for good.
- Hold the session active and deactivate it only on a release. Rejected: each release would then deactivate and reactivate a `playAndRecord` session, two hardware changes instead of one.
- Record without ducking, so a press and a release need no session change. Rejected: the User wants music lower while they speak.
- Pin the hardware sample rate and buffer to their current values before activating, in case the short gap is the hardware switching format when the microphone starts. Not done: it is a guess no document supports, and it would mix with this fix in one device check. It is the next experiment if the gap remains.

## Consequences

- Music always keeps playing and returns to its own level by itself after a Talk, a reply and Live mode.
- A press and a release may still make the fraction-of-a-second gap that came before the first fix. Starting and stopping recording reconfigures the shared audio hardware, and no documented API avoids that while still ducking. Whether the gap remains, and how long it is, is a device check in `ios/README.md`.
- Apple asks for `duckOthers` to be used for a few seconds at a time. Live mode keeps music ducked for as long as it is on, as ADR 0032 decided.
- Starting Live mode while a reply plays changes the active `playback` session to `playAndRecord`, as it did before the first fix. Turning Live mode on mid-reply is rare, and its first device check will show whether it disturbs music.
- Package tests check the sequence of session calls through a fake driver: one activation per use, deactivation only after the audio object stops, and no activation of an already active session.
