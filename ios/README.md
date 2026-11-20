# Jarvis on iPhone

The app target is in `JarvisApp/`. Shared Client logic is in the `JarvisClient` Swift package, which builds and tests on the Mac without a phone. On first launch, Pairing accepts the QR printed by `./jarvis pair` or its HTTPS origin and six-digit code. The paired app opens the live Conversation script and typed composer. The Talk key records while held and sends on release. Voice on/off controls speech on this iPhone; Settings provides Hold or Tap gestures, Focus and confirmed unpairing. Pairing requests camera permission only when Scan is pressed; microphone permission is requested on the first Talk press.

## Hub client package

`JarvisClient` provides the v1 protocol decoder and an actor-owned `HubClient`. Construct `HubTransport.urlSession(origin:token:)` using the paired HTTPS origin and Keychain token, then supply it to the client. Authorization stays in headers. The ephemeral transport rejects redirects and waits for the authenticated WebSocket upgrade with `jarvis.v1` before reading or sending. New input requires replay completion, a fresh foreground display-presence acknowledgement and a current positive `status.ready` value. A running Hub warming up stays Connecting. Foreground presence also has a two-second acknowledgement deadline; expiry closes the attempt and reconnects with backoff and a fresh presence id. A successful socket send does not acknowledge a command: `pendingCommands` retains its original id and payload until a matching result, including a visible `outcome_unknown`, arrives. Commands are encoded and checked against the wire envelope limits before retention, so local encoding errors cannot poison reconnect. An unanswered command retries the identical bytes twice at five-second acknowledgement deadlines on a healthy socket. After the third deadline it remains pending and appears in `unansweredCommands`; a later result or same-id reconnect retry can resolve it. Acknowledgement deadlines use an injected clock and stop on results, replacement and backgrounding.

Consume `updates` from one owner for connection state, sequenced events, paired PCM clips and command results. Protocol payload structs support every documented event; unknown event types still advance the cursor. The optional `cache` closure is the persistence seam for the later store: write replayable events keyed by sequence, commit a monotonically increasing cursor, and return only after storage succeeds. A nil event commits only the replay watermark or a handled live-only sequence. Without that closure, the cursor is volatile for this client lifetime. Never persist a cursor separately from the text it covers. `history(before:limit:)` returns older pages without changing the live cursor.

The app lifecycle and audio owners must use the package policy. Call `setForeground(false, playingSpeechID:)` while foreground execution is still available, passing only the speech id whose playback session is active, including its bounded wait between clips, or nil when idle. The client closes immediately when idle. For existing playback it sends socket-local `set_presence(false)` and awaits acknowledgement, closing on failure or timeout. That socket can drain the same speech only; new playback is refused. Call `reportPlayback` for that stream and `playbackDrained(speechID:)` when it ends. Background path changes and failures never reconnect. Returning while the same healthy reply is draining renews presence on that socket, preserving queued playback. Other foreground transitions replace the socket and establish fresh presence without taking Focus. Presence ids are never retried on another socket; unanswered User commands keep their original ids.

Pass `HubNetworkPaths.changes` to `start(paths:)` to replace attempts after network changes. Retain the path owner, call `stop()` and cancel the path owner when unpairing, and create a new transport/client for new pairing credentials. Path availability is only a hint: only actual HTTP/transport responses distinguish a reachable Mac proxy with a stopped Hub from a timeout or unreachable Mac. The app's `AppSession` owns the single updates consumer, network-path owner and foreground policy. It loads one coherent Keychain credential record, starts a fresh Client for that identity, and clears credentials on token refusal. In-flight pairing is generation-guarded so an old response cannot restore an unpaired identity. The Talk recorder, speech player and durable conversation cache are integrated.

