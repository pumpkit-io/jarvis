# Jarvis on iPhone

The app target is in `JarvisApp/`. Shared Client logic is in the `JarvisClient` Swift package, which builds and tests on the Mac without a phone. This first skeleton shows the empty Conversation screen in the system's light or dark theme. Talk, typing and Voice are marked Coming soon until their tickets wire them to the Hub. It makes no network requests or recordings.

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
