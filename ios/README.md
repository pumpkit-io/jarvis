# Jarvis on iPhone

The app target is in `JarvisApp/`. Shared Client logic is in the `JarvisClient` Swift package, which builds and tests on the Mac without a phone. On first launch, Pairing accepts the QR printed by `./jarvis pair` or its HTTPS origin and six-digit code. The paired app opens the live Conversation script and typed composer. The mic records while held and sends on release; sliding up locks a Talk for hands-free recording and sliding left discards it. The sound button controls speech on this iPhone; Settings provides Focus and confirmed unpairing. Pairing requests camera permission only when Scan is pressed; microphone permission is requested on the first Talk press.

## Hub client package

`JarvisClient` provides the v1 protocol decoder and an actor-owned `HubClient`. Construct `HubTransport.urlSession(origin:token:)` using the paired HTTPS origin and Keychain token, then supply it to the client. Authorization stays in headers. The ephemeral transport rejects redirects and waits for the authenticated WebSocket upgrade with `jarvis.v1` before reading or sending. New input requires replay completion, a fresh foreground display-presence acknowledgement and a current positive `status.ready` value. A running Hub warming up stays Connecting. Foreground presence also has a two-second acknowledgement deadline; expiry closes the attempt and reconnects with backoff and a fresh presence id. A successful socket send does not acknowledge a command: `pendingCommands` retains its original id and payload until a matching result, including a visible `outcome_unknown`, arrives. Commands are encoded and checked against the wire envelope limits before retention, so local encoding errors cannot poison reconnect. An unanswered command retries the identical bytes twice at five-second acknowledgement deadlines on a healthy socket. After the third deadline it remains pending and appears in `unansweredCommands`; a later result or same-id reconnect retry can resolve it. Acknowledgement deadlines use an injected clock and stop on results, replacement and backgrounding.

Consume `updates` from one owner for connection state, sequenced events, paired PCM clips and command results. Protocol payload structs support every documented event; unknown event types still advance the cursor. The optional `cache` closure is the persistence seam for the later store: write replayable events keyed by sequence, commit a monotonically increasing cursor, and throw when storage has failed. The closure may return before the write reaches disk, provided every write keeps events and cursor together. A nil event commits only the replay watermark or a handled live-only sequence. Without that closure, the cursor is volatile for this client lifetime. Never persist a cursor separately from the text it covers. `history(before:limit:)` returns older pages without changing the live cursor.

The app lifecycle and audio owners must use the package policy. Call `setForeground(false, playingSpeechID:)` while foreground execution is still available, passing only the speech id whose playback session is active, including its bounded wait between clips, or nil when idle. The client closes immediately when idle. For existing playback it sends socket-local `set_presence(false)` and awaits acknowledgement, closing on failure or timeout. That socket can drain the same speech only; new playback is refused. Call `reportPlayback` for that stream and `playbackDrained(speechID:)` when it ends. Background path changes and failures never reconnect. Returning while the same healthy reply is draining renews presence on that socket, preserving queued playback. Other foreground transitions replace the socket and establish fresh presence without taking Focus. Presence ids are never retried on another socket; unanswered User commands keep their original ids.

Pass `HubNetworkPaths.changes` to `start(paths:)`. A network change pings a ready socket and replaces only one that fails to answer, or a connection that was not ready; the client also pings every 10 seconds and closes a socket that stops answering. Retain the path owner, call `stop()` and cancel the path owner when unpairing, and create a new transport/client for new pairing credentials. Path availability is only a hint: only actual HTTP/transport responses distinguish a reachable Mac proxy with a stopped Hub from a timeout or unreachable Mac. The app's `AppSession` owns the single updates consumer, network-path owner and foreground policy. It loads one coherent Keychain credential record, starts a fresh Client for that identity, and clears credentials on token refusal. In-flight pairing is generation-guarded so an old response cannot restore an unpaired identity. The Talk recorder, speech player and durable conversation cache are integrated.

Swift tests read the single Python-generated `Tests/JarvisClientTests/Fixtures/client-v1.json` resource directly and replay its typed exchange through the client. Fake transport, HTTP, clock, cache, path and lifecycle seams test retries, generation ownership, audio framing, reachability and background policy without starting a Hub or spending model usage. The native receive seam injects Foundation WebSocket messages while keeping the real adapter conversion and native buffer configuration. A Python-loopback recording of 70 KB shown text and a reply exercises that boundary through cache and reconnect. The 64 KiB limit applies to outbound Client commands; native receives use the platform integer maximum because v1 defines no Hub event size ceiling. These tests do not establish a real URLSession handshake, device connection or iOS background execution.

