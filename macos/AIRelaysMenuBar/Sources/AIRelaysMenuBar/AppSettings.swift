import Foundation

func shellEscape(_ value: String) -> String {
    "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

func prettyJSON(_ object: Any) -> String {
    guard JSONSerialization.isValidJSONObject(object),
          let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]),
          let text = String(data: data, encoding: .utf8)
    else {
        return String(describing: object)
    }
    return text
}

private func iso8601DateFormatter() -> ISO8601DateFormatter {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}

private func simpleTimeFormatter() -> DateFormatter {
    let formatter = DateFormatter()
    formatter.dateStyle = .none
    formatter.timeStyle = .medium
    formatter.doesRelativeDateFormatting = false
    return formatter
}

func parseTrafficTimestamp(_ text: String) -> Date? {
    iso8601DateFormatter().date(from: text)
}

func requestTimeString(_ date: Date) -> String {
    simpleTimeFormatter().string(from: date)
}

func consoleTimeString(_ date: Date) -> String {
    simpleTimeFormatter().string(from: date)
}

enum RelayLifecycle: String {
    case stopped
    case starting
    case running
    case stopping
    case failed

    var label: String {
        switch self {
        case .stopped:
            return "Stopped"
        case .starting:
            return "Starting"
        case .running:
            return "Running"
        case .stopping:
            return "Stopping"
        case .failed:
            return "Failed"
        }
    }

    var symbolName: String {
        switch self {
        case .stopped:
            return "bolt.slash.circle"
        case .starting:
            return "arrow.triangle.2.circlepath.circle"
        case .running:
            return "bolt.horizontal.circle.fill"
        case .stopping:
            return "stop.circle"
        case .failed:
            return "exclamationmark.triangle.fill"
        }
    }
}

struct AlertMessage: Identifiable {
    let id = UUID()
    let title: String
    let message: String
}

struct ConsoleEntry: Identifiable {
    let id = UUID()
    let timestamp: Date
    let source: String
    let text: String
    let isError: Bool

    var timestampLabel: String {
        consoleTimeString(timestamp)
    }
}

struct TrafficRecord: Identifiable {
    let id: String
    let requestID: String?
    let timestamp: Date
    let phase: String
    let method: String?
    let path: String?
    let statusCode: Int?
    let provider: String?
    let model: String?
    let rawText: String

    var timestampLabel: String {
        requestTimeString(timestamp)
    }

    var summaryLabel: String {
        var parts: [String] = [phase]
        if let method, let path {
            parts.append("\(method) \(path)")
        }
        if let statusCode {
            parts.append("status \(statusCode)")
        }
        if let provider {
            parts.append(provider)
        }
        if let model {
            parts.append(model)
        }
        return parts.joined(separator: " | ")
    }
}

struct RequestSummary: Identifiable {
    let id: String
    let lastSeenAt: Date
    let method: String
    let path: String
    let provider: String
    let model: String
    let statusCode: Int?
    let lastPhase: String
    let eventCount: Int
    let details: String

    var timestampLabel: String {
        requestTimeString(lastSeenAt)
    }

    var routeLabel: String {
        "\(method) \(path)"
    }
}

struct ProviderSnapshot: Identifiable {
    let id: String
    let enabled: Bool
    let ready: Bool
    let detail: String
}

struct RelayStatusSnapshot {
    let version: String
    let requireBearerAuth: Bool
    let relayTokenReady: Bool
    let anyProviderReady: Bool
    let baseURL: String
    let rawStatusJSON: String
    let providers: [ProviderSnapshot]
}

struct RelayAppSettings: Codable {
    var workingDirectory: String
    var relayCommand: String
    var configPath: String
    var upstreamBaseURL: String
    var issuerBaseURL: String
    var clientID: String
    var clientVersion: String
    var requestTimeoutSeconds: Double
    var host: String
    var port: Int
    var authStorageMode: String
    var dataDir: String
    var logsDir: String
    var browserOpen: Bool
    var loginTimeoutSeconds: Double
    var requireBearerAuth: Bool
    var autoGenerateBearerToken: Bool
    var bearerTokenFile: String
    var rateLimitPerMinute: Int
    var rateLimitBurst: Int
    var concurrentRequestsPerIP: Int
    var failedAuthWindowSeconds: Int
    var failedAuthMaxAttempts: Int
    var failedAuthBlockSeconds: Int
    var trustXForwardedFor: Bool
    var maxUploadBytes: Int
    var maxTotalUploadBytes: Int
    var enableOpenAIProvider: Bool
    var modelsCacheTTLSeconds: Double
    var enableClaudeExperimental: Bool
    var claudeBin: String
    var claudeTimeoutSeconds: Double
    var claudeMaxConcurrentRequests: Int
    var claudeStripAPIKeyEnv: Bool
    var claudeModelsCSV: String
    var extraServeArgs: String
    var extraEnvironment: String
    // Optional so settings from earlier app versions still decode; nil marks
    // a legacy file whose network default predates private-network exposure.
    var networkDefaultsVersion: Int?