Swift tests read the single Python-generated `Tests/JarvisClientTests/Fixtures/client-v1.json` resource directly and replay its typed exchange through the client. Fake transport, HTTP, clock, cache, path and lifecycle seams test retries, generation ownership, audio framing, reachability and background policy without starting a Hub or spending model usage. The native receive seam injects Foundation WebSocket messages while keeping the real adapter conversion and native buffer configuration. A Python-loopback recording of 70 KB shown text and a reply exercises that boundary through cache and reconnect. The 64 KiB limit applies to outbound Client commands; native receives use the platform integer maximum because v1 defines no Hub event size ceiling. These tests do not establish a real URLSession handshake, device connection or iOS background execution.

## Conversation

`AppSession` is the only consumer of `HubClient.updates`. It routes replayable events through the package's `ConversationProjection`, which retains canonical events and groups script rows by request/reply identity. Sequence deduplication handles reconnect replay; older events can enter the same projection in sequence order for later history support. Live deltas append, while `reply_end` replaces the text with the authoritative final reply. Both Clients appear without source filtering. A show arriving before text creates the reply anchor; an unassociated show has its own stable Details anchor. Details use native disclosure and literal selectable text, with no remote content loading.

The typed composer sends `say_text` only after actual Client readiness. Empty lines and lines over 16,384 UTF-8 bytes cannot send. Each submission retains its original command and tracks acknowledgement separately from local transport completion. A write failure or unanswered result remains uncertain; the same pending text cannot create a new command id. The Client owns exact-id retry and reconnect. A definitive rejection keeps its original words selectable and offers Restore line. Terminal `outcome_unknown` results offer Review and restore line, with explicit confirmation after checking the conversation; restoration only fills the composer, and a separate Send action creates a new command. A still-retained uncertain request cannot use this recovery path and continues to retry its original id. Restoration never replaces a newer draft: the original words stay available to copy until the current draft is finished or cleared. Known preflight failure is reconciled against the Client’s retained command and completed result in one actor turn. A generic queued connection failure cannot turn a command that was never retained into a phantom pending request. The original words remain in the composer, or stay selectable and restorable if a newer draft exists. Late send completion preserves a known result and an explicitly restored draft. Unpairing clears the script and pending line state, with generation checks preventing old async completions from changing a new session. The draft remains memory-only. Durable history hydrates this same projection before connecting. The defaulted `AppSession.sendCommand` boundary delegates directly to `HubClient.send`; fake tests can hold that boundary while an actual fake-socket disconnect reaches the real Client preflight guard. Separate controlled socket tests deliver accepted/rejected results before a late write error. Tests observe public session recovery, draft and result behavior, without manipulating private queues.

The screen follows the accepted palette and script typography and keeps Details expansion keyed to stable rows. One id-based scroll-position binding retains the top visible row during history prepend. iOS 18 scroll geometry measures bottom proximity; only reader scrolling changes whether new text is followed. Separate default anchors start at the latest text, keep the bottom fixed during streaming and composer-size changes while following, and leave short scripts top-aligned. Opening Details suspends following so the disclosure opens in place. Conversation gaps over one hour use event timestamps. Status comes directly from the Client, including warmup and Usage-limit reset information; stale text remains readable offline. The composer sits in the safe-area inset and hides the Talk key and Voice control while typing. Keyboard, scrolling gestures and VoiceOver still need installed-device verification.

Native macOS-hosted renders at 393 × 852 points were inspected for populated, streaming, empty, offline, Usage-limit and expanded Details states in both themes. Rejection, terminal uncertainty, newer-draft preservation and restored-composer recovery states were also inspected in both themes. Separate 38/30-point base-type stress renders check wrapping at twice the normal 19/15-point sizes. The host does not exercise iOS Dynamic Type, safe areas or the on-screen keyboard. Swift 6 direct iPhone SDK compilation and linking cover the package and full native app; these are not a signed device build or launch.

## Offline history

The production `AppSession` factory loads a `HistoryStore` before constructing its Client, hydrates the existing conversation projection, and supplies the durable cursor and cache callback to `HubClient`. No second updates consumer or transcript interpreter is added. Cached status never enables Talk, typing, playback or Focus: the actual current handshake decides readiness, and the status line keeps the actual connection reason. Cached completed replies offer deliberate Play through the existing replay command; hydration never plays audio.

