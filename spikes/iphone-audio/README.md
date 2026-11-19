# iPhone audio experiments

This throwaway app supports E1 to E3 for #95. It is independent of the Jarvis iPhone app and uses no Hub, network service or model quota. Results feed the later music Spec; no behavior measured here is assumed in the first iPhone slice.

Install XcodeGen (`brew install xcodegen`). Complete Xcode's first-launch setup, sign into the free personal team in Xcode, connect and unlock the iPhone, and enable Developer Mode/trust when iOS asks. Find the device identifier with `xcrun devicectl list devices` and the team id in Xcode's Signing settings. Then run:

```bash
DEVELOPMENT_TEAM=your-team-id DEVICE_ID=your-device-id IOS_DEPLOYMENT_TARGET=26.0 ./spikes/iphone-audio/install.sh
```

Set `IOS_DEPLOYMENT_TARGET` to the connected phone's actual iOS version (26.0 above is an example, not a measurement). The User's phone version has not yet been confirmed. The current SDK names the Bluetooth input option `allowBluetoothHFP`; the spike has also been compiled against an iOS 17 deployment target. Bundle ids must be available to that personal team; change the spike's `PRODUCT_BUNDLE_IDENTIFIER` in `project.yml` if necessary. The command generates an ignored Xcode project, builds with automatic signing, installs and launches. A free-team build needs reinstalling every seven days. It makes no purchases.

On the phone, start music or a podcast in another app first. Select a recording setup and microphone, then **Record 5 seconds**. The app records only those five seconds; selecting AirPods fails visibly if no HFP input exists. Each run reports the actual input and output routes. Play the recording with each playback setup. Schedule playback with each delay, then switch apps or lock the phone. **Stop run** cancels a pending run, stops recording/playback and releases the session. The session is inactive during a scheduled delay, deliberately allowing iOS suspension to prevent playback. Do not add silent audio, timers in the background or other keepalive workarounds: E3 measures that limit.

The app writes one temporary five-second recording, replaces it on the next recording and does not upload it. Its in-memory log can be shared explicitly with **Share run log**. A missing recording makes playback fail visibly. The audio background capability permits an already playing clip to continue; it does not promise that delayed playback can begin after suspension.

Use the complete [run checklist](../../docs/notes/iphone-audio-experiments.md). Native Form controls use system semantic colors, Dynamic Type and VoiceOver labels. This is an experiment panel, not the production Conversation screen; it intentionally shows the session choices being tested. No browser stack is available. Device display, microphone, AirPods, signing, install, and background behavior remain unverified until the device runs.

Local verification on this Mac used Xcode 27.1's iPhoneOS 27.1 SDK, generated the Xcode project, and compiled and linked an unsigned arm64 executable with Swift 6 and an iOS 17 deployment target:

```bash
mkdir -p spikes/iphone-audio/.build/Unsigned.app
xcrun --sdk iphoneos swiftc -parse-as-library -swift-version 6 \
  -target arm64-apple-ios17.0 -sdk "$(xcrun --sdk iphoneos --show-sdk-path)" \
  -emit-executable -o spikes/iphone-audio/.build/Unsigned.app/JarvisAudioSpike \
  spikes/iphone-audio/Sources/AudioSpikeApp.swift
```

This verifies compilation and linking, not device installation. Xcode first-launch initialization and device discovery have not completed in this session; the signed project build, installation, all controls and audio outcomes still need the device checklist. The spike uses native semantic colors rather than a custom palette. The contrast checker confirms black/white text pairs at 21.00:1 in both orders; rendered native control colors and accessibility still need on-device inspection.