    static func defaults() -> RelayAppSettings {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let launch = defaultLaunchConfiguration()
        return RelayAppSettings(
            workingDirectory: launch.workingDirectory,
            relayCommand: launch.relayCommand,
            configPath: "\(home)/.config/airelays/config.toml",
            upstreamBaseURL: "https://chatgpt.com/backend-api/codex",
            issuerBaseURL: "https://auth.openai.com",
            clientID: "app_EMoamEEZ73f0CkXaXp7hrann",
            clientVersion: "0.124.0",
            requestTimeoutSeconds: 120.0,
            // Listen on all interfaces so devices on the private network can
            // reach the relay out of the box. Loopback-only remains one click
            // away in the app.
            host: "0.0.0.0",
            port: defaultRelayPort,
            authStorageMode: "auto",
            dataDir: "\(home)/.airelays",
            logsDir: "\(home)/.airelays/logs",
            browserOpen: false,
            loginTimeoutSeconds: 900.0,
            requireBearerAuth: true,
            autoGenerateBearerToken: false,
            bearerTokenFile: "\(home)/.airelays/relay-token",
            rateLimitPerMinute: 120,
            rateLimitBurst: 40,
            concurrentRequestsPerIP: 8,
            failedAuthWindowSeconds: 300,
            failedAuthMaxAttempts: 8,
            failedAuthBlockSeconds: 900,
            trustXForwardedFor: false,
            maxUploadBytes: 32 * 1024 * 1024,
            maxTotalUploadBytes: 256 * 1024 * 1024,
            enableOpenAIProvider: true,
            modelsCacheTTLSeconds: 300.0,
            enableClaudeExperimental: true,
            claudeBin: "claude",
            claudeTimeoutSeconds: 600.0,
            claudeMaxConcurrentRequests: 2,
            claudeStripAPIKeyEnv: true,
            claudeModelsCSV: "claude:sonnet, claude:opus, claude:haiku, claude:fable",
            extraServeArgs: "",
            extraEnvironment: "",
            networkDefaultsVersion: 1
        )
    }

    /// Upgrades network settings that still carry an older app version's
    /// defaults. Each step runs once (tracked by `networkDefaultsVersion`),
    /// so explicit choices made afterwards are preserved.
    ///
    /// - v1: loopback-only default became all interfaces (private network).
    /// - v2: default port moved off collision-prone 8080.
    mutating func migrateNetworkDefaultsIfLegacy() -> Bool {
        let version = networkDefaultsVersion ?? 0
        let targetVersion = 2
        guard version < targetVersion else {
            return false
        }
        if version < 1, host == "127.0.0.1" {
            host = "0.0.0.0"
        }
        if version < 2, port == 8080 {
            port = defaultRelayPort
        }
        networkDefaultsVersion = targetVersion
        return true
    }

    /// Refreshes launch settings that were auto-derived by earlier app
    /// versions (development-checkout paths) or that no longer resolve to an
    /// existing executable. User-customized commands are left untouched.
    /// Returns true when something was migrated.
    mutating func migrateLaunchDefaultsIfStale() -> Bool {
        let launch = defaultLaunchConfiguration()
        guard relayCommand != launch.relayCommand else {
            return false
        }
        let commandExecutable = leadingExecutablePath(of: relayCommand)
        let executableMissing = commandExecutable.map {
            !FileManager.default.isExecutableFile(atPath: $0)
        } ?? false
        let isAutoDerived = relayCommand == "airelays"
            || commandExecutable?.hasSuffix("/.venv/bin/airelays") == true
        guard executableMissing || (isAutoDerived && embeddedRelayCommand() != nil) else {
            return false
        }
        relayCommand = launch.relayCommand
        workingDirectory = launch.workingDirectory
        return true
    }

