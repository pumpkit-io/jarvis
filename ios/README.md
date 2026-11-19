# Jarvis on iPhone

The app target is in `JarvisApp/`. Shared Client logic is in the `JarvisClient` Swift package, which builds and tests on the Mac without a phone. On first launch, Pairing accepts the QR printed by `./jarvis pair` or its HTTPS origin and six-digit code. The paired app opens the live Conversation script and typed composer. Talk and Voice remain Coming soon until their tickets wire them to the Hub. Pairing requests camera permission only when Scan is pressed; it never requests microphone access.

## Hub client package

`JarvisClient` provides the v1 protocol decoder and an actor-owned `HubClient`. Construct `HubTransport.urlSession(origin:token:)` using the paired HTTPS origin and Keychain token, then supply it to the client. Authorization stays in headers. The ephemeral transport rejects redirects and waits for the authenticated WebSocket upgrade with `jarvis.v1` before reading or sending. New input requires replay completion, a fresh foreground display-presence acknowledgement and a current positive `status.ready` value. A running Hub warming up stays Connecting. Foreground presence also has a two-second acknowledgement deadline; expiry closes the attempt and reconnects with backoff and a fresh presence id. A successful socket send does not acknowledge a command: `pendingCommands` retains its original id and payload until a matching result, including a visible `outcome_unknown`, arrives. Commands are encoded and checked against the wire envelope limits before retention, so local encoding errors cannot poison reconnect. An unanswered command retries the identical bytes twice at five-second acknowledgement deadlines on a healthy socket. After the third deadline it remains pending and appears in `unansweredCommands`; a later result or same-id reconnect retry can resolve it. Acknowledgement deadlines use an injected clock and stop on results, replacement and backgrounding.

Consume `updates` from one owner for connection state, sequenced events, paired PCM clips and command results. Protocol payload structs support every documented event; unknown event types still advance the cursor. The optional `cache` closure is the persistence seam for the later store: write replayable events keyed by sequence, commit a monotonically increasing cursor, and return only after storage succeeds. A nil event commits only the replay watermark or a handled live-only sequence. Without that closure, the cursor is volatile for this client lifetime. Never persist a cursor separately from the text it covers. `history(before:limit:)` returns older pages without changing the live cursor.

The app lifecycle and audio owners must use the package policy. Call `setForeground(false, playingSpeechID:)` while foreground execution is still available, passing only the speech id actually playing, or nil when idle. The client closes immediately when idle. For existing playback it sends socket-local `set_presence(false)` and awaits acknowledgement, closing on failure or timeout. That socket can drain the same speech only; new playback is refused. Call `reportPlayback` for that stream and `playbackDrained(speechID:)` when it ends. Background path changes and failures never reconnect. Foregrounding replaces the socket and establishes fresh presence without taking Focus. Presence ids are never retried on another socket; unanswered User commands keep their original ids.

Pass `HubNetworkPaths.changes` to `start(paths:)` to replace attempts after network changes. Retain the path owner, call `stop()` and cancel the path owner when unpairing, and create a new transport/client for new pairing credentials. Path availability is only a hint: only actual HTTP/transport responses distinguish a reachable Mac proxy with a stopped Hub from a timeout or unreachable Mac. The app's `AppSession` owns the single updates consumer, network-path owner and foreground policy. It loads one coherent Keychain credential record, starts a fresh Client for that identity, and clears credentials on token refusal. In-flight pairing is generation-guarded so an old response cannot restore an unpaired identity. Recorder, player and conversation persistence remain later tickets.

Swift tests read the single Python-generated `Tests/JarvisClientTests/Fixtures/client-v1.json` resource directly and replay its typed exchange through the client. Fake transport, HTTP, clock, cache, path and lifecycle seams test retries, generation ownership, audio framing, reachability and background policy without starting a Hub or spending model usage. The native receive seam injects Foundation WebSocket messages while keeping the real adapter conversion and native buffer configuration. A Python-loopback recording of 70 KB shown text and a reply exercises that boundary through cache and reconnect. The 64 KiB limit applies to outbound Client commands; native receives use the platform integer maximum because v1 defines no Hub event size ceiling. These tests do not establish a real URLSession handshake, device connection or iOS background execution.

## Conversation

`AppSession` is the only consumer of `HubClient.updates`. It routes replayable events through the package's `ConversationProjection`, which retains canonical events and groups script rows by request/reply identity. Sequence deduplication handles reconnect replay; older events can enter the same projection in sequence order for later history support. Live deltas append, while `reply_end` replaces the text with the authoritative final reply. Both Clients appear without source filtering. A show arriving before text creates the reply anchor; an unassociated show has its own stable Details anchor. Details use native disclosure and literal selectable text, with no remote content loading.

