import AppKit
import Foundation

private struct ShellCommandResult {
    let stdout: String
    let stderr: String
    let status: Int32
}

@MainActor
final class RelayController: ObservableObject {
    @Published var settings: RelayAppSettings
    @Published var lifecycle: RelayLifecycle = .stopped
    @Published var isDirty = false
    @Published var consoleEntries: [ConsoleEntry] = []
    @Published var requestSummaries: [RequestSummary] = []
    @Published var selectedRequestID: String?
    @Published var statusSnapshot: RelayStatusSnapshot?
    @Published var alertMessage: AlertMessage?
    @Published var managedProcessID: Int32?
    @Published var lastStatusRefresh = Date.distantPast

    private var relayProcess: Process?
    private var pendingRestart = false
    private var pollTask: Task<Void, Never>?
    private let session: URLSession

    init() {
        var migratedLaunchDefaults = false
        var migratedNetworkDefaults = false
        if let data = try? Data(contentsOf: AppPaths.settingsFile),
           var decoded = try? JSONDecoder().decode(RelayAppSettings.self, from: data) {
            migratedLaunchDefaults = decoded.migrateLaunchDefaultsIfStale()
            migratedNetworkDefaults = decoded.migrateNetworkDefaultsIfLegacy()
            settings = decoded
        } else {
            settings = RelayAppSettings.defaults()
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 2.0
        configuration.timeoutIntervalForResource = 2.0
        session = URLSession(configuration: configuration)
        appendConsole(source: "app", text: "AIRelays menu bar app ready.", isError: false)
        if migratedLaunchDefaults || migratedNetworkDefaults {
            try? persistSettings()
        }
        if migratedLaunchDefaults {
            appendConsole(
                source: "config",
                text: "Updated launch settings to the app's embedded relay runtime.",
                isError: false
            )
        }
        if migratedNetworkDefaults {
            appendConsole(
                source: "config",
                text: "Updated listener defaults: \(settings.host):\(settings.port). Adjust host/port in Settings if needed.",
                isError: false
            )
        }
        startPolling()
    }

    deinit {
        pollTask?.cancel()
    }

    var menuBarSymbolName: String {
        if isReachable {
            return "bolt.horizontal.circle.fill"
        }
        return lifecycle.symbolName
    }

    var isReachable: Bool {
        statusSnapshot != nil
    }

    var endpointLabel: String {
        settings.baseURL
    }

    var selectedRequestDetails: String {
        requestSummaries.first(where: { $0.id == selectedRequestID })?.details ?? "Select a request to inspect its phases."
    }

    func updateSettings(_ transform: (inout RelayAppSettings) -> Void) {
        var next = settings
        transform(&next)
        settings = next
        isDirty = true
    }

    func saveSettings() {
        do {
            try persistSettings()
            try writeRelayConfig()
            isDirty = false
            appendConsole(source: "config", text: "Saved app settings and relay config.", isError: false)
        } catch {
            presentError("Save failed", error)
        }
    }

    func saveAndRestartIfNeeded() {
        saveSettings()
        guard relayProcess != nil else {
            return
        }
        restartRelay()
    }

    func startRelay() {
        Task {
            do {
                try persistSettings()
                try writeRelayConfig()
                try launchRelayProcess()
                isDirty = false
            } catch {
                presentError("Start failed", error)
            }
        }
    }

    func stopRelay() {
        pendingRestart = false
        guard let relayProcess else {
            if isReachable {
                alertMessage = AlertMessage(
                    title: "Relay is reachable",
                    message: "The relay responds on \(settings.baseURL), but this app did not start the current process."
                )
            }
            return
        }
        lifecycle = .stopping
        appendConsole(source: "relay", text: "Stopping AIRelays...", isError: false)
        relayProcess.terminate()
        let processID = relayProcess.processIdentifier
        DispatchQueue.global().asyncAfter(deadline: .now() + 3.0) {
            if relayProcess.isRunning {
                kill(processID, SIGKILL)
            }
        }
    }

    func restartRelay() {
        if relayProcess == nil {
            startRelay()
            return
        }
        pendingRestart = true
        stopRelay()
    }

    /// Switches between the protected mode (relay token required) and the
    /// open no-auth mode, then applies the change to a running relay.
    func setAuthMode(requireToken: Bool) {
        guard settings.requireBearerAuth != requireToken else {
            return
        }
        updateSettings { $0.requireBearerAuth = requireToken }
        appendConsole(
            source: "config",
            text: requireToken
                ? "Auth mode: protected (relay token required)."
                : "Auth mode: open (--no-auth), no relay token needed.",
            isError: false
        )
        saveAndRestartIfNeeded()
    }

    /// Switches the listener between loopback-only and all interfaces
    /// (private network), then applies the change to a running relay.
    func setPrivateNetworkExposure(_ exposed: Bool) {
        guard settings.isLoopbackHost == exposed else {
            return
        }
        updateSettings { $0.host = exposed ? "0.0.0.0" : "127.0.0.1" }
        var note = exposed
            ? "Listener: all interfaces (private network can connect)."
            : "Listener: loopback only (this Mac)."
        if exposed && settings.enableClaudeExperimental {
            note += " Claude experimental is loopback-only and stays disabled while exposed."
        }
        appendConsole(source: "config", text: note, isError: false)
        saveAndRestartIfNeeded()
    }

    func rotateRelayToken() {
        Task {
            do {
                try persistSettings()
                try writeRelayConfig()
                let result = try await executeShellCommand(
                    label: "token-rotate",
                    shellCommand: settings.relayShellCommand(["token", "rotate", "--json", "--config", settings.expandedConfigPath])
                )
                let token = parseJSONValue(from: result.stdout, keyPath: ["relay_token"]) ?? "(token not returned)"
                alertMessage = AlertMessage(title: "Relay token rotated", message: token)
            } catch {
                presentError("Token rotation failed", error)
            }
        }
    }

    func revealRelayToken() {
        Task {
            do {
                try persistSettings()
                try writeRelayConfig()
                let result = try await executeShellCommand(
                    label: "token-show",
                    shellCommand: settings.relayShellCommand(["token", "show", "--json", "--config", settings.expandedConfigPath])
                )
                let token = parseJSONValue(from: result.stdout, keyPath: ["relay_token"]) ?? "No token configured."
                alertMessage = AlertMessage(title: "Relay token", message: token)
            } catch {
                presentError("Token reveal failed", error)
            }
        }
    }

    func setCustomRelayToken(_ token: String) {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            alertMessage = AlertMessage(title: "Custom token", message: "Enter a non-empty relay token.")
            return
        }
        do {
            let path = URL(fileURLWithPath: settings.expandedBearerTokenFile)
            try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: nil)
            try "\(trimmed)\n".write(to: path, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            appendConsole(source: "token", text: "Wrote custom relay token to \(path.path).", isError: false)
        } catch {
            presentError("Custom token write failed", error)
        }
    }

