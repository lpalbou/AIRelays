"""Sign-ins are always stored as private files (ADR 0008): Settings offers no
storage choice, and saving never sends one."""

from test_log_retention_browser import dashboard_url, open_settings, page, playwright  # noqa: F401


def test_settings_offer_no_login_storage_choice(page, dashboard_url):
    page.add_init_script("""
        window.savedSettings = [];
        window.__TAURI__ = {core: {invoke: async (command, args) => {
            if (command === 'save_settings') window.savedSettings.push(args.settings);
            return (await import('/js/mock.js')).mockInvoke(command, args);
        }}};
    """)
    open_settings(page, dashboard_url)
    playwright.expect(page.get_by_label("Sign-in timeout (seconds)")).to_be_visible()
    playwright.expect(page.get_by_text("Login storage")).to_have_count(0)
    playwright.expect(page.locator("#se-authStorageMode")).to_have_count(0)

    page.get_by_role("button", name="Save", exact=True).click()
    playwright.expect(page.get_by_text("Settings saved", exact=True)).to_be_visible()
    saved = page.evaluate("window.savedSettings")
    assert len(saved) == 1
    assert "authStorageMode" not in saved[0]
