# The iPhone holds its audio session so a Talk never cuts music

With music playing, holding the Talk key made the music stop for a fraction of a second before it came back ducked, and releasing the key stopped it again before it returned to full volume. ADR 0032 settled that music is ducked, never paused.

A Talk activated a `playAndRecord` session on every press and deactivated it on every release. Activating or deactivating that session moves the audio hardware between playback and play-and-record, which can change the route and the input and output configuration, and a music app plays a gap while it does. The options were not the cause: `duckOthers` already implies mixing. This cause is inferred from the code and iOS's documented behaviour; it has not been reproduced on a device.

While the app is in the foreground and paired, `NativeTalkAudioSession` now holds the session active in a standby setup: `playAndRecord` with `mixWithOthers`, `defaultToSpeaker` and `allowBluetoothA2DP`, no duck. A press switches the options of the active session to `recordingOptions`, which add `duckOthers`. A release switches them back. Neither calls `setActive`. Standby ends when the app leaves the foreground or the User unpairs, with `notifyOthersOnDeactivation`. A reply still uses the `playback` session of ADR 0032 and still deactivates on release, so a podcast still pauses and resumes; standby resumes straight after it, and after Live mode.

## Considered Options

- Move the duck without leaving the session active: not possible, because iOS ends a duck only when the session deactivates or its options change.
- Play replies in the standby session too. Rejected: `interruptSpokenAudioAndMixWithOthers` needs the `playback` category, and without it a podcast would play under Jarvis.
- Prepare the microphone engine ahead of the press. Not done: the engine does not touch the music app; the session activation does. It remains the next step if the device check still hears a gap.

## Consequences

- The first Talk after launch, after a reply and after Live mode no longer activates the session itself; a reply's release and Live mode's end now reactivate standby, which can themselves make a short gap in music. It was a gap at a reply's end before, too.
- An idle foreground app holds a `playAndRecord` session. It shows no microphone indicator, because the engine does not run, but it may affect the route of other audio. The device check in `ios/README.md` covers this.
- Package tests check the sequence of session calls through a fake driver. Whether the gap is gone is a device check.