The store writes replayable events and their monotonic live watermark in one atomic record under Application Support/Jarvis/History. A hash of the normalized Hub origin and Client id selects the namespace; the device token is never stored there. Files are private to the account and excluded from backup. Thirty-day pruning runs on load and durable updates, keeping the watermark through live-only audio gaps without storing speech metadata or PCM. Corrupt history is discarded and fetched again without revoking valid pairing credentials. A storage failure prevents the Client from advancing its cursor.

The top of the script fetches older pages while connected, with a single request in flight and a separate continuation from the live cursor. Pages merge into the same sequence-deduplicating projection, preserve stable Details ids, and stop at the server's final page. Native scroll-position anchors preserve the reading position on prepend, and prepend does not trigger bottom-follow. Errors leave a retry control. Replay pruning is reported in the script. Unpairing clears the owned cache and retires that store against late callbacks; generation and captured-Client guards discard old page completions. Re-pairing creates a new Client id and namespace.

Package tests use temporary directories, fake sockets/HTTP and an injected clock to consume the canonical Python fixture unchanged. They verify restart, monotonic audio-only watermarks, deduplication, pruning, origin separation, production factory hydration without cached readiness, corruption recovery, single-flight paging alongside live events, and delayed pages after unpairing. No test writes the User's Application Support directory. The pre-existing history/duplicate Client test now awaits its independent updates observer before asserting the unchanged exact sequence list; this fixes an observer race without changing production history behavior.

Native macOS-hosted renders cover offline cached text, populated, loading history, history failure and doubled base type in both themes at 320 and 393 points. The scroll check also feeds live deltas and older history into the actual Conversation view, then inspects native frames at both widths and themes. Native clip movement bracketed by wheel phases checks reading position; it does not establish physical touch scrolling or the iPhone keyboard. Tasks currently shows task summaries and shares Talk/Voice controls through `WorkspaceView`; it has no embedded Conversation script, so the script changes apply to Conversation. Swift 6 compilation and linking target the iPhone SDK at iOS 18. These checks do not establish installed-iPhone scrolling, Dynamic Type, VoiceOver, safe areas, keyboard behavior or offline relaunch. Those physical checks remain Waiting on User.

## Talk

The full-width key records only while held. It shows a timer and “Release to send. Slide up to cancel.” while recording, then “Jarvis is thinking” while uploading and awaiting the reply. Sliding up by 60 points cancels that gesture without uploading; sliding back does not begin another capture. The first press requests microphone permission. A refusal shows “Jarvis needs the microphone to hear you. Allow it in Settings.” and an Open Settings link. A permission dialog, release, cancellation, lost readiness, backgrounding or unpairing retires the old hold, so a delayed grant cannot turn the microphone on. Returning from the permission dialog can require a fresh hold.

`TalkController` owns capture intent and upload generations. `NativeTalkRecorder` activates the audio session, converts input to 16 kHz mono PCM16, and synchronously stops its source and releases the session before any acknowledgement or upload wait. The audio callback buffer caps the body at 9,600,000 bytes, independently of the five-minute UI timer. Neither idle nor thinking activates the session. A Talk stops local speech through the session's player hook and sends `interrupt` with only fully completed clips before `talk_begin`. `AppSession` stops its native player before awaiting the interrupt or recording. It sends one interrupt carrying the last contiguous finished clip, without a second stopped report that could cancel a newer turn.

A begin is capture intent, so it is the exception to generic command retention. On release, `finishTalkBegin` removes its retry intent and waits at most two seconds for the existing socket's result before uploading. Cancellation or lost connection removes an unanswered begin and closes the uncertain socket to release any admitted floor. It is never replayed after reconnecting. Other unanswered commands keep their existing retry behavior. An accepted upload uses one id and byte-identical PCM on connection retries for at most 30 seconds from the first attempt. Cancellation, backgrounding and unpairing discard late completions. Canonical transcript and reply events go through the sole `AppSession` consumer; a reply that finished before the HTTP response does not leave the key thinking forever.

