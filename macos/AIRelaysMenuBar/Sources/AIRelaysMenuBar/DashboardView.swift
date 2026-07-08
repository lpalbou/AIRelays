import SwiftUI

/// Main window: a compact tabbed layout instead of one long scroll.
///
/// - Overview: relay state, access modes, providers, token.
/// - Traffic: request table with a detail pane.
/// - Console: command output and diagnostics.
struct DashboardView: View {
    @ObservedObject var controller: RelayController

    var body: some View {
        TabView {
            OverviewTab(controller: controller)
                .tabItem {
                    Label("Overview", systemImage: "gauge.medium")
                }
            TrafficTab(controller: controller)
                .tabItem {
                    Label("Traffic", systemImage: "list.bullet.rectangle")
                }
            ConsoleTab(controller: controller)
                .tabItem {
                    Label("Console", systemImage: "terminal")
                }
        }
        .frame(minWidth: 860, minHeight: 620)
        .alert(item: $controller.alertMessage) { alert in
            Alert(title: Text(alert.title), message: Text(alert.message), dismissButton: .default(Text("OK")))
        }
    }
}
