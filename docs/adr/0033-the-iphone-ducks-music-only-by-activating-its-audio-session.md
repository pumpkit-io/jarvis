# The iPhone ducks music only by activating its audio session

ADR 0032 settled that music is ducked under Jarvis, never paused. On a real iPhone 15 Pro (iOS 26.7.3), a Talk's press made music stop for a fraction of a second before it came back ducked, and its release did the same before music returned to full volume. The orb on the home screen also froze for a moment on a press.

## Duck by activating, resume by deactivating

iOS ties ducking to activation. Apple documents `duckOthers` this way: "Ducking begins when you activate your app's audio session and ends when you deactivate the session." Changing the options of a session that is already active is not a documented way to start or end a duck. A first attempt held a `playAndRecord` session active and switched `duckOthers` on a press and a release; on the device music fell to near silence on a press, and on a release it stopped and stayed stopped. An app a session has interrupted resumes only when that session deactivates with `notifyOthersOnDeactivation`, and the held session never deactivated.

`NativeTalkAudioSession` therefore has one lifecycle for every use. A Talk, a reply and Live mode each set their category and options while the session is inactive and then activate it, which starts the duck. When the use ends, after its microphone or speech output has stopped, the session deactivates with `notifyOthersOnDeactivation`, which ends the duck and lets an app the session interrupted resume. Nothing changes the options of an active session, and nothing holds the session between uses. Every option set is mixable: `mixWithOthers` is written out next to `duckOthers` and `interruptSpokenAudioAndMixWithOthers`, which already imply it. While Live mode is on, a Talk and a reply use its active session and make no session call of their own. The options themselves are ADR 0032's.

Every activation first asks for the sample rate and IO buffer duration the hardware already runs at, so joining it does not restart other apps' audio at a new format. Asking for the current format is at worst a no-op.

## The audio work runs off the main thread

The Talk path used to do all its audio work on the main thread in one call: set the category, activate, build and start an `AVAudioEngine`; and on a release or a cancel, stop the engine and deactivate. Each step returns only when the audio hardware has finished reconfiguring, and that wait was the orb's freeze.

`NativeTalkAudioSession` now runs a Talk's activation, microphone start, microphone stop and every deactivation on one serial audio queue. A press shows recording at once and the queue activates the session and starts the microphone after. A release or a cancel returns the recorded audio at once, and the queue stops the microphone and deactivates after. If the microphone fails on the queue, the session deactivates there and the Talk ends as failed, or a resumed Talk returns to paused. A Talk released before the queue reaches its start never activates. A reply and Live mode still activate synchronously, because their output starts right after, but through the same queue, so neither can overtake a Talk still stopping.

Nothing is prepared before the press. An `AVAudioEngine` whose input node is read before the session is `playAndRecord` takes the format of an inactive route and must be rebuilt, and keeping an engine running between Talks is the held microphone rejected below.

## Known limitation: a short music gap on press, release and cancel

A Talk's press, release and cancel still stop music for a fraction of a second before the duck or the return. This is accepted. It is the microphone joining the shared audio hardware, and it follows from three device rounds rather than a measurement: a reply's `playback` activation, which needs no microphone, never gaps, while every design that turned the microphone on and off at the press and the release did. No documented API adds a microphone to running hardware without that reconfiguration, and iOS ties ducking to activation, so no design that activates per Talk can both duck and leave music untouched. The Talk's session already matches what Apple's SDK headers document for recording while another app plays: mode `default`, no voice processing, `allowBluetoothA2DP` without HFP so AirPods stay on A2DP, `defaultToSpeaker`, the microphone stopped before deactivating, and the hardware format pinned.

The only arrangement with no change at a press or a release is a microphone already open before the press. Live mode is that arrangement today, and a Talk during Live mode makes no session call. Holding a microphone open for every Talk was rejected: it shows the orange microphone indicator for as long as the app is open, uses more battery, and only moves the gap to opening and leaving the app.

## Considered Options

- Hold the session active and switch only `duckOthers` on a press and a release. Rejected: undocumented, and on the device it ducked through silence and stopped music for good.
- Hold the session active and deactivate it only on a release. Rejected: each release would then deactivate and reactivate a `playAndRecord` session, two hardware changes instead of one.
- Record without ducking, so a press and a release need no session change. Rejected: the User wants music lower while they speak.
- Keep the Talk's audio work on the main thread and only prepare the engine ahead of the press. Rejected: the waits are the hardware reconfiguring, which preparing cannot remove, and the orb would still freeze.
- Keep the microphone engine running while the app is in the foreground. Rejected, as above.
- Change the Talk's mode to `measurement`, `videoRecording`, `voiceChat` or `videoChat`. Rejected: none is documented to avoid the hardware change, the chat modes bring HFP and voice processing, and `duckOthers` is documented for mode `default`.
- Capture with `AVAudioRecorder` or an input-only audio queue instead of `AVAudioEngine`. Not adopted: the microphone starting is the change, whichever API starts it.
- Deactivate while the microphone engine still runs, to merge two changes into one. Rejected: the SDK header calls deactivating over running I/O a misuse.

## Consequences

- Music keeps playing and returns to its own level by itself after a Talk, a reply and Live mode.
- The orb and the Talk controls no longer wait for the audio hardware on a press, a release or a cancel.
- A press, a cancel and a release make a short music gap on the device. A Talk during Live mode does not. The device checks are in `ios/README.md`.
- A Talk's clock starts at the press, slightly before the microphone records. Audio from that moment was never captured before either, since the main thread was blocked until the engine ran.
- Apple asks for `duckOthers` to be used for a few seconds at a time. Live mode keeps music ducked for as long as it is on, as ADR 0032 decided.
- Starting Live mode while a reply plays changes the active `playback` session to `playAndRecord`. Turning Live mode on mid-reply is rare.
- Package tests check the sequence of session calls through a fake driver: one activation per use, deactivation only after the audio object stops, no activation of an already active session, no Talk call on the main thread, a press and a release returning while the hardware is still busy, and a quick tap that never turns the microphone on.