Public package tests inject permission, capture source, recorder, session, upload, socket and clock fakes. They cover immediate session release, bounded PCM, the five-minute stop, cancellation, delayed grants across lifecycle transitions, acknowledgement loss, no deferred begin, exact upload retries, failure, interruption identity and early canonical completion. No test opens a microphone or spends model usage.

Native macOS-hosted renders were inspected at 320 × 740 and 393 × 852 points in both themes for recording, uploading, thinking, unreachable, refused, failed and cancelled states. Explicit 38/30-point type stress checks wrapping. The original Signal red against white was 4.45:1; the adjusted `#D6402E` reaches 4.53:1, with a 3.11:1 edge against Baize and 3.86:1 against Linen. This keeps red reserved for active recording. These renders and a Swift 6 direct iOS 17 compile/link do not establish physical Dynamic Type, microphone-indicator timing, permission-dialog behavior, audio routing, VoiceOver or real gestures.

Waiting on the User's hands: install and exercise the hold, slide-up, permission refusal/Settings recovery and five-minute flow on the iPhone; verify the system microphone indicator and audio session are off immediately on release/cancel and during idle/thinking. Device checks remain in the first-slice checklist. No device, simulator, actual capture, permission prompt or live Hub was used for this implementation.

## Pairing

Run `./jarvis pair` on the Mac with its configured HTTPS origin. Scan the QR or choose Type the code and enter that same origin and all six digits, including leading zeroes. Camera refusal leaves manual entry available and provides a Settings link. Scan capture stops on acceptance, dismissal and backgrounding.

The unauthenticated pairing POST uses an ephemeral URLSession, refuses redirects and validates the entire response, protocol version and normalized origin before saving credentials. It never retries automatically: if the successful response was lost, create a new code on the Mac. The returned token, origin, Client id and display names live together in a single `WhenUnlockedThisDeviceOnly` Keychain item, behind the fakeable `PairingStore` boundary. No credentials are written to preferences, logs or files. The Settings screen calls `AppSession.unpair()` after confirmation.

Fake POST/store/session/permission tests run without a Hub, camera, microphone or Keychain access. Light/dark Pairing states have been inspected in native macOS-hosted phone-size renders; direct Swift 6 iOS 17 SDK compilation and linking cover package and app sources. Neither proves actual camera scanning or Keychain behavior on an installed iPhone. Those checks, including install/relaunch, token revocation against a real Hub and permission changes in system Settings, still need the device.

## Build and test locally

```sh
swift build --package-path ios/JarvisClient
swift test --package-path ios/JarvisClient
brew install xcodegen
xcodegen generate --spec ios/project.yml
xcodebuild -project ios/Jarvis.xcodeproj -scheme Jarvis \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath ios/.build CODE_SIGNING_ALLOWED=NO build
```

`project.yml` is the checked-in project definition. The generated Xcode project, plist and build output are ignored. Open `ios/Jarvis.xcodeproj` after generating it to inspect the app in Xcode. Swift checks run locally; there is no Swift CI.

### Native audio callback regression

The microphone tap factory is nonisolated. It installs with the input node's hardware format (`nil`) and creates its converter from the first received buffer. `NativeTalkCaptureTests.swift` feeds real PCM to that handler from a background queue, checks the converted bytes, and verifies the byte-limit callback reaches the main actor without opening a microphone.

To reproduce the actor violation if the factory loses `nonisolated`, run the tests with actor runtime checks. The native build system ensures this compiler flag is applied on the current Swift toolchain:

```sh
swift test --package-path ios/JarvisClient --build-system native \
  --scratch-path /tmp/jarvis-tap-actor-checks \
  -Xswiftc -enable-actor-data-race-checks --filter nativeTap
```

With an actor-isolated factory, the background test terminated with signal 5; with the nonisolated factory, both tests passed. A default package test run on Swift 6.4 omitted the callback's runtime check and passed even before the fix.

For a callback audit, emit checked iOS simulator SIL:

```sh
swiftc -swift-version 6 -enable-actor-data-race-checks -parse-as-library \
  -emit-sil -module-name JarvisClient -target arm64-apple-ios18.0-simulator \
  -sdk "$(xcrun --sdk iphonesimulator --show-sdk-path)" \
  ios/JarvisClient/Sources/JarvisClient/*.swift > /tmp/jarvis-callbacks.sil 2>&1
```

The audit found no `swift_task_isCurrentExecutor` in the tap returned by `NativeTalkCaptureSource.makeTapHandler`, the `NativePCMConverter` input block, `NativeSpeechOutput`'s scheduled-buffer completion, or `HubNetworkPaths`' path-update handler. All are nonisolated. Their explicit `Task { @MainActor in ... }` hops retain main-actor delivery. The URLSession delegate methods on `RefuseRedirects`, `URLSessionHubSocket`, `TalkRedirectRefusal` and `PairingRedirectGuard` also have no executor check; the other URLSession operations use async APIs rather than completion closures. Physical microphone capture still needs the iPhone check below.

## Speech playback

`AppSession` routes live speech from its sole update consumer to `SpeechPlayer`. The default recorder and player share one `NativeTalkAudioSession`: recording uses `playAndRecord`, playback uses `playback` with `spokenAudio`, and an owner check prevents an old playback release from deactivating recording. Neither thinking nor a speech header activates the session.

The player validates stream, reply and contiguous clip identity, converts 24 kHz mono PCM16LE to native buffers, and schedules available buffers ahead on `AVAudioPlayerNode`. Its `dataPlayedBack` callbacks advance contiguous finished receipts and the exact sentence highlight. Queued buffers are submitted before earlier playback finishes; a partial clip never advances the heard index. `speech_end` waits for actual queue drain. When output drains while synthesis is still pending, a two-second watchdog releases the engine/session; a valid later clip can reactivate it. Generation checks discard callbacks from stopped or replaced streams. Empty PCM stops with a visible retry message and no finished receipt.

Only a stream with an active playback session can retain its socket on background departure, including the existing two-second wait between clips. A speech header alone cannot retain it. After physical playback or the watchdog releases the session, an outstanding receipt flush can retain transport until the ordered drain completes, even if completion happened in foreground. Presence is withdrawn first, including when a foreground presence acknowledgement is still pending. That stream's reports continue through `reportPlayback`, and actual drain or the idle watchdog retires it through `playbackDrained` after queued reports finish. A repeated non-active scene preserves that receipt flush without admitting audio after watchdog expiry. Foreground resumption of the same speech supersedes an older queued drain; only the latest report window can retire its socket. Idle background closes immediately. A typed connection-end update stops the matching local stream on socket retirement; unrelated acknowledgement diagnostics leave healthy playback alone. A Hub stop and the local `silencePlayback()` hook release locally without sending a cancellation back. The Voice control uses this hook before sending scoped Silence to the Hub.

Missed audio is tracked by reply identity. Play sends `replay_speech`, never audio copied from history, a new conversation turn or a Focus command. A pending retry retains its command id; a definitive result clears it so the next deliberate Play gets a fresh id. Rejection and transport uncertainty remain visible beside the reply. Unpairing clears playback and replay state.

Package tests use fake output, audio sessions, clocks and sockets, including the unchanged Python-produced speech fixture. They cover ahead scheduling, exact PCM, ordered reports, end before drain, long clips, watchdog reactivation, stale callbacks, record/play exclusion, background drain, healthy acknowledgement timeouts, socket loss, empty PCM, local Silence and replay ids. Native speaking, missed and offline states were inspected in both themes at 320 and 393 points. Doubled base-type stress keeps the Talk key full width. These macOS-hosted frames and the Swift 6 iOS 17 SDK link do not verify actual iPhone execution.

Waiting on User: installed-device AirPods/current-route playback, lock-screen drain, microphone indicator release, safe areas, VoiceOver, Dynamic Type and signed launch. No real audio, model or account run was used for this change.

## Voice on and Voice off

