// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "JobDestroyer",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "JobDestroyer", path: "Sources/JobDestroyer")
    ]
)
