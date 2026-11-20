// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "JarvisClient",
    platforms: [.iOS(.v18), .macOS(.v14)],
    products: [.library(name: "JarvisClient", targets: ["JarvisClient"])],
    targets: [
        .target(name: "JarvisClient"),
        .testTarget(name: "JarvisClientTests", dependencies: ["JarvisClient"], resources: [.copy("Fixtures")])
    ]
)