The Voice control beside Talk changes speech on this iPhone. Voice off stops local playback and releases its audio session immediately, without an interrupt or stopped receipt. The scoped `set_silence` command then stops destination synthesis while canonical text and Tasks continue. Talk still records and sends normally; changing Voice during capture keeps that capture active. Mac speech is unchanged. Voice on applies to future speech; hearing an older or skipped reply requires deliberate Play. Play is disabled while Voice is off.

An explicit choice is saved atomically as one boolean under Application Support/Jarvis/Voice, with a namespace hashed from the normalized Hub origin and Client id. The file contains no token, conversation or PCM, has private permissions and is excluded from backup. Unpairing clears that pairing's choice. Temporary stores are injected in tests. With no explicit local choice, the Client learns the Hub's existing Silence snapshot and leaves it unchanged.

On reconnect, the Client sends the latest desired Silence and waits for its acknowledgement before foreground presence, held news or ready input. An uncertain retry keeps its original command identity; a newer choice retires older setting retries. Native update revisions prevent an old Off confirmation from confirming a later Off → On → Off sequence. The sole session consumer retains its pairing and foreground guards. Live Voice changes leave an already healthy Talk ready. Pending or uncertain delivery remains visible, and a local save failure remains visible after remote acknowledgement.

Public package tests cover restart through the production factory, offline choice ordering on actual fake sockets, stale acknowledgements, queued update revisions, capture continuity, playback release and save failures. Protocol tests exercise a held model in both text and voice modes, proving complete text after Silence, no further phone synthesis, unchanged Mac playback and offline Voice off before held-news presence. Native macOS-hosted renders cover on, off, speaking, recording, offline and doubled base type in both themes at 320 and 393 points. Swift 6 compilation and linking target the iPhone SDK at iOS 17.

Waiting on User: installed-iPhone Voice off mid-sentence, AirPods/current-route behavior, physical session release, offline relaunch, VoiceOver, Dynamic Type, safe areas and signed launch. Host renders, fake audio tests and SDK linking do not establish those device results. No live Hub, model, device or account action was used.

## Settings

Open Settings from the Conversation screen. Hold to talk remains the default. With Tap to start and stop selected, the first tap starts the same Talk recorder and the next sends it. Tap to interrupt stops the current reply before recording. Sliding up cancels; changing gesture or leaving the Talk control cancels an active capture without sending it. Leaving the screen after release keeps the admitted upload intact. Permission, backgrounding, disconnect and unpair guards still apply. The gesture is stored privately beside this pairing's Voice choice and is removed on unpair.

Send updates here requests Focus without starting a conversation or model turn. Updates come to this iPhone follows current sequenced Hub status, including another Client taking Focus; a late command acknowledgement cannot override it. Definite offline failure says “Couldn't reach the Mac. Not changed.” An unanswered request remains pending under its original identity and is described as unconfirmed, since it may already have reached the Hub. The Settings screen shows the paired Mac's name and confirms Unpair this iPhone before invoking the existing credential, history, audio and preference cleanup.

Package tests use fake sockets, recorders, permission gates and private temporary stores. They cover tap capture/send, delayed permission, old-view cleanup, gesture changes during capture, restart and unpair, stale Focus results and uncertain delivery. Device gestures, permission prompts, VoiceOver and signed navigation remain manual checks.

## Install on your iPhone

Complete these once:

1. Open Xcode, finish its first-launch setup and add your Apple ID in Settings > Accounts. Choose your free Personal Team; no paid membership is required for this slice.
2. Connect the iPhone, trust the Mac, and enable Developer Mode on the phone if requested.
3. Copy `ios/device.env.example` to `ios/device.env`. Set the personal Team ID, the connected device identifier from `xcrun devicectl list devices`, and the iOS version in the phone's Settings > General > About. `device.env` is a local shell configuration, so keep only your own assignments there.

Then rebuild, sign and install with one command:

```sh
./ios/install.sh
```