## Conversation

`AppSession` is the only consumer of `HubClient.updates`. It routes replayable events through the package's `ConversationProjection`, which retains canonical events and groups script rows by request/reply identity. Sequence deduplication handles reconnect replay; older events can enter the same projection in sequence order for later history support. Live deltas append, while `reply_end` replaces the text with the authoritative final reply. Both Clients appear without source filtering. A show arriving before text creates the reply anchor; an unassociated show has its own stable Details anchor. Details use native disclosure and literal selectable text, with no remote content loading.

The input bar queues typed lines through the Outbox, including while the Mac is unreachable. Empty lines and lines over 16,384 UTF-8 bytes cannot send. With a draft, its round action sends the line; with an empty field, it records a Talk. A queued line remains a User bubble with a clock until its `heard` event arrives, and a queued Talk also shows a waveform and duration. Definitive failures retain their original content with Try again and Delete; an uncertain item offers Send again. Retrying puts the item last in the queue. The draft stays in memory, and unpairing clears the conversation and Outbox with the existing generation guards.

Jarvis's replies use full-width serif text without a bubble. The User's lines sit in trailing Deep bubbles. Details, deliberate Play and the spoken-sentence underline keep their existing identity and behavior.

The screen follows the accepted palette and script typography and keeps Details expansion keyed to stable rows. One id-based scroll-position binding retains the top visible row during history prepend. iOS 18 scroll geometry measures bottom proximity; only reader scrolling changes whether new text is followed. Separate default anchors start at the latest text, keep the bottom fixed during streaming and composer-size changes while following, and leave short scripts top-aligned. Opening or closing Details suspends following through its native animation so the disclosure opens in place. Once that layout settles, the current bottom geometry restores following if the reader is still at the bottom, including when the whole script fits. Newer disclosure changes retire earlier animation completions. Conversation gaps over one hour use event timestamps. Status comes directly from the Client, including warmup and Usage-limit reset information; stale text remains readable offline. The shared input bar stays above the keyboard and remains available while typing. The main screen opens in orb mode on every launch. Only the chevron at the top of the preview expands and collapses the conversation; it points up in orb mode and bends down when expanded. Dragging the conversation only scrolls it. Dragging down past the keyboard dismisses it interactively, as in Messages, and tapping the orb dismisses it too. The script column is exactly as wide as the viewport and its scroll anchors are leading, so resizing for the keyboard, a Talk or expansion cannot shift it sideways. The same conversation view remains mounted, retaining its reading anchor, Details and history state. Expanded mode hides the orb and sound button and pauses the orb; when it returns, the orb resumes where it stopped. The orb is one white sphere of 14,000 points drawn with Metal (`ParticleOrb.metal`): a faint grid on the front face and a bright rim. The package's `OrbMotion` eases its level and surface toward the current speaker at any frame rate, while the rotation keeps one rate. Speech ripples the outline, opens tears across the surface and lets wisps drift off the edge, more for Jarvis than for the User; the orb swells up to 9% with the voice. Microphone and playback levels are read in decibels, so ordinary speech moves the orb clearly. Reduce Motion slows the surface and rotation and softens speech. With the keyboard open, the orb area shrinks while reserving three scaled reply lines; if a 120-point orb area cannot fit too, the screen expands until the keyboard closes. Collapsing while the keyboard is up dismisses it. Keyboard, scrolling gestures and VoiceOver still need installed-device verification.

Native macOS-hosted renders at 393 × 852 points were inspected for populated, streaming, empty, offline, Usage-limit and expanded Details states in both themes. Rejection, terminal uncertainty, newer-draft preservation and restored-composer recovery states were also inspected in both themes. Separate 38/30-point base-type stress renders check wrapping at twice the normal 19/15-point sizes. The host does not exercise iOS Dynamic Type, safe areas or the on-screen keyboard. Swift 6 direct iPhone SDK compilation and linking cover the package and full native app; these are not a signed device build or launch.

The script column takes its width from the viewport, with 20-point margins and a 640-point cap. Replies, Details and the empty-script line lead within that column; User lines trail at its other edge. Every conversation row fills the same column before its contents are aligned, so newly rendered or streaming text cannot determine the column width. Playback marks the spoken sentence with a single underline and inherits the paragraph's serif face and weight. This implements the underline-only departure from `DESIGN.md` specified in #153. Native host renders at 320, 393 and 800 points in both themes cover empty, short, User, streaming and playback states. Comparing the unhighlighted paragraph with each of two highlighted sentences changed only two-pixel underline bands; glyph positions and line breaks were identical. Playback uses fake audio output in these checks. Watching a long reply stream and play on the iPhone remains a device check.

