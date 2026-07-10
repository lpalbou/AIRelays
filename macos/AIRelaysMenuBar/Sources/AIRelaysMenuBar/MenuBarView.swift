import SwiftUI

/// Content of the status-bar dropdown menu.
struct MenuBarRootView: View {
    @ObservedObject var controller: RelayController
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Group {
            Text("\(controller.lifecycle.label) — \(controller.endpointLabel)")
            Divider()
            Button("Open Dashboard") {
                openWindow(id: "dashboard")
            }
            Button("Open Settings") {
                openSettings()
            }
            Divider()
            Button("Start Relay") {
                controller.startRelay()
            }
            .disabled(controller.lifecycle == .starting || controller.lifecycle == .running)
            Button("Stop Relay") {
                controller.stopRelay()
            }
            .disabled(controller.lifecycle == .stopped || controller.lifecycle == .stopping)
            Button("Restart Relay") {
                controller.restartRelay()
            }
            Divider()
            Picker("Auth", selection: authModeBinding) {
                Text("Protected (relay token)").tag(true)
                Text("Open (no auth)").tag(false)
            }
            .pickerStyle(.inline)
            Picker("Network", selection: networkExposureBinding) {
                Text("Loopback only (this Mac)").tag(false)
                Text("Private network (LAN)").tag(true)
            }
            .pickerStyle(.inline)
            Divider()
            Button("OpenAI Login") {
                controller.runOpenAILogin()
            }
            Button("Claude Login") {
                controller.runClaudeLogin()
            }
            Divider()
            Button("Quit") {
                controller.quitApp()
            }
        }
    }

    private var authModeBinding: Binding<Bool> {
        Binding(
            get: { controller.settings.requireBearerAuth },
            set: { controller.setAuthMode(requireToken: $0) }
        )
    }

    private var networkExposureBinding: Binding<Bool> {
        Binding(
            get: { !controller.settings.isLoopbackHost },
            set: { controller.setPrivateNetworkExposure($0) }
        )
    }
}