The command can run from any directory. It generates the project, selects the configured phone, uses automatic development signing and installs the result. `JARVIS_IOS_VERSION` sets the app's deployment target to the actual phone's version; update it when that phone is updated. The app and package require iOS 18 or later for scroll geometry and size-change anchoring. Configure the phone's actual version at or above that minimum. Environment variables with the names in `device.env.example` may also supply the configuration when there is no local file.

Open Jarvis on the phone. If iOS asks, trust the developer profile in Settings. A free Personal Team build expires after seven days; rerun the same command to renew it. The app is for this iPhone, with no iPad layout or push notifications in this slice.

## Palette verification

Ratios were computed with the `ui-design` skill's `scripts/contrast-check.py`. Text needs 4.5:1; control edges need 3:1.

| Use | Foreground | Background | Ratio |
|---|---|---|---|
| Dark script | Linen `#E9EEEA` | Baize `#16302B` | 11.99:1 |
| Dark status and outlines | Slate `#8FA39C` | Baize | 5.29:1 |
| Dark Talk key edge | Brass `#C39A4A` | Baize | 5.39:1 |
| Dark Talk label | Baize | Brass | 5.39:1 |
| Light script | Baize | Linen | 11.99:1 |
| Original light status | Slate `#8FA39C` | Linen | 2.27:1, fails |
| Original light key edge | Brass `#C39A4A` | Linen | 2.22:1, fails |
| Adjusted light status and outlines | `#536D64` | Linen | 4.78:1 |
| Adjusted light Talk key | `#806020` | Linen | 4.95:1 |
| Light Talk label | Linen | `#806020` | 4.95:1 |

Only the light-theme Slate and Brass are darkened. The Talk key stays the only large coloured element. The red Signal marks active recording. Dynamic Type scales both type sizes, the script scrolls, and the bottom controls are kept above the safe area. The screen contains no invented conversation, Tasks or readiness claim.

## Verification still needing the device

Use the [real-device checklist](../docs/notes/iphone-real-device-verification.md) for setup, cellular/AirPods, two-Client room, security, lifecycle and accessibility checks. Every physical result remains Waiting on User.

- Install and launch under the actual Personal Team, then trust the profile if prompted.
- Check the empty script in light and dark themes, portrait and landscape, and with larger text.
- Confirm the weekly rebuild installs over the existing app.

An initial project build with the implementation Mac's Xcode 27.1 failed before compilation: `IDESimulatorFoundation` could not load a required symbol from the installed `DVTDownloads` system framework, and Xcode reported incomplete first-launch setup. A later check at commit `77e6836` built both Jarvis and the audio spike from the same archived commit using a process-local override for matching Xcode-bundled private frameworks. With a generic iOS destination, signing disabled and an iOS 17 deployment floor, both generated projects produced unsigned arm64 `.app` bundles. Both bundles declare iPhone only (`UIDeviceFamily: [1]`) and audio background mode; the main app retains its camera and microphone descriptions, and the spike retains its microphone description. The override did not repair global host configuration or complete Xcode setup.

Package build/tests, direct Swift 6 iOS 17 SDK linking and actual-source macOS `NSHostingView` renders also passed. Synthetic Mac mouse events verified Talk key press/release feedback through fake audio; these are not physical iPhone gestures. A simulator preview, signed installation and device launch remain unverified. The actual phone version, Team, signing configuration and every physical checklist result still need the User.

Apple documents the [New York system serif](https://developer.apple.com/documentation/technologyoverviews/fonts) used for Jarvis's script. The generated project follows [XcodeGen's project specification](https://github.com/yonaskolb/XcodeGen/blob/master/Docs/ProjectSpec.md).

## Tasks

The Conversation header opens Tasks and shows the same open-Task count as the list. Tasks waiting for the User come first, with the full committed Approval question, followed by running Tasks and their last reports. Answer through Talk or return to Conversation to type; the list has no Approval action buttons.

Tasks reuse the conversation's retained events. Cached Tasks stay readable offline and appear in secondary text with “Updating…” until a fresh Task snapshot arrives. Older history pages cannot restore a Task removed by a newer snapshot. The Talk and Voice controls share the same owners across Conversation and Tasks.
