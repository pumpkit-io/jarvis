#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
: "${DEVELOPMENT_TEAM:?Set DEVELOPMENT_TEAM to your personal team id from Xcode}"
: "${DEVICE_ID:?Set DEVICE_ID to your connected iPhone identifier}"
: "${IOS_DEPLOYMENT_TARGET:?Set IOS_DEPLOYMENT_TARGET to the connected phone iOS version}"
export IOS_DEPLOYMENT_TARGET
command -v xcodegen >/dev/null || { echo 'Install XcodeGen with brew install xcodegen.' >&2; exit 1; }
xcodegen generate --spec project.yml
xcodebuild -project JarvisAudioSpike.xcodeproj -scheme JarvisAudioSpike \
  -configuration Debug -destination "id=$DEVICE_ID" \
  -derivedDataPath .build/DerivedData -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM" build
xcrun devicectl device install app --device "$DEVICE_ID" \
  .build/DerivedData/Build/Products/Debug-iphoneos/JarvisAudioSpike.app
xcrun devicectl device process launch --device "$DEVICE_ID" io.pumpkit.jarvis.audio-spike
