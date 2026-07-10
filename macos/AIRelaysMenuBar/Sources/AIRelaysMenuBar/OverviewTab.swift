import SwiftUI

/// Landing tab: relay state, endpoints, access modes, providers, and token.
struct OverviewTab: View {
    @ObservedObject var controller: RelayController
    @Environment(\.openSettings) private var openSettings
    @State private var customToken = ""
    @State private var showingCustomTokenSheet = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                headerCard
                HStack(alignment: .top, spacing: 14) {
                    accessCard
                    tokenCard
                }
                providersCard
            }
            .padding(16)
        }
        .sheet(isPresented: $showingCustomTokenSheet) {
            customTokenSheet
        }
    }

    // MARK: - Header

    private var headerCard: some View {
        SectionCard(title: "Relay") {
            HStack(spacing: 12) {
                StatusDot(kind: badgeKind(for: controller.lifecycle))
                VStack(alignment: .leading, spacing: 3) {
                    Text(controller.lifecycle.label)
                        .font(.title3.weight(.semibold))
                    endpointLine(label: "Local", url: controller.endpointLabel)
                    if !controller.settings.isLoopbackHost {
                        ForEach(lanEndpoints, id: \.self) { endpoint in
                            endpointLine(label: "LAN", url: endpoint)
                        }
                    }
                }
                Spacer()
                Button("Start") {
                    controller.startRelay()
                }
                .buttonStyle(.borderedProminent)
                .disabled(controller.lifecycle == .starting || controller.lifecycle == .running)
                Button("Stop") {
                    controller.stopRelay()
                }
                .disabled(controller.lifecycle == .stopping || controller.lifecycle == .stopped)
                Button("Restart") {
                    controller.restartRelay()
                }
                Button("Settings…") {
                    openSettings()
                }
            }
        }
    }

    private var lanEndpoints: [String] {
        NetworkInfo.privateIPv4Addresses().map {
            "http://\($0):\(controller.settings.port)/v1"
        }
    }

    private func endpointLine(label: String, url: String) -> some View {
        HStack(spacing: 6) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 36, alignment: .leading)
            Text(url)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(url, forType: .string)
            } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Copy URL")
        }
    }

    // MARK: - Access modes

    private var accessCard: some View {
        SectionCard(
            title: "Access",
            caption: "Changes apply immediately and restart a running relay."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Authentication")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Picker("Authentication", selection: Binding(
                        get: { controller.settings.requireBearerAuth },
                        set: { controller.setAuthMode(requireToken: $0) }
                    )) {
                        Text("Protected (token)").tag(true)
                        Text("Open (no auth)").tag(false)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                }
                VStack(alignment: .leading, spacing: 5) {
                    Text("Network")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Picker("Network", selection: Binding(
                        get: { !controller.settings.isLoopbackHost },
                        set: { controller.setPrivateNetworkExposure($0) }
                    )) {
                        Text("Loopback only").tag(false)
                        Text("Private network").tag(true)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                }
                if !controller.settings.requireBearerAuth {
                    Label("Anyone who can reach the listener can use the relay.", systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                if !controller.settings.isLoopbackHost && controller.settings.enableClaudeExperimental {
                    Label("Claude is loopback-only; it stays disabled while exposed to the LAN.", systemImage: "info.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Relay token

    private var tokenCard: some View {
        SectionCard(
            title: "Relay Token",
            caption: "Client credential for protected mode."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                if let status = controller.statusSnapshot {
                    StatusBadge(
                        text: status.relayTokenReady ? "Token ready" : "Token missing",
                        kind: status.relayTokenReady ? .active : .danger
                    )
                } else {
                    StatusBadge(text: "Relay not reachable", kind: .neutral)
                }
                HStack(spacing: 8) {
                    Button("Reveal") {
                        controller.revealRelayToken()
                    }
                    Button("Rotate") {
                        controller.rotateRelayToken()
                    }
                    Button("Set Custom…") {
                        showingCustomTokenSheet = true
                    }
                }
                .disabled(!controller.settings.requireBearerAuth)
                if !controller.settings.requireBearerAuth {
                    Text("Not used in open (no auth) mode.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Providers

    private var providersCard: some View {
        SectionCard(title: "Providers") {
            VStack(alignment: .leading, spacing: 10) {
                if let status = controller.statusSnapshot {
                    ForEach(status.providers) { provider in
                        providerRow(provider)
                    }
                } else {
                    Text("Start the relay to see provider status.")
                        .foregroundStyle(.secondary)
                }
                Divider()
                HStack(spacing: 8) {
                    Button("OpenAI Login") {
                        controller.runOpenAILogin()
                    }
                    Button("Claude Login") {
                        controller.runClaudeLogin()
                    }
                    Button("Claude setup-token") {
                        controller.runClaudeSetupToken()
                    }
                    Spacer()
                    Button("Run Doctor") {
                        controller.runDoctor()
                    }
                    .help("Checks setup, login readiness, and upstream connectivity. Output goes to the Console tab.")
                }
            }
        }
    }

    private func providerRow(_ provider: ProviderSnapshot) -> some View {
        HStack(spacing: 10) {
            Text(provider.id.capitalized)
                .font(.body.weight(.medium))
                .frame(width: 80, alignment: .leading)
            StatusBadge(
                text: provider.enabled ? "Enabled" : "Disabled",
                kind: provider.enabled ? .active : .neutral
            )
            StatusBadge(
                text: provider.ready ? "Ready" : "Not ready",
                kind: provider.ready ? .active : .warning
            )
            Text(provider.detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
        }
    }

    // MARK: - Custom token sheet

    private var customTokenSheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Set Custom Relay Token")
                .font(.title3.weight(.semibold))
            SecureField("Relay token", text: $customToken)
                .textFieldStyle(.roundedBorder)
            HStack {
                Spacer()
                Button("Cancel") {
                    customToken = ""
                    showingCustomTokenSheet = false
                }
                Button("Save Token") {
                    controller.setCustomRelayToken(customToken)
                    customToken = ""
                    showingCustomTokenSheet = false
                }
                .buttonStyle(.borderedProminent)
                .disabled(customToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(width: 420)
    }
}
