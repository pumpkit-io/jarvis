# Jarvis on iPhone

The app target is in `JarvisApp/`. Shared Client logic is in the `JarvisClient` Swift package, which builds and tests on the Mac without a phone. This first skeleton shows the empty Conversation screen in the system's light or dark theme. Talk, typing and Voice are marked Coming soon until their tickets wire them to the Hub. It makes no network requests or recordings.

## Hub client package

`JarvisClient` provides the v1 protocol decoder and an actor-owned `HubClient`. Construct `HubTransport.urlSession(origin:token:)` using the paired HTTPS origin and Keychain token, then supply it to the client. Authorization stays in headers. The ephemeral transport rejects redirects and waits for the authenticated WebSocket upgrade with `jarvis.v1` before reading or sending. New input requires replay completion and a fresh foreground display-presence acknowledgement. A successful socket send does not acknowledge a command: `pendingCommands` retains its original id and payload until a matching result, including a visible `outcome_unknown`, arrives.

Consume `updates` from one owner for connection state, sequenced events, paired PCM clips and command results. Protocol payload structs support every documented event; unknown event types still advance the cursor. The optional `cache` closure is the persistence seam for the later store: write replayable events keyed by sequence, commit a monotonically increasing cursor, and return only after storage succeeds. A nil event commits only the replay watermark or a handled live-only sequence. Without that closure, the cursor is volatile for this client lifetime. Never persist a cursor separately from the text it covers. `history(before:limit:)` returns older pages without changing the live cursor.

The app lifecycle and audio owners must use the package policy. Call `setForeground(false, playingSpeechID:)` while foreground execution is still available, passing only the speech id actually playing, or nil when idle. The client closes immediately when idle. For existing playback it sends socket-local `set_presence(false)` and awaits acknowledgement, closing on failure or timeout. That socket can drain the same speech only; new playback is refused. Call `reportPlayback` for that stream and `playbackDrained(speechID:)` when it ends. Background path changes and failures never reconnect. Foregrounding replaces the socket and establishes fresh presence without taking Focus. Presence ids are never retried on another socket; unanswered User commands keep their original ids.

Pass `HubNetworkPaths.changes` to `start(paths:)` to replace attempts after network changes. Retain the path owner, call `stop()` and cancel the path owner when unpairing, and create a new transport/client for new pairing credentials. Path availability is only a hint: only actual HTTP/transport responses distinguish a reachable Mac proxy with a stopped Hub from a timeout or unreachable Mac. The package has not yet been wired to the skeleton views, recorder, player or persistent store.

Swift tests read the single Python-generated `Tests/JarvisClientTests/Fixtures/client-v1.json` resource directly and replay its typed exchange through the client. Fake transport, HTTP, clock, cache, path and lifecycle seams test retries, generation ownership, audio framing, reachability and background policy without starting a Hub or spending model usage.

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

`project.yml` is the checked-in project definition. The generated Xcode project, plist and build output are ignored. Open `ios/Jarvis.xcodeproj` after generating it to use the Dark and Light SwiftUI previews. Swift checks run locally; there is no Swift CI.

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

Only the light-theme Slate and Brass are darkened. The Talk key stays the only large coloured element. The red Signal is reserved for recording and is not used by this skeleton. Dynamic Type scales both type sizes, the script scrolls, and the bottom controls are kept above the safe area. The screen contains no invented conversation, Tasks or readiness claim.

## Verification still needing the device

- Install and launch under the actual Personal Team, then trust the profile if prompted.
- Check the empty script in light and dark themes, portrait and landscape, and with larger text.
- Confirm the weekly rebuild installs over the existing app.

The implementation Mac's Xcode 27.1 could generate the project, but `xcodebuild` failed before compiling: `IDESimulatorFoundation` could not load a required symbol from the installed `DVTDownloads` system framework. Xcode reported that first-launch setup was incomplete. Package build/tests and a direct iPhone SDK source type-check passed. Both themes were also inspected in offscreen macOS `NSHostingView` renders at 393 × 852 points; these are not iOS Simulator or device evidence. A full app build, simulator preview and signed device launch remain unverified until Xcode's system components are repaired and the phone/signing configuration is supplied. Do not treat these as completed device checks.

Apple documents the [New York system serif](https://developer.apple.com/documentation/technologyoverviews/fonts) used for Jarvis's script. The generated project follows [XcodeGen's project specification](https://github.com/yonaskolb/XcodeGen/blob/master/Docs/ProjectSpec.md).
