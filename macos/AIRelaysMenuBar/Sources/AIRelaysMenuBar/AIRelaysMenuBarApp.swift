import SwiftUI

@main
struct AIRelaysMenuBarApp: App {
    @StateObject private var controller = RelayController()

    var body: some Scene {
        MenuBarExtra {
            MenuBarRootView(controller: controller)
        } label: {
            // .original preserves the red/green status colors; template
            // rendering would flatten them to the menu bar tint.
            Image(nsImage: MenuBarIcon.image(active: controller.isReachable))
                .renderingMode(.original)
        }
        .menuBarExtraStyle(.menu)

        WindowGroup("AIRelays Dashboard", id: "dashboard") {
            DashboardView(controller: controller)
        }
        .defaultSize(width: 940, height: 680)

        Settings {
            SettingsRootView(controller: controller)
        }
    }
}
