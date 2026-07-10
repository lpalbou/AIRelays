import SwiftUI

/// Full relay configuration editor. This mirrors the config surface that
/// `airelays` reads from `config.toml`, plus app-level launch options.
struct SettingsRootView: View {
    @ObservedObject var controller: RelayController

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("AIRelays Settings")
                            .font(.title2.weight(.semibold))
                        Text("Edits the same relay config you would normally drive from the shell.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Save") {
                        controller.saveSettings()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!controller.isDirty)
                    Button("Save && Restart") {
                        controller.saveAndRestartIfNeeded()
                    }
                    .disabled(!controller.isDirty)
                }
                settingsSections
            }
            .padding(20)
        }
        .frame(minWidth: 900, minHeight: 700)
    }

    private var settingsSections: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionCard(title: "Launch", caption: "How the app runs the relay. Defaults use the runtime embedded in the app.") {
                settingsGrid {
                    textField("Working Directory", keyPath: \.workingDirectory)
                    textField("Relay Command", keyPath: \.relayCommand)
                    textField("Config Path", keyPath: \.configPath)
                    textField("Data Dir", keyPath: \.dataDir)
                    textField("Logs Dir", keyPath: \.logsDir)
                    textField("Bearer Token File", keyPath: \.bearerTokenFile)
                }
            }
            SectionCard(title: "Listener & Auth") {
                settingsGrid {
                    textField("Host", keyPath: \.host)
                    integerField("Port", keyPath: \.port)
                    booleanField("Require Relay Token", keyPath: \.requireBearerAuth)
                    booleanField("Auto Generate Token", keyPath: \.autoGenerateBearerToken)
                    textField("Auth Storage", keyPath: \.authStorageMode)
                    booleanField("Browser Open", keyPath: \.browserOpen)
                    doubleField("Login Timeout Seconds", keyPath: \.loginTimeoutSeconds)
                }
            }
            SectionCard(title: "Upstream") {
                settingsGrid {
                    textField("Upstream Base URL", keyPath: \.upstreamBaseURL)
                    textField("Issuer Base URL", keyPath: \.issuerBaseURL)
                    textField("Client ID", keyPath: \.clientID)
                    textField("Client Version", keyPath: \.clientVersion)
                    doubleField("Request Timeout Seconds", keyPath: \.requestTimeoutSeconds)
                }
            }
            SectionCard(title: "Security Limits") {
                settingsGrid {
                    integerField("Rate Limit Per Minute", keyPath: \.rateLimitPerMinute)
                    integerField("Rate Limit Burst", keyPath: \.rateLimitBurst)
                    integerField("Concurrent Requests / IP", keyPath: \.concurrentRequestsPerIP)
                    integerField("Failed Auth Window Seconds", keyPath: \.failedAuthWindowSeconds)
                    integerField("Failed Auth Max Attempts", keyPath: \.failedAuthMaxAttempts)
                    integerField("Failed Auth Block Seconds", keyPath: \.failedAuthBlockSeconds)
                    booleanField("Trust X-Forwarded-For", keyPath: \.trustXForwardedFor)
                    integerField("Max Upload Bytes", keyPath: \.maxUploadBytes)
                    integerField("Max Total Upload Bytes", keyPath: \.maxTotalUploadBytes)
                }
            }
            SectionCard(title: "Providers") {
                settingsGrid {
                    booleanField("Enable OpenAI", keyPath: \.enableOpenAIProvider)
                    doubleField("OpenAI Models Cache TTL", keyPath: \.modelsCacheTTLSeconds)
                    booleanField("Enable Claude", keyPath: \.enableClaudeExperimental)
                    textField("Claude Bin", keyPath: \.claudeBin)
                    doubleField("Claude Timeout Seconds", keyPath: \.claudeTimeoutSeconds)
                    integerField("Claude Max Concurrent", keyPath: \.claudeMaxConcurrentRequests)
                    booleanField("Claude Strip API Key Env", keyPath: \.claudeStripAPIKeyEnv)
                    textField("Claude Models CSV", keyPath: \.claudeModelsCSV)
                }
            }
            SectionCard(title: "Shell Parity") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Extra Environment")
                        .font(.headline)
                    TextEditor(text: stringBinding(\.extraEnvironment))
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 100)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
                    Text("Extra Serve Args")
                        .font(.headline)
                    TextEditor(text: stringBinding(\.extraServeArgs))
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 60)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
                }
            }
        }
    }

    private func settingsGrid<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        LazyVGrid(columns: [
            GridItem(.flexible(minimum: 240)),
            GridItem(.flexible(minimum: 240)),
        ], alignment: .leading, spacing: 12) {
            content()
        }
    }

    private func textField(_ label: String, keyPath: WritableKeyPath<RelayAppSettings, String>) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField(label, text: stringBinding(keyPath))
                .textFieldStyle(.roundedBorder)
        }
    }

    private func integerField(_ label: String, keyPath: WritableKeyPath<RelayAppSettings, Int>) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField(label, value: intBinding(keyPath), format: .number)
                .textFieldStyle(.roundedBorder)
        }
    }

    private func doubleField(_ label: String, keyPath: WritableKeyPath<RelayAppSettings, Double>) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField(label, value: doubleBinding(keyPath), format: .number)
                .textFieldStyle(.roundedBorder)
        }
    }

    private func booleanField(_ label: String, keyPath: WritableKeyPath<RelayAppSettings, Bool>) -> some View {
        Toggle(label, isOn: boolBinding(keyPath))
    }

    private func stringBinding(_ keyPath: WritableKeyPath<RelayAppSettings, String>) -> Binding<String> {
        Binding(
            get: { controller.settings[keyPath: keyPath] },
            set: { value in
                controller.updateSettings { $0[keyPath: keyPath] = value }
            }
        )
    }

    private func intBinding(_ keyPath: WritableKeyPath<RelayAppSettings, Int>) -> Binding<Int> {
        Binding(
            get: { controller.settings[keyPath: keyPath] },
            set: { value in
                controller.updateSettings { $0[keyPath: keyPath] = value }
            }
        )
    }

    private func doubleBinding(_ keyPath: WritableKeyPath<RelayAppSettings, Double>) -> Binding<Double> {
        Binding(
            get: { controller.settings[keyPath: keyPath] },
            set: { value in
                controller.updateSettings { $0[keyPath: keyPath] = value }
            }
        )
    }

    private func boolBinding(_ keyPath: WritableKeyPath<RelayAppSettings, Bool>) -> Binding<Bool> {
        Binding(
            get: { controller.settings[keyPath: keyPath] },
            set: { value in
                controller.updateSettings { $0[keyPath: keyPath] = value }
            }
        )
    }
}
