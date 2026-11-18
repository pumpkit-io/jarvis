# iPhone audio experiments E1 to E3

Ticket #95, Spec #91. Status: waiting on the User for device runs. No run below has been measured. The harness is [spikes/iphone-audio](../../spikes/iphone-audio/README.md).

## Method

Record the iPhone model, iOS version, AirPods model/firmware, music/podcast app, app version, volume, and build commit here before running: **waiting on the User**. Build and install with the spike's install command using the personal team; confirm microphone permission. Free-team trust, signing and physical installation still need verification.

E1 uses `playAndRecord`, default mode, with mix, duck, or no mixing option, and explicitly selects either the built-in microphone or AirPods HFP input. Bluetooth HFP and A2DP are allowed. For each run, start other audio, record five seconds speaking a known sentence, listen to the recording, and copy the actual input/output routes from the log. Note whether music continued, lowered, paused, changed quality or changed route. Note whether it recovered after capture stopped. Repeat the matrix with music and spoken content.

E2 uses `playback`, spokenAudio mode: duckOthers; duckOthers plus interruptSpokenAudioAndMixWithOthers; or no category options (plain playback). Start each other-audio source afresh, play the recorded five-second clip, and record behavior both during and after it. Set volume gently before each run. The goal is to observe the selected setup, never to assert a route or recovery that did not happen.

E3 schedules the same recording after each delay. Start with the app foregrounded and idle, schedule, then leave it as indicated. No audio session is held while waiting. Note both the scheduled and actual start time, whether the app was suspended, and whether playback happened only when reopening it. Restore other audio before each trial. Repeat failures once and preserve both results. Background audio capability is enabled; it must not be mistaken for permission to start arbitrary audio after suspension.

Every result cell must become a real observation or stay **waiting on User**. An unavailable input is a result, not a successful AirPods run. Share the log after runs and link it here only after removing private speech or identifying device details if necessary. The current build keeps one temporary recording on-device and the log in memory.

## E1: recording while other audio plays

| Other audio | Microphone | Setup | Music behavior, actual routes, recording quality, recovery |
|---|---|---|---|
| Music | Built-in | Mix | Waiting on User |
| Music | Built-in | Duck | Waiting on User |
| Music | Built-in | Pause | Waiting on User |
| Music | AirPods HFP | Mix | Waiting on User |
| Music | AirPods HFP | Duck | Waiting on User |
| Music | AirPods HFP | Pause | Waiting on User |
| Podcast/spoken audio | Built-in | Mix | Waiting on User |
| Podcast/spoken audio | Built-in | Duck | Waiting on User |
| Podcast/spoken audio | Built-in | Pause | Waiting on User |
| Podcast/spoken audio | AirPods HFP | Mix | Waiting on User |
| Podcast/spoken audio | AirPods HFP | Duck | Waiting on User |
| Podcast/spoken audio | AirPods HFP | Pause | Waiting on User |

## E2: playback and recovery

| Other audio | Output | Setup | Behavior during clip, actual route, recovery after clip |
|---|---|---|---|
| Music | Phone speaker | Duck | Waiting on User |
| Music | Phone speaker | Duck and interrupt spoken | Waiting on User |
| Music | Phone speaker | Plain playback | Waiting on User |
| Music | AirPods | Duck | Waiting on User |
| Music | AirPods | Duck and interrupt spoken | Waiting on User |
| Music | AirPods | Plain playback | Waiting on User |
| Podcast/spoken audio | Phone speaker | Duck | Waiting on User |
| Podcast/spoken audio | Phone speaker | Duck and interrupt spoken | Waiting on User |
| Podcast/spoken audio | Phone speaker | Plain playback | Waiting on User |
| Podcast/spoken audio | AirPods | Duck | Waiting on User |
| Podcast/spoken audio | AirPods | Duck and interrupt spoken | Waiting on User |
| Podcast/spoken audio | AirPods | Plain playback | Waiting on User |

## E3: delayed playback

Use the duck playback setup for this matrix; record that selection in the exported log. Repeating the matrix for other setups is optional follow-up if observations differ.

| Delay | App state after scheduling | Output | Started? Scheduled/actual time, suspension, reopening behavior |
|---|---|---|---|
| 5 seconds | Foreground | Phone speaker | Waiting on User |
| 5 seconds | Foreground | AirPods | Waiting on User |
| 5 seconds | Different app foregrounded | Phone speaker | Waiting on User |
| 5 seconds | Different app foregrounded | AirPods | Waiting on User |
| 5 seconds | Screen locked | Phone speaker | Waiting on User |
| 5 seconds | Screen locked | AirPods | Waiting on User |
| 30 seconds | Foreground | Phone speaker | Waiting on User |
| 30 seconds | Foreground | AirPods | Waiting on User |
| 30 seconds | Different app foregrounded | Phone speaker | Waiting on User |
| 30 seconds | Different app foregrounded | AirPods | Waiting on User |
| 30 seconds | Screen locked | Phone speaker | Waiting on User |
| 30 seconds | Screen locked | AirPods | Waiting on User |
| 2 minutes | Foreground | Phone speaker | Waiting on User |
| 2 minutes | Foreground | AirPods | Waiting on User |
| 2 minutes | Different app foregrounded | Phone speaker | Waiting on User |
| 2 minutes | Different app foregrounded | AirPods | Waiting on User |
| 2 minutes | Screen locked | Phone speaker | Waiting on User |
| 2 minutes | Screen locked | AirPods | Waiting on User |
| 10 minutes | Foreground | Phone speaker | Waiting on User |
| 10 minutes | Foreground | AirPods | Waiting on User |
| 10 minutes | Different app foregrounded | Phone speaker | Waiting on User |
| 10 minutes | Different app foregrounded | AirPods | Waiting on User |
| 10 minutes | Screen locked | Phone speaker | Waiting on User |
| 10 minutes | Screen locked | AirPods | Waiting on User |

## Cleanup and conclusions

- [ ] Stop during capture: microphone indicator goes off; other audio recovery recorded. Waiting on User.
- [ ] Stop during playback: clip stops and other audio recovery recorded. Waiting on User.
- [ ] Stop a scheduled run: no later clip starts. Waiting on User.
- [ ] Refuse microphone access: clear guidance, no recording. Waiting on User.
- [ ] Disconnect AirPods before capture: unavailable-input result, no fallback reported as AirPods. Waiting on User.
- [ ] Lock while a clip is already playing: note whether it finishes. Waiting on User.
- [ ] Review controls, Dynamic Type, VoiceOver and light/dark themes on the real phone. Waiting on User.

E1 conclusion: **not measured**. E2 conclusion: **not measured**. E3 conclusion: **not measured**. Follow-up decisions for the music Spec wait on these results; this spike blocks no other ticket in Spec #91.

API references: Apple's [duckOthers](https://developer.apple.com/documentation/avfaudio/avaudiosession/categoryoptions-swift.struct/duckothers) explains activation and release; [interruptSpokenAudioAndMixWithOthers](https://developer.apple.com/documentation/avfaudio/avaudiosession/categoryoptions-swift.struct/interruptspokenaudioandmixwithothers) describes spoken-audio interruption. These describe intended platform behavior, not measurements on this phone.
