# iPhone real-device verification

Manual `real_device` checklist for ticket #120, Spec #91. Status: **Waiting on User** for every physical, account and real-provider check below. No signed install, real Hub/model run, cellular measurement or device audio observation has been performed for this checklist. Automated evidence is separate at the end. Replace each result with the observed outcome, date, build commit and sanitized evidence; a failure stays a failure. If a platform assumption fails, record it and stop that path rather than invent a workaround.

## Record the actual setup

| Record | Result |
|---|---|
| Build commit, Mac/macOS/Xcode versions | Waiting on User |
| Current iPhone model and Settings > General > About iOS version | Waiting on User |
| Personal Team and signing/install result (do not publish identifiers) | Waiting on User |
| AirPods model/firmware, actual input/output route and volume | Waiting on User |
| Actual Jarvis HTTPS origin/port, tailnet ACL and cellular/Wi-Fi route | Waiting on User |

Cached phone inventories do not establish which phone or iOS version is in use. Follow [installation](../../ios/README.md#install-on-your-iphone), supplying the actual phone's version, identifier and Team in local `ios/device.env`. Do not commit that file, tokens, QR codes, private speech or raw credential/cache files.

## Set up the private door

Perform these with the User present. Starting Jarvis spends a hidden Talker warm-up turn; actual requests spend model usage. The Mac must stay awake and on power with its terminal open.

| Step and expected observation | Result |
|---|---|
| Install/sign in to Tailscale on the actual iPhone and Mac in the same tailnet. Confirm their online state. | Waiting on User |
| Inspect `tailscale serve status` before editing anything. HTTPS 443 on the implementation Mac currently serves unrelated loopback 3000; preserve it. Choose an unused additional HTTPS port (for example 8443 only if free), using the [status-first setup](../../README.md#remote-access). Never reset/overwrite Serve or enable Funnel. | Waiting on User |
| Enable tailnet HTTPS and restrict ACL access to the User's own devices on the selected HTTPS port, removing broader rules that defeat the restriction. Check authorized and unauthorized devices separately. | Waiting on User |
| Set `JARVIS_REMOTE=on`, loopback port and exact root `JARVIS_REMOTE_ORIGIN`, including the additional HTTPS port. No non-root path prefix is supported. Start the Hub; confirm the origin from the phone. | Waiting on User |
| Run `./jarvis pair` in another terminal. Scan the QR; also test manual origin and six-digit entry including a leading zero when issued. Confirm five-minute expiry, single use and useful error/recovery. | Waiting on User |
| Deny camera permission: manual entry still works and Settings recovery is available. Relaunch with a valid pairing: credentials/history survive; revoked pairing returns to Pairing. | Waiting on User |
| Build/sign/install, trust the developer profile if required, launch and relaunch. After seven days, rebuild with `./ios/install.sh` and verify renewal over the existing app. | Waiting on User |

## First slice on cellular

Turn Wi-Fi off on the phone and verify cellular plus Tailscale is the actual route. Keep the Mac connected normally. Start a harmless, User-chosen Task from the terminal and note its exact name so both Clients can refer to the same work.

| Check and expected observation | Result |
|---|---|
| With default Hold selected, hold Talk and ask about that terminal-started Task. Release sends once. Hear the answer in AirPods and see the same canonical User line and reply once in both phone and terminal histories. | Waiting on User |
| In Hold mode the system microphone indicator is off whenever Talk is not held: before press, after release, slide-up cancellation, idle and thinking. Verify permission-delayed release does not start a late capture. | Waiting on User |
| Select Tap in Settings: first tap records and second tap sends. The indicator may remain on between taps by design; it turns off on send, cancel, background or leaving the control. Restore Hold for the previous check. | Waiting on User |
| Deny microphone permission and recover through Settings. No recording/upload occurs after refusal. Hold, release, slide-up cancellation and the five-minute limit behave as labeled without leaving capture active. | Waiting on User |
| Turn Voice off mid-sentence: phone speech and its playback session stop immediately while complete reply text and Tasks continue. Mac speech is unchanged. Voice on affects future speech; deliberate Play is required for missed audio. | Waiting on User |
| Lock/background while an existing reply plays: only that reply may drain; no new proactive reply starts in the background. Idle departure closes immediately. Reopen to recover text once, without automatic audio replay. | Waiting on User |
| Switch cellular/Wi-Fi, briefly lose reachability, and return. The talk key and typing stay available; a drop under 3 seconds leaves the header on ready. Talks and lines recorded offline show Waiting to send, then reach Jarvis once, in order, when the Mac is reachable again, also after force-quitting the app. No audio is repeated on recovery. Leave the app idle for 2 minutes: the header stays on Jarvis is ready. | Waiting on User |
| Send a typed phone line and use Tasks/Conversation navigation. Draft, Details, full Approval questions, open count, older history, cached Updating state and shared Talk/Voice remain coherent. Answer an Approval through normal Talk/typing, not a separate approval button. | Waiting on User |
| Settings preserves Hold/Tap and Voice choices after relaunch. Send updates here moves Focus without a conversation turn; another Client can move it back. Uncertain delivery is described as unconfirmed. Unpair requires confirmation and clears local pairing, history and preferences. | Waiting on User |
| Inspect both themes at actual safe areas, portrait/landscape, largest practical Dynamic Type and with VoiceOver. Read/tap navigation, input, Talk, cancellation, Voice, Tasks, Settings and confirmation controls without clipping or inaccessible actions. | Waiting on User |

## E7: two Clients in one room

Place the Mac and phone side by side. First use AirPods, then use the phone speaker at a safe audible volume to expose echo. Record which Client has Focus before each step. Use one identifiable request/update at a time and compare both histories.

| Check and expected observation | Result |
|---|---|
| Talk to the phone while Mac voice mode is listening: one transcript, one request and one reply; the Mac does not wake, admit input or act on the phone Talk or its reply. Nothing is heard or spoken twice. | Waiting on User |
| Talk to the Mac: only the Mac speaks the reply, while both histories show it once. Focus moves with accepted input. Repeat phone → Mac → phone. | Waiting on User |
| Generate one Task update with phone Focus and foreground presence: the phone speaks it once and Mac stays silent. Repeat with Mac Focus. | Waiting on User |
| Background the focused phone while idle: news waits. Reopen it: held news is heard once. Reconnecting/foregrounding alone never takes Focus from the Mac. | Waiting on User |
| Start a phone Talk during Mac speech, then cancel; repeat with release/send. Mac playback/capture ownership remains correct, and cancellation never starts a second action. | Waiting on User |
| Leave after speaking to the Mac: Focus stays there, so a subsequent Announcement may speak to the room. This is an accepted limit, not automatic phone handoff. | Waiting on User |

## Security and replay from another tailnet machine

Use a separate authorized machine with Python 3.12 and the [reference Client](../../tools/README.md). Keep private credentials/cache/output outside the repo. Use the exact Jarvis root HTTPS origin and port. The reference Client saves WAVs but never plays them or claims physical playback.

| Check and procedure | Result |
|---|---|
| Before pairing, request `/v1/health`, `/v1/history` and the WebSocket upgrade without Authorization, then with an invented invalid bearer token. Expect HTTP 401, no event stream and no sensitive response. Use a local script or HTTP client; never put a real token in a URL or shared transcript. | Waiting on User |
| Pair a disposable reference identity: `python reference_client.py pair ORIGIN --name 'Verification reference'` (enter code secretly). Start `python reference_client.py --timeout 600 listen`. Confirm authenticated health/history and socket access work over HTTPS. | Waiting on User |
| On the Mac, use `./jarvis clients`, then `./jarvis revoke 'Verification reference'` using the exact assigned name. The idle socket closes with policy refusal (1008); the next authenticated HTTP request is 401. Reference Client erases the refused token and asks to pair again. | Waiting on User |
| Pair again with a fresh code. Keep `listen` active while a separate terminal input starts a long reply. Interrupt only this machine's network mid-reply, then restore it. Inspect emitted JSON sequence ids and final `history`: missed replayable events arrive once, final canonical text matches the phone/Mac and no request is repeated. Sequence gaps are normal; speech is live-only and is not replayed automatically. | Waiting on User |
| Confirm an unauthorized tailnet machine cannot reach the Serve origin at all under the ACL. This is distinct from an authorized machine reaching the Hub and receiving 401 for a bad token. Record both outcomes. | Waiting on User |

Use `uv run --frozen python tools/reference_client.py` from a checkout, or `python reference_client.py` on the separate machine. `history` reads the private cache offline. Do not treat its intentional printing of cached events as duplicate network delivery. Restore that machine's networking after the test; reconcile history before retrying uncertain input.

## Terminal lifecycle matrix

Restart between rows after changing `.env`; do not run concurrent Hubs against the same state. Use a harmless request and one Task update. Door-on text mode lazily loads remote speech services without opening Mac audio; terminal input remains text-only unless `--speak` is explicit.

| Launch / door | Check | Result |
|---|---|---|
| `./jarvis`, off | Wake word, Talk, follow-up, stop phrase, typed text-only input and Task update behave as before; no remote listener. | Waiting on User |
| `./jarvis --text`, off | Typed replies/Tasks, no audio device requirement and no remote listener. | Waiting on User |
| `./jarvis`, on | Same terminal behavior plus phone Talk, shared histories and Focus without duplicate capture/speech (E7). | Waiting on User |
| `./jarvis --text`, on | Terminal remains text-only; phone Talk gets transcription and sentence audio without opening the Mac microphone/speaker. | Waiting on User |
| `./jarvis --text --speak`, on | Explicit terminal speech still works; phone Silence affects only phone speech. | Waiting on User |
| Both modes, off/on | Ctrl-D or Ctrl-C ends the Hub and listener even with unfinished phone playback; undelivered news persists. Restart restores Tasks/waits, not the prior conversation session or session Authorization. | Waiting on User |

## Remaining experiments

| Evidence | Result |
|---|---|
| E1–E3 physical recording, music/podcast ducking/recovery, routes and delayed background starts using the [spike matrix](iphone-audio-experiments.md). These inform the next music slice; no coexistence claim is made here. | Waiting on User |
| Physical native capture/playback and lock/background ownership across the cases above. Fake clocks/audio are not evidence of iOS scheduling or permission behavior. | Waiting on User |
| E5: [20 Wi-Fi + 20 LTE real-provider runs](iphone-latency-e5.md), medians/spread, upload and physical first-audio boundaries. Target median release-to-first-audio ≤2.5 s; a miss requires a compression/streaming decision, not an invented passing number. | Waiting on User |
| Physical Voice off, interruption and playback completion behavior through the cases above; preserve actual routes and observations. | Waiting on User |
| ADR 0032 music ducking, -16 LUFS speech level, AirPods A2DP with the iPhone's microphone, and Live mode echo at the new level, using the [music and speech level checks](../../ios/README.md#music-and-speech-level-device-checks). | Waiting on User |

## Automated evidence and its limits

The reproducible provider-free gates are `uv run pytest -q`, `uv run pyright`, `swift build --package-path ios/JarvisClient` and `swift test --package-path ios/JarvisClient`. Real-model and real-GitHub markers remain off. The [protocol fixture and loopback coverage](../protocol/client-v1.md#shared-exchange-fixtures) and [reference Client tests](../../tools/README.md#local-proof-without-providers) cover authentication/revocation, replay, idempotency and two-Client behavior through fake agents/audio. The native package tests cover permissions, transport, lifecycle, projection, recording/playback and settings through injected seams.

The integrated #119 baseline passed 1,904 Python tests (34 skipped), Pyright and 134 Swift tests. Swift 6 iOS 17 SDK compilation/linking and macOS-hosted phone-size fake-backed light/dark renders were also checked. These establish source compatibility and automated behavior; they establish no signed package, installed launch, actual phone gesture, VoiceOver, Dynamic Type, permission prompt, microphone timing, AirPods route or physical latency. At that baseline, the normal Xcode project build encountered a host private-framework mismatch before compilation, and a process-local probe could list the project only. Neither observation was a device result.

The #120 documentation gate reran the full suite: 1,904 Python tests passed, 34 skipped, Pyright reported zero errors, and local Swift build plus all 134 Swift tests passed. Runtime, test, app, package and installer sources are unchanged from #119; no new SDK link, render, real-provider or physical run is claimed for this documentation change. `AGENTS.md` was audited and already requires both local Swift checks.

The whole-stack review fixes passed 1,901 Python tests (34 skipped), Pyright with zero errors, local Swift build and all 134 Swift tests. Direct Swift 6 iOS 17 SDK compilation/linking passed, and 36 actual-source Mac-hosted UI frames were generated, with sampled frames inspected. Synthetic mouse events exercised Talk key tap/hold press and release with public fake recorder/wire dependencies; the rendered key shrank while pressed and returned to full size on release. The [test-intent map](iphone-review-fixes.md) records the public transcript, identity, Approval and restart authority coverage.

Unsigned project packaging at commit `77e6836` built both the main app and audio spike from the same archived source, in 11.160 and 5.918 seconds respectively. A process-local override selected matching Xcode-bundled private frameworks; it did not repair the ordinary global host setup. Both generic-iOS builds disabled signing and produced arm64 `.app` bundles with an iOS 17 deployment floor and iPhone-only `UIDeviceFamily: [1]`. Audio background and microphone metadata remain present in both bundles; camera metadata remains present in the main app. The deployment floor is an API baseline, not a measurement of the User's phone version.

These packages establish unsigned project packaging only. No signed installation, device launch, actual iPhone gesture, permission prompt, microphone indicator, route, accessibility, network/provider or physical latency result is claimed. Every physical checklist entry remains Waiting on User.