    var expandedWorkingDirectory: String {
        NSString(string: workingDirectory).expandingTildeInPath
    }

    var expandedConfigPath: String {
        NSString(string: configPath).expandingTildeInPath
    }

    var expandedDataDir: String {
        NSString(string: dataDir).expandingTildeInPath
    }

    var expandedLogsDir: String {
        NSString(string: logsDir).expandingTildeInPath
    }

    var expandedBearerTokenFile: String {
        NSString(string: bearerTokenFile).expandingTildeInPath
    }

    var expandedClaudeBin: String {
        NSString(string: claudeBin).expandingTildeInPath
    }

    var isLoopbackHost: Bool {
        ["127.0.0.1", "localhost", "::1"].contains(host)
    }

    /// Host the app itself should dial: wildcard bind addresses are not
    /// dialable, so local clients go through loopback.
    var clientHost: String {
        ["0.0.0.0", "::"].contains(host) ? "127.0.0.1" : host
    }

    var baseURL: String {
        "http://\(clientHost):\(port)/v1"
    }

    /// The relay enforces loopback-only listeners for the experimental
    /// Claude runtime, so it must be disabled in the rendered config when
    /// the listener is exposed beyond loopback.
    var claudeEffectivelyEnabled: Bool {
        enableClaudeExperimental && isLoopbackHost
    }

