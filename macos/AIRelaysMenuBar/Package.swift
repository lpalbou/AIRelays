// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AIRelaysMenuBar",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .executable(name: "AIRelaysMenuBar", targets: ["AIRelaysMenuBar"]),
    ],
    targets: [
        .executableTarget(
            name: "AIRelaysMenuBar",
            path: "Sources/AIRelaysMenuBar",
            resources: [
                .process("Resources"),
            ]
        ),
    ]
)