## Offline history

The production `AppSession` factory loads a `HistoryStore` before constructing its Client, hydrates the existing conversation projection, and supplies the durable cursor and cache callback to `HubClient`. No second updates consumer or transcript interpreter is added. Cached status never enables Talk, typing, playback or Focus: the actual current handshake decides readiness, and the status line keeps the actual connection reason. Cached completed replies offer deliberate Play through the existing replay command; hydration never plays audio.

The store keeps the history in memory and writes replayable events and their monotonic live watermark in one atomic record under Application Support/Jarvis/History. Writes are batched at most every half second, and on going to the background and merging older pages, so a reconnect's catch-up of thousands of events costs one write; a store ended before its batch leaves the previous consistent record, and the Hub replays the rest. A hash of the normalized Hub origin and Client id selects the namespace; the device token is never stored there. Files are private to the account and excluded from backup. Thirty-day pruning runs on load and on updates once an event is old enough, keeping the watermark through live-only audio gaps without storing speech metadata or PCM. Corrupt history is discarded and fetched again without revoking valid pairing credentials. While writes fail, each commit retries the write and throws, so the Client stops advancing its cursor and reconnects from what it holds.

The top of the script fetches older pages while connected, with a single request in flight and a separate continuation from the live cursor. Pages merge into the same sequence-deduplicating projection, preserve stable Details ids, and stop at the server's final page. Native scroll-position anchors preserve the reading position on prepend, and prepend does not trigger bottom-follow. Errors leave a retry control. Replay pruning is reported in the script. Unpairing clears the owned cache and retires that store against late callbacks; generation and captured-Client guards discard old page completions. Re-pairing creates a new Client id and namespace.

Package tests use temporary directories, fake sockets/HTTP and an injected clock to consume the canonical Python fixture unchanged. They verify restart, monotonic audio-only watermarks, deduplication, pruning, origin separation, production factory hydration without cached readiness, corruption recovery, single-flight paging alongside live events, and delayed pages after unpairing. No test writes the User's Application Support directory. The pre-existing history/duplicate Client test now awaits its independent updates observer before asserting the unchanged exact sequence list; this fixes an observer race without changing production history behavior.

Native macOS-hosted renders cover offline cached text, populated, loading history, history failure and doubled base type in both themes at 320 and 393 points. The scroll check also feeds live deltas and older history into the actual Conversation view, then inspects native frames at both widths and themes. Native clip movement bracketed by wheel phases checks reading position; it does not establish physical touch scrolling or the iPhone keyboard. Tasks currently shows task summaries and shares the messaging-style input bar with Conversation; it has no embedded Conversation script, so the script changes apply to Conversation. The original Swift 6 compilation and linking targeted the iPhone SDK at iOS 17. These checks do not establish installed-iPhone scrolling, Dynamic Type, VoiceOver, safe areas, keyboard behavior or offline relaunch. Those physical checks remain Waiting on User.

## Talk

Hold the round mic to record a Talk, whatever the connection. The input bar shows a red dot, timer and “‹ Slide to cancel”; the mic grows and a lock capsule appears above it. Slide up past 60 points to lock the Talk, then release your finger and keep speaking. The locked bar has trash, timer, a live microphone waveform and Send. Slide left past 120 points while holding, or tap trash while locked, to discard without putting anything in the Outbox. Release an unlocked Talk or tap Send on a locked Talk to queue it once. A capture shorter than 0.3 seconds sends nothing and shows “Hold to talk.” Start, lock and discard give haptic feedback; discard also animates a trash symbol. The thresholds are named controller constants for device tuning.

VoiceOver activation starts a Talk and the next activation sends it; Cancel Talk discards it. There is one gesture, with no Hold/Tap preference. Existing saved gesture files are ignored and removed on unpair. The first press requests microphone permission. A refusal keeps the existing message and Open Settings link. Releasing before permission resolves, cancelling, backgrounding, unpairing or leaving the control retires the capture intent; a delayed grant cannot turn the microphone on. A locked Talk is also retired on those lifecycle changes.

`TalkController` owns capture intent. `NativeTalkRecorder` activates the audio session, converts input to 16 kHz mono PCM16, and synchronously stops its source and releases the session before any acknowledgement or upload wait. The audio callback buffer caps the body at 9,600,000 bytes, independently of the five-minute UI timer. Neither idle nor thinking activates the session. A Talk stops local speech through the session's player hook and sends `interrupt` with only fully completed clips before `talk_begin`. `AppSession` stops its native player before awaiting the interrupt or recording. It sends one interrupt carrying the last contiguous finished clip, without a second stopped report that could cancel a newer turn.

