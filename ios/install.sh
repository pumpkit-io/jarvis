#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "--help" ]]; then
    cat <<'HELP'
Usage: ./ios/install.sh
Copy ios/device.env.example to ios/device.env and fill in your personal team,
connected iPhone identifier, and the iOS version shown on that phone.
Generates the project, builds/signs Jarvis and installs it on that iPhone.
Requires Xcode with first-launch setup complete and XcodeGen (brew install xcodegen).
HELP
    exit 0
fi
if [[ $# -ne 0 ]]; then
    echo "Unexpected argument. Run ./ios/install.sh --help." >&2
    exit 2
fi
if [[ -f device.env ]]; then
    source device.env
fi
: "${JARVIS_APP_TEAM:?Set JARVIS_APP_TEAM in ios/device.env to your Xcode personal team id}"
: "${JARVIS_APP_DEVICE:?Set JARVIS_APP_DEVICE in ios/device.env to the connected iPhone identifier}"
: "${JARVIS_IOS_VERSION:?Set JARVIS_IOS_VERSION in ios/device.env to the iOS version on that iPhone}"
if ! [[ "$JARVIS_IOS_VERSION" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    echo "JARVIS_IOS_VERSION must be a version such as 27.1." >&2
    exit 2
fi
if ! command -v xcodegen >/dev/null; then
    echo "Install XcodeGen first: brew install xcodegen" >&2
    exit 1
fi
if ! xcodebuild -checkFirstLaunchStatus; then
    echo "Finish Xcode's first-launch setup, then rerun ./ios/install.sh." >&2
    exit 1
fi
xcodegen generate --spec project.yml
xcodebuild -project Jarvis.xcodeproj -scheme Jarvis -configuration Debug \
    -destination "id=$JARVIS_APP_DEVICE" -derivedDataPath .build \
    -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
    "DEVELOPMENT_TEAM=$JARVIS_APP_TEAM" "IPHONEOS_DEPLOYMENT_TARGET=$JARVIS_IOS_VERSION" build
xcrun devicectl device install app --device "$JARVIS_APP_DEVICE" .build/Build/Products/Debug-iphoneos/Jarvis.app
printf 'Installed Jarvis. Open it on the iPhone.\n'