The typed composer sends `say_text` only after actual Client readiness. Empty lines and lines over 16,384 UTF-8 bytes cannot send. Each submission retains its original command and tracks acknowledgement separately from local transport completion. A write failure or unanswered result remains uncertain; the same pending text cannot create a new command id. The Client owns exact-id retry and reconnect. A definitive rejection keeps its original words selectable and offers Restore line. Terminal `outcome_unknown` results offer Review and restore line, with explicit confirmation after checking the conversation; restoration only fills the composer, and a separate Send action creates a new command. A still-retained uncertain request cannot use this recovery path and continues to retry its original id. Restoration never replaces a newer draft: the original words stay available to copy until the current draft is finished or cleared. Known preflight failure preserves the unsent draft. Unpairing clears the script and pending line state, with generation checks preventing old async completions from changing a new session. The script and draft are memory-only; durable history belongs to the later cache ticket and must reuse this projection.

The screen follows the accepted palette and script typography, keeps Details expansion keyed to stable rows, and follows incoming text only while the latest content is in view. Conversation gaps over one hour use event timestamps. Status comes directly from the Client, including warmup and Usage-limit reset information; stale text remains readable offline. The composer sits in the safe-area inset and hides the future Talk/Voice placeholders while typing. Keyboard, scrolling gestures and VoiceOver still need installed-device verification.

Native macOS-hosted renders at 393 × 852 points were inspected for populated, streaming, empty, offline, Usage-limit and expanded Details states in both themes. Rejection, terminal uncertainty, newer-draft preservation and restored-composer recovery states were also inspected in both themes. Separate 38/30-point base-type stress renders check wrapping at twice the normal 19/15-point sizes. The host does not exercise iOS Dynamic Type, safe areas or the on-screen keyboard. Swift 6 direct iPhone SDK compilation and linking cover the package and full native app; these are not a signed device build or launch.

## Pairing

Run `./jarvis pair` on the Mac with its configured HTTPS origin. Scan the QR or choose Type the code and enter that same origin and all six digits, including leading zeroes. Camera refusal leaves manual entry available and provides a Settings link. Scan capture stops on acceptance, dismissal and backgrounding.

The unauthenticated pairing POST uses an ephemeral URLSession, refuses redirects and validates the entire response, protocol version and normalized origin before saving credentials. It never retries automatically: if the successful response was lost, create a new code on the Mac. The returned token, origin, Client id and display names live together in a single `WhenUnlockedThisDeviceOnly` Keychain item, behind the fakeable `PairingStore` boundary. No credentials are written to preferences, logs or files. `AppSession.unpair()` exposes cleanup for the later Settings screen.

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

## Install on your iPhone

Complete these once:

1. Open Xcode, finish its first-launch setup and add your Apple ID in Settings > Accounts. Choose your free Personal Team; no paid membership is required for this slice.
2. Connect the iPhone, trust the Mac, and enable Developer Mode on the phone if requested.
3. Copy `ios/device.env.example` to `ios/device.env`. Set the personal Team ID, the connected device identifier from `xcrun devicectl list devices`, and the iOS version in the phone's Settings > General > About. `device.env` is a local shell configuration, so keep only your own assignments there.

Then rebuild, sign and install with one command:

```sh
./ios/install.sh
```

The command can run from any directory. It generates the project, selects the configured phone, uses automatic development signing and installs the result. `JARVIS_IOS_VERSION` sets the app's deployment target to the actual phone's version; update it when that phone is updated. The package's iOS 17 baseline only describes its API requirements, not the app's configured minimum. Environment variables with the names in `device.env.example` may also supply the configuration when there is no local file.

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

Only the light-theme Slate and Brass are darkened. The Talk key stays the only large coloured element. The red Signal is reserved for recording and is not used until recording is implemented. Dynamic Type scales both type sizes, the script scrolls, and the bottom controls are kept above the safe area. The screen contains no invented conversation, Tasks or readiness claim.

## Verification still needing the device

- Install and launch under the actual Personal Team, then trust the profile if prompted.
- Check the empty script in light and dark themes, portrait and landscape, and with larger text.
- Confirm the weekly rebuild installs over the existing app.

The implementation Mac's Xcode 27.1 could generate the project, but `xcodebuild` failed before compiling: `IDESimulatorFoundation` could not load a required symbol from the installed `DVTDownloads` system framework. Xcode reported that first-launch setup was incomplete. Package build/tests and a direct iPhone SDK source type-check passed. Both themes were also inspected in offscreen macOS `NSHostingView` renders at 393 × 852 points; these are not iOS Simulator or device evidence. A full app build, simulator preview and signed device launch remain unverified until Xcode's system components are repaired and the phone/signing configuration is supplied. Do not treat these as completed device checks.

Apple documents the [New York system serif](https://developer.apple.com/documentation/technologyoverviews/fonts) used for Jarvis's script. The generated project follows [XcodeGen's project specification](https://github.com/yonaskolb/XcodeGen/blob/master/Docs/ProjectSpec.md).