    var claudeModels: [String] {
        claudeModelsCSV
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    func renderConfigTOML() -> String {
        let quotedModels = claudeModels.map { "\"\($0)\"" }.joined(separator: ", ")
        return """
[server]
host = "\(host)"
port = \(port)

[paths]
data_dir = "\(expandedDataDir)"
logs_dir = "\(expandedLogsDir)"

[auth]
storage = "\(authStorageMode)"
browser_open = \(String(browserOpen).lowercased())
login_timeout_seconds = \(Int(loginTimeoutSeconds))

[upstream]
base_url = "\(upstreamBaseURL)"
issuer_base_url = "\(issuerBaseURL)"
client_id = "\(clientID)"
client_version = "\(clientVersion)"
request_timeout_seconds = \(requestTimeoutSeconds)

[security]
require_bearer_auth = \(String(requireBearerAuth).lowercased())
bearer_token_file = "\(expandedBearerTokenFile)"
auto_generate_bearer_token = \(String(autoGenerateBearerToken).lowercased())
rate_limit_per_minute = \(rateLimitPerMinute)
rate_limit_burst = \(rateLimitBurst)
concurrent_requests_per_ip = \(concurrentRequestsPerIP)
failed_auth_window_seconds = \(failedAuthWindowSeconds)
failed_auth_max_attempts = \(failedAuthMaxAttempts)
failed_auth_block_seconds = \(failedAuthBlockSeconds)
trust_x_forwarded_for = \(String(trustXForwardedFor).lowercased())

[uploads]
max_upload_bytes = \(maxUploadBytes)
max_total_upload_bytes = \(maxTotalUploadBytes)

[providers.openai]
enabled = \(String(enableOpenAIProvider).lowercased())
models_cache_ttl_seconds = \(modelsCacheTTLSeconds)

[providers.claude]
enabled = \(String(claudeEffectivelyEnabled).lowercased())
bin = "\(expandedClaudeBin)"
timeout_seconds = \(claudeTimeoutSeconds)
max_concurrent_requests = \(claudeMaxConcurrentRequests)
strip_api_key_env = \(String(claudeStripAPIKeyEnv).lowercased())
models = [\(quotedModels)]
"""
    }

    func parsedEnvironmentOverrides() -> [String: String] {
        var overrides: [String: String] = [:]
        for line in extraEnvironment.split(whereSeparator: \.isNewline) {
            let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty || text.hasPrefix("#") {
                continue
            }
            let parts = text.split(separator: "=", maxSplits: 1)
            if parts.count == 2 {
                overrides[String(parts[0]).trimmingCharacters(in: .whitespaces)] =
                    String(parts[1]).trimmingCharacters(in: .whitespaces)
            }
        }
        return overrides
    }

    func relayShellCommand(_ components: [String], rawSuffix: String = "") -> String {
        var parts = [relayCommand]
        parts.append(contentsOf: components.map(shellEscape))
        let command = parts.joined(separator: " ")
        var finalCommand = "cd \(shellEscape(expandedWorkingDirectory)) && \(command)"
        let trimmedSuffix = rawSuffix.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedSuffix.isEmpty {
            finalCommand += " \(trimmedSuffix)"
        }
        return finalCommand
    }
}

/// Default relay port. 8080 collided constantly with dev servers and other
/// tools; 8317 is IANA-unregistered and not a common default of anything.
let defaultRelayPort = 8317

struct LaunchConfiguration {
    let workingDirectory: String
    let relayCommand: String
}

/// Extracts the executable path from the first token of a shell command,
/// honoring the single-quoting produced by `shellEscape`.
func leadingExecutablePath(of command: String) -> String? {
    let trimmed = command.trimmingCharacters(in: .whitespaces)
    guard !trimmed.isEmpty else {
        return nil
    }
    if trimmed.hasPrefix("'") {
        let rest = trimmed.dropFirst()
        guard let closing = rest.firstIndex(of: "'") else {
            return nil
        }
        return String(rest[..<closing])
    }
    let token = trimmed.split(separator: " ", maxSplits: 1)[0]
    // Bare command names (resolved via PATH) are not filesystem paths.
    return token.contains("/") ? String(token) : nil
}

/// Path of the Python runtime embedded in the app bundle, when present.
///
/// The packaged app ships a standalone CPython with `airelays` installed
/// under `Contents/Resources/runtime`, so the app is self-contained and
/// never depends on a development checkout.
func embeddedRuntimePython() -> String? {
    guard let resourceURL = Bundle.main.resourceURL else {
        return nil
    }
    let python = resourceURL.appendingPathComponent("runtime/bin/python3").path
    return FileManager.default.isExecutableFile(atPath: python) ? python : nil
}

func embeddedRelayCommand() -> String? {
    // The interpreter path is resolved from the live bundle location on
    // every launch, so the command stays valid if the app is moved.
    embeddedRuntimePython().map { "\(shellEscape($0)) -m airelays" }
}

/// Development fallback: when running via `swift run` from a checkout,
/// use the repository's virtual environment.
func locateRepositoryRoot() -> String? {
    let candidates = [
        FileManager.default.currentDirectoryPath,
        URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().path,
    ]
    for candidate in candidates {
        var url = URL(fileURLWithPath: candidate)
        for _ in 0..<8 {
            let marker = url.appendingPathComponent("src/airelay/cli.py").path
            if FileManager.default.fileExists(atPath: marker) {
                return url.path
            }
            let parent = url.deletingLastPathComponent()
            if parent.path == url.path {
                break
            }
            url = parent
        }
    }
    return nil
}

/// Resolution order: embedded runtime (packaged app), then a development
/// checkout's virtual environment (`swift run`), then `airelays` on PATH.
func defaultLaunchConfiguration() -> LaunchConfiguration {
    if let embedded = embeddedRelayCommand() {
        return LaunchConfiguration(
            workingDirectory: AppPaths.supportDirectory.path,
            relayCommand: embedded
        )
    }
    if let root = locateRepositoryRoot() {
        let venvBinary = "\(root)/.venv/bin/airelays"
        if FileManager.default.isExecutableFile(atPath: venvBinary) {
            return LaunchConfiguration(workingDirectory: root, relayCommand: shellEscape(venvBinary))
        }
        return LaunchConfiguration(workingDirectory: root, relayCommand: "airelays")
    }
    return LaunchConfiguration(
        workingDirectory: AppPaths.supportDirectory.path,
        relayCommand: "airelays"
    )
}

enum AppPaths {
    static var supportDirectory: URL {
        let applicationSupport = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support", isDirectory: true)
        let base = applicationSupport.appendingPathComponent("AIRelays", isDirectory: true)
        // One-time migration from the app's earlier "AIRelaysMenuBar" name.
        let legacy = applicationSupport.appendingPathComponent("AIRelaysMenuBar", isDirectory: true)
        if !FileManager.default.fileExists(atPath: base.path),
           FileManager.default.fileExists(atPath: legacy.path) {
            try? FileManager.default.moveItem(at: legacy, to: base)
        }
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true, attributes: nil)
        return base
    }

    static var settingsFile: URL {
        supportDirectory.appendingPathComponent("app-settings.json")
    }
}