`talk_begin`, `talk_cancel` and `interrupt` go through `sendLive`: written in order on the current ready socket, never retained or resent, and skipped when there is none. A released Talk goes into the Outbox (ADR 0027) under the begun Talk id; a press shorter than 0.3 seconds is cancelled instead. The Outbox saves each Talk and typed line under Application Support, then sends them one at a time, oldest first, through a background `URLSession` (`POST /v1/utterances/{talk_id}` and `POST /v1/lines`) that waits for the network and finishes while the app is suspended. A lost response retries the same ids with backoff; a Hub refusal that admitted nothing retries under new ids; `outcome_unknown` and refused content stay on screen with Delete, and Try again for failures. Each item shows under the conversation until its `heard` event arrives; Try again (or Send again, for an uncertain one) sends it as the newest message. A live Talk released while older messages wait cancels its begin and goes as a queued Talk, so it never holds the Mac's microphone while waiting. Temporary Hub refusals retry until accepted. When iOS wakes the suspended app for a finished upload, the next one is handed over. If iOS has ended the app, a locked phone cannot read the device token, so the rest wait until the app is opened.

Public package tests inject permission, capture source, recorder, session, upload, socket and clock fakes. They cover immediate session release, bounded PCM, the five-minute stop, cancellation, delayed grants across lifecycle transitions, recording while unreachable, interruption order and Outbox order, retries, renewals, persistence and background handover. No test opens a microphone or spends model usage.

The dark input bar uses Frost on Deep, Cyan controls with Void icons and Signal recording/discard indicators. Package tests exercise hold, lock, release, explicit send, slide and trash discard, too-short feedback, VoiceOver activation, delayed permission and cleanup on control disappearance, backgrounding and unpairing. Native simulator checks use fake audio and never open a microphone.

Physical threshold feel, haptics, VoiceOver and microphone-indicator release after locked send/discard still need the User's iPhone.

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

The orb's shader needs Xcode's Metal Toolchain; if the app build reports it missing, run `xcodebuild -downloadComponent MetalToolchain` once. The app icon is the single 1024-pixel opaque image in `JarvisApp/Assets.xcassets/AppIcon.appiconset`.

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

The sound button beside the orb changes speech on this iPhone. It appears only in orb mode. Voice off stops local playback and releases its audio session immediately, without an interrupt or stopped receipt. The scoped `set_silence` command then stops destination synthesis while canonical text and Tasks continue. Talk still records and sends normally; changing Voice during capture keeps that capture active. Mac speech is unchanged. Voice on applies to future speech; hearing an older or skipped reply requires deliberate Play. Play is disabled while Voice is off.

An explicit choice is saved atomically as one boolean under Application Support/Jarvis/Voice, with a namespace hashed from the normalized Hub origin and Client id. The file contains no token, conversation or PCM, has private permissions and is excluded from backup. Unpairing clears that pairing's choice. Temporary stores are injected in tests. With no explicit local choice, the Client learns the Hub's existing Silence snapshot and leaves it unchanged.

On reconnect, the Client sends the latest desired Silence and waits for its acknowledgement before foreground presence, held news or ready input. An uncertain retry keeps its original command identity; a newer choice retires older setting retries. Native update revisions prevent an old Off confirmation from confirming a later Off → On → Off sequence. The sole session consumer retains its pairing and foreground guards. Live Voice changes leave an already healthy Talk ready. Pending or uncertain delivery remains visible, and a local save failure remains visible after remote acknowledgement.

Public package tests cover restart through the production factory, offline choice ordering on actual fake sockets, stale acknowledgements, queued update revisions, capture continuity, playback release and save failures. Protocol tests exercise a held model in both text and voice modes, proving complete text after Silence, no further phone synthesis, unchanged Mac playback and offline Voice off before held-news presence. Native macOS-hosted renders cover on, off, speaking, recording, offline and doubled base type in both themes at 320 and 393 points. Swift 6 compilation and linking target the iPhone SDK at iOS 17.

Waiting on User: installed-iPhone Voice off mid-sentence, AirPods/current-route behavior, physical session release, offline relaunch, VoiceOver, Dynamic Type, safe areas and signed launch. Host renders, fake audio tests and SDK linking do not establish those device results. No live Hub, model, device or account action was used.

## Settings

Open Settings from the top-bar icon. Talk always uses hold, slide-up lock and slide-left discard; there is no gesture setting. Leaving the Talk control cancels an active hold or locked recording without sending, while leaving after send keeps the admitted Outbox item.

