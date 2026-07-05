import AppKit

/// Loads the branded status-bar glyphs bundled with the app.
///
/// The glyphs are color-coded rather than template-rendered: a green bolt
/// with relay arcs when the relay is reachable, a red bolt when it is not.
/// The PNGs are rendered @2x (44x36 px) and displayed at 22x18 pt.
enum MenuBarIcon {
    static func image(active: Bool) -> NSImage {
        let name = active ? "menu_bar_icon_connected" : "menu_bar_icon_disconnected"
        guard let url = Bundle.module.url(forResource: name, withExtension: "png"),
              let image = NSImage(contentsOf: url) else {
            // Fallback keeps the menu bar item usable if resources are missing.
            return NSImage(systemSymbolName: "bolt.horizontal.circle", accessibilityDescription: "AIRelays")!
        }
        // Color must survive rendering, so this is intentionally not a template image.
        image.isTemplate = false
        image.size = NSSize(width: 22, height: 18)
        return image
    }
}