    func runDoctor(skipResponse: Bool = false) {
        var components = ["doctor", "--config", settings.expandedConfigPath]
        if skipResponse {
            components.append("--skip-response")
        }
        runDetachedShellCommand(label: "doctor", shellCommand: settings.relayShellCommand(components))
    }

    func runOpenAILogin() {
        runDetachedShellCommand(
            label: "openai-login",
            shellCommand: settings.relayShellCommand(["login", "--config", settings.expandedConfigPath])
        )
    }

    func runClaudeLogin() {
        let command = "cd \(shellEscape(settings.expandedWorkingDirectory)) && \(shellEscape(settings.expandedClaudeBin)) auth login --claudeai"
        runDetachedShellCommand(label: "claude-login", shellCommand: command)
    }

    func runClaudeSetupToken() {
        let command = "cd \(shellEscape(settings.expandedWorkingDirectory)) && \(shellEscape(settings.expandedClaudeBin)) setup-token"
        runDetachedShellCommand(label: "claude-setup-token", shellCommand: command)
    }

    func openLogsFolder() {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: settings.expandedLogsDir)])
    }

    func openConfigFile() {
        let url = URL(fileURLWithPath: settings.expandedConfigPath)
        if !FileManager.default.fileExists(atPath: url.path) {
            do {
                try writeRelayConfig()
            } catch {
                presentError("Open config failed", error)
                return
            }
        }
        NSWorkspace.shared.open(url)
    }

    func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                await refreshRelayStatus()
                refreshTrafficLogs()
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    private func persistSettings() throws {
        let data = try JSONEncoder().encode(settings)
        try FileManager.default.createDirectory(at: AppPaths.supportDirectory, withIntermediateDirectories: true, attributes: nil)
        try data.write(to: AppPaths.settingsFile, options: .atomic)
    }

    private func writeRelayConfig() throws {
        let path = URL(fileURLWithPath: settings.expandedConfigPath)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: nil)
        try settings.renderConfigTOML().write(to: path, atomically: true, encoding: .utf8)
    }

    private func launchRelayProcess() throws {
        guard relayProcess == nil else {
            appendConsole(source: "relay", text: "AIRelays is already managed by the app.", isError: false)
            return
        }
        let process = Process()
        let stdout = Pipe()
        let stderr = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = [
            "-lc",
            settings.relayShellCommand(
                ["serve", "--config", settings.expandedConfigPath],
                rawSuffix: settings.extraServeArgs
            ),
        ]
        process.environment = mergedEnvironment()
        process.currentDirectoryURL = URL(fileURLWithPath: settings.expandedWorkingDirectory)
        process.standardOutput = stdout
        process.standardError = stderr
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                return
            }
            Task { @MainActor in
                self?.appendConsole(source: "relay", text: text, isError: false)
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                return
            }
            Task { @MainActor in
                self?.appendConsole(source: "relay", text: text, isError: true)
            }
        }
        process.terminationHandler = { [weak self] terminatedProcess in
            Task { @MainActor in
                stdout.fileHandleForReading.readabilityHandler = nil
                stderr.fileHandleForReading.readabilityHandler = nil
                self?.relayProcess = nil
                self?.managedProcessID = nil
                let code = terminatedProcess.terminationStatus
                let wasRestarting = self?.pendingRestart == true
                self?.pendingRestart = false
                self?.lifecycle = code == 0 || wasRestarting ? .stopped : .failed
                self?.appendConsole(
                    source: "relay",
                    text: "AIRelays process exited with status \(code).",
                    isError: code != 0 && !wasRestarting
                )
                if wasRestarting {
                    self?.startRelay()
                }
            }
        }
        lifecycle = .starting
        try process.run()
        relayProcess = process
        managedProcessID = process.processIdentifier
        appendConsole(source: "relay", text: "Started AIRelays with PID \(process.processIdentifier).", isError: false)
    }

    private func mergedEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        // The embedded runtime lives inside the signed app bundle; writing
        // .pyc files at run time would invalidate the code signature.
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for (key, value) in settings.parsedEnvironmentOverrides() {
            environment[key] = value
        }
        return environment
    }

    private func refreshTrafficLogs() {
        let logsURL = URL(fileURLWithPath: settings.expandedLogsDir)
        guard let enumerator = FileManager.default.enumerator(at: logsURL, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles]) else {
            requestSummaries = []
            return
        }
        var files: [URL] = []
        for case let url as URL in enumerator where url.pathExtension == "log" {
            files.append(url)
        }
        let sortedFiles = files.sorted { lhs, rhs in
            let lhsDate = (try? lhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            let rhsDate = (try? rhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
            return lhsDate > rhsDate
        }
        let recentFiles = Array(sortedFiles.prefix(3))
        var records: [TrafficRecord] = []
        for file in recentFiles {
            guard let content = try? String(contentsOf: file, encoding: .utf8) else {
                continue
            }
            let lines = content.split(whereSeparator: \.isNewline).suffix(250)
            for (index, line) in lines.enumerated() {
                guard let record = parseTrafficRecord(line: String(line), file: file.path, index: index) else {
                    continue
                }
                records.append(record)
            }
        }
        records.sort { $0.timestamp > $1.timestamp }
        requestSummaries = summarizeTraffic(records: records)
        if let selectedRequestID, !requestSummaries.contains(where: { $0.id == selectedRequestID }) {
            self.selectedRequestID = requestSummaries.first?.id
        } else if self.selectedRequestID == nil {
            self.selectedRequestID = requestSummaries.first?.id
        }
    }

    private func parseTrafficRecord(line: String, file: String, index: Int) -> TrafficRecord? {
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return nil
        }
        let timestampText = object["logged_at"] as? String ?? object["timestamp"] as? String
        let timestamp = timestampText.flatMap(parseTrafficTimestamp) ?? Date.distantPast
        let phase = object["phase"] as? String ?? "unknown"
        let requestID = object["request_id"] as? String
        let method = object["method"] as? String
        let path = object["path"] as? String
        let statusCode = object["status_code"] as? Int
        let provider = object["provider"] as? String
        let model = object["model"] as? String
        return TrafficRecord(
            id: "\(file)#\(index)",
            requestID: requestID,
            timestamp: timestamp,
            phase: phase,
            method: method,
            path: path,
            statusCode: statusCode,
            provider: provider,
            model: model,
            rawText: prettyJSON(object)
        )
    }

    private func summarizeTraffic(records: [TrafficRecord]) -> [RequestSummary] {
        struct Accumulator {
            var lastSeenAt = Date.distantPast
            var method = "?"
            var path = "/"
            var provider = "-"
            var model = "-"
            var statusCode: Int?
            var lastPhase = "unknown"
            var eventCount = 0
            var detailLines: [String] = []
        }
        var grouped: [String: Accumulator] = [:]
        for record in records {
            guard let requestID = record.requestID, requestID != "startup" else {
                continue
            }
            var accumulator = grouped[requestID] ?? Accumulator()
            if record.timestamp > accumulator.lastSeenAt {
                accumulator.lastSeenAt = record.timestamp
            }
            if let method = record.method {
                accumulator.method = method
            }
            if let path = record.path {
                accumulator.path = path
            }
            if let provider = record.provider {
                accumulator.provider = provider
            }
            if let model = record.model {
                accumulator.model = model
            }
            if let statusCode = record.statusCode {
                accumulator.statusCode = statusCode
            }
            accumulator.lastPhase = record.phase
            accumulator.eventCount += 1
            accumulator.detailLines.append("[\(record.timestampLabel)] \(record.summaryLabel)\n\(record.rawText)")
            grouped[requestID] = accumulator
        }
        return grouped.map { requestID, accumulator in
            RequestSummary(
                id: requestID,
                lastSeenAt: accumulator.lastSeenAt,
                method: accumulator.method,
                path: accumulator.path,
                provider: accumulator.provider,
                model: accumulator.model,
                statusCode: accumulator.statusCode,
                lastPhase: accumulator.lastPhase,
                eventCount: accumulator.eventCount,
                details: accumulator.detailLines.joined(separator: "\n\n")
            )
        }
        .sorted { $0.lastSeenAt > $1.lastSeenAt }
    }

    private func refreshRelayStatus() async {
        guard let url = URL(string: "\(settings.baseURL)/relay/status") else {
            return
        }
        var request = URLRequest(url: url)
        if settings.requireBearerAuth,
           let token = try? String(contentsOfFile: settings.expandedBearerTokenFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        do {
            let (data, response) = try await session.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                statusSnapshot = nil
                if lifecycle == .running {
                    lifecycle = .starting
                }
                return
            }
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                statusSnapshot = nil
                return
            }
            statusSnapshot = parseStatusSnapshot(object)
            lifecycle = .running
            lastStatusRefresh = Date()
        } catch {
            statusSnapshot = nil
            if relayProcess == nil {
                lifecycle = .stopped
            }
        }
    }

    private func parseStatusSnapshot(_ object: [String: Any]) -> RelayStatusSnapshot {
        let ready = object["ready"] as? [String: Any] ?? [:]
        let relay = object["relay"] as? [String: Any] ?? [:]
        let providersObject = object["providers"] as? [String: Any] ?? [:]
        let providers = providersObject.keys.sorted().map { name in
            let entry = providersObject[name] as? [String: Any] ?? [:]
            let enabled = entry["enabled"] as? Bool ?? false
            let readyForRequests = entry["ready_for_requests"] as? Bool ?? false
            var detailParts: [String] = []
            if let email = entry["email"] as? String, !email.isEmpty {
                detailParts.append(email)
            }
            if let cliVersion = entry["cli_version"] as? String, !cliVersion.isEmpty {
                detailParts.append(cliVersion)
            }
            if let plan = entry["plan_type"] as? String, !plan.isEmpty {
                detailParts.append(plan)
            }
            if detailParts.isEmpty {
                detailParts.append(enabled ? "configured" : "disabled")
            }
            return ProviderSnapshot(
                id: name,
                enabled: enabled,
                ready: readyForRequests,
                detail: detailParts.joined(separator: " | ")
            )
        }
        return RelayStatusSnapshot(
            version: object["version"] as? String ?? "?",
            requireBearerAuth: relay["require_bearer_auth"] as? Bool ?? settings.requireBearerAuth,
            relayTokenReady: ready["relay_token"] as? Bool ?? false,
            anyProviderReady: ready["any_provider"] as? Bool ?? false,
            baseURL: relay["client_base_url"] as? String ?? settings.baseURL,
            rawStatusJSON: prettyJSON(object),
            providers: providers
        )
    }

    private func runDetachedShellCommand(label: String, shellCommand: String) {
        Task {
            do {
                try persistSettings()
                try writeRelayConfig()
                _ = try await executeShellCommand(label: label, shellCommand: shellCommand)
            } catch {
                presentError("\(label) failed", error)
            }
        }
    }

    private func executeShellCommand(label: String, shellCommand: String) async throws -> ShellCommandResult {
        appendConsole(source: label, text: shellCommand, isError: false)
        let result = try await Task.detached(priority: .userInitiated) { [environment = mergedEnvironment(), workingDirectory = settings.expandedWorkingDirectory] in
            let process = Process()
            let stdout = Pipe()
            let stderr = Pipe()
            process.executableURL = URL(fileURLWithPath: "/bin/zsh")
            process.arguments = ["-lc", shellCommand]
            process.environment = environment
            process.currentDirectoryURL = URL(fileURLWithPath: workingDirectory)
            process.standardOutput = stdout
            process.standardError = stderr
            try process.run()
            process.waitUntilExit()
            let stdoutData = stdout.fileHandleForReading.readDataToEndOfFile()
            let stderrData = stderr.fileHandleForReading.readDataToEndOfFile()
            let stdoutText = String(data: stdoutData, encoding: .utf8) ?? ""
            let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
            return ShellCommandResult(stdout: stdoutText, stderr: stderrText, status: process.terminationStatus)
        }.value
        appendConsole(source: label, text: result.stdout, isError: false)
        appendConsole(source: label, text: result.stderr, isError: result.status != 0)
        if result.status != 0 {
            throw NSError(domain: "AIRelaysMenuBar", code: Int(result.status), userInfo: [
                NSLocalizedDescriptionKey: result.stderr.isEmpty ? "Command failed with status \(result.status)." : result.stderr
            ])
        }
        return result
    }

    private func appendConsole(source: String, text: String, isError: Bool) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }
        let lines = trimmed.split(whereSeparator: \.isNewline)
        for line in lines {
            consoleEntries.append(
                ConsoleEntry(
                    timestamp: Date(),
                    source: source,
                    text: String(line),
                    isError: isError
                )
            )
        }
        if consoleEntries.count > 300 {
            consoleEntries.removeFirst(consoleEntries.count - 300)
        }
    }

    private func parseJSONValue(from text: String, keyPath: [String]) -> String? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return nil
        }
        var current: Any = object
        for key in keyPath {
            guard let dict = current as? [String: Any], let next = dict[key] else {
                return nil
            }
            current = next
        }
        if let string = current as? String {
            return string
        }
        return nil
    }

    private func presentError(_ title: String, _ error: Error) {
        lifecycle = relayProcess == nil ? .failed : lifecycle
        appendConsole(source: "error", text: error.localizedDescription, isError: true)
        alertMessage = AlertMessage(
            title: title,
            message: conciseErrorSummary(error.localizedDescription)
        )
    }

    /// Reduces multi-line tool output (e.g. Python tracebacks) to the line
    /// that states the actual failure, keeping alerts readable. The full
    /// output is always available in the console.
    private func conciseErrorSummary(_ text: String) -> String {
        let meaningfulLines = text
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { line in
                !line.isEmpty && !line.allSatisfy { "^~".contains($0) }
            }
        // In tracebacks and most CLI failures the final line carries the
        // actual error message.
        guard var summary = meaningfulLines.last else {
            return "Unknown error. See the Console tab for details."
        }
        if summary.count > 300 {
            summary = String(summary.prefix(300)) + "…"
        }
        if meaningfulLines.count > 1 {
            summary += "\n\nFull output is in the Console tab."
        }
        return summary
    }

    func clearConsole() {
        consoleEntries.removeAll()
    }
}