Send updates here requests Focus without starting a conversation or model turn. Updates come to this iPhone follows current sequenced Hub status, including another Client taking Focus; a late command acknowledgement cannot override it. Definite offline failure says “Couldn't reach the Mac. Not changed.” An unanswered request remains pending under its original identity and is described as unconfirmed, since it may already have reached the Hub. The Settings screen shows the paired Mac's name and confirms Unpair this iPhone before invoking the existing credential, history, audio and preference cleanup.

Package tests use fake sockets, recorders, permission gates and private temporary stores. They cover hold and locked capture, delayed permission, old-view cleanup, legacy gesture-file removal on unpair, stale Focus results and uncertain delivery. Device gestures, permission prompts, VoiceOver and signed navigation remain manual checks.

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

The app forces dark appearance and uses one palette, independent of the system setting. Ratios were computed with the `ui-design` skill's `scripts/contrast-check.py`. Text needs 4.5:1; control edges and indicators need 3:1.

| Foreground | Background | Ratio |
|---|---|---|
| Frost `#E6EEF5` | Void `#05070C` | 17.19:1 |
| Frost `#E6EEF5` | Deep `#0E1420` | 15.72:1 |
| Haze `#7F8EA3` | Void `#05070C` | 6.05:1 |
| Haze `#7F8EA3` | Deep `#0E1420` | 5.53:1 |
| Void `#05070C` | Cyan `#33E6FF` | 13.34:1 |
| Signal edge `#D6402E` | Deep `#0E1420` | 4.06:1 |
| Cyan edge `#33E6FF` | Void `#05070C` | 13.34:1 |
| Green indicator `#3DDC84` | Void `#05070C` | 11.29:1 |
| Amber indicator `#F5B544` | Void `#05070C` | 11.11:1 |

Recording uses Frost text on Deep with a Signal edge: Frost on a solid Signal fill would fail the normal-text threshold. Jarvis's replies retain the system serif at 18 points relative to body; other screen copy uses system sans relative to subheadline. Dynamic Type scales both sizes and labels wrap.

Native macOS-hosted renders of Pairing, Settings, Tasks and Conversation were inspected at 320 and 393 points. The package build and tests and the generated app's unsigned iPhone and iPhone Simulator SDK builds passed. These renders verify the actual SwiftUI screen code but do not establish iPhone safe-area, keyboard or physical accessibility results.

## Verification still needing the device

Use the [real-device checklist](../docs/notes/iphone-real-device-verification.md) for setup, cellular/AirPods, two-Client room, security, lifecycle and accessibility checks. Every physical result remains Waiting on User.

- Install and launch under the actual Personal Team, then trust the profile if prompted.
- Check the empty script in dark appearance, portrait and landscape, and with larger text.
- Confirm the weekly rebuild installs over the existing app.

An initial project build with the implementation Mac's Xcode 27.1 failed before compilation: `IDESimulatorFoundation` could not load a required symbol from the installed `DVTDownloads` system framework, and Xcode reported incomplete first-launch setup. A later check at commit `77e6836` built both Jarvis and the audio spike from the same archived commit using a process-local override for matching Xcode-bundled private frameworks. With a generic iOS destination, signing disabled and an iOS 17 deployment floor, both generated projects produced unsigned arm64 `.app` bundles. Both bundles declare iPhone only (`UIDeviceFamily: [1]`) and audio background mode; the main app retains its camera and microphone descriptions, and the spike retains its microphone description. The override did not repair global host configuration or complete Xcode setup.

Package build/tests, direct Swift 6 iOS 17 SDK linking and actual-source macOS `NSHostingView` renders also passed. Synthetic Mac mouse events verified Talk key press/release feedback through fake audio; these are not physical iPhone gestures. A simulator preview, signed installation and device launch remain unverified. The actual phone version, Team, signing configuration and every physical checklist result still need the User.

Apple documents the [New York system serif](https://developer.apple.com/documentation/technologyoverviews/fonts) used for Jarvis's script. The generated project follows [XcodeGen's project specification](https://github.com/yonaskolb/XcodeGen/blob/master/Docs/ProjectSpec.md).

## Tasks

The Tasks icon in the top bar opens Tasks and shows the same open-Task count as the list. Tasks waiting for the User come first, with the full committed Approval question, followed by running Tasks and their last reports. Answer through Talk or type in the input bar; the list has no Approval action buttons.

Tasks reuse the conversation's retained events. Cached Tasks stay readable offline and appear in secondary text with “Updating…” until a fresh Task snapshot arrives. Older history pages cannot restore a Task removed by a newer snapshot. Conversation and Tasks share the input bar and its Talk owner. Return to orb mode to change Silence.
