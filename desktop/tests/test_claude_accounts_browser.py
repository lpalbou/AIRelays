"""Claude account workflows against the dashboard's local mock backend."""

from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401


def open_dashboard(page, url):
    page.add_init_script("""
        window.claudeCalls = [];
        window.__TAURI__ = {core: {invoke: async (command, args) => {
            const mock = (await import('/js/mock.js')).mockInvoke;
            if (command === 'get_state') {
                await mock('set_network_exposure', {exposed: false});
            }
            if (command === 'run_login' || command === 'logout_claude') {
                window.claudeCalls.push({command, args});
            }
            return mock(command, args);
        }}};
    """)
    page.goto(url)
    section = page.get_by_role("region", name="Anthropic accounts", exact=True)
    playwright.expect(section.get_by_text("work@claude.ai", exact=True)).to_be_visible()
    return section


def test_accounts_have_distinct_usage_and_add_keeps_existing_accounts(page, dashboard_url, tmp_path):
    section = open_dashboard(page, dashboard_url)
    playwright.expect(section.locator(".account-block")).to_have_count(2)
    personal = section.locator(".account-block").filter(has_text="perso@claude.ai")
    work = section.locator(".account-block").filter(has_text="work@claude.ai")
    playwright.expect(personal).to_contain_text("61%")
    playwright.expect(work).to_contain_text("41%")
    section.get_by_role("button", name="Add account", exact=True).click()
    playwright.expect(section.locator(".account-block")).to_have_count(3)
    playwright.expect(section.get_by_text("perso@claude.ai", exact=True)).to_be_visible()
    playwright.expect(section.get_by_text("work@claude.ai", exact=True)).to_be_visible()
    assert page.evaluate("window.claudeCalls[0]") == {
        "command": "run_login", "args": {"provider": "claude", "account": None},
    }
    section.screenshot(path=str(tmp_path / "claude-accounts.png"))


def test_signout_targets_one_profile_and_preserves_default(page, dashboard_url):
    section = open_dashboard(page, dashboard_url)
    section.get_by_role("button", name="Sign out of Claude account work@claude.ai", exact=True).click()
    dialog = page.locator("#ov-logout-dialog")
    playwright.expect(dialog).to_contain_text("only this AIRelays profile")
    playwright.expect(dialog).to_contain_text("remaining accounts")
    dialog.get_by_role("button", name="Sign out", exact=True).click()
    playwright.expect(section.locator(".account-block")).to_have_count(1)
    playwright.expect(section.get_by_text("perso@claude.ai", exact=True)).to_be_visible()
    assert page.evaluate("window.claudeCalls[0]") == {
        "command": "logout_claude", "args": {"account": "claude-work"},
    }


def test_default_signout_explains_effect_on_other_cli_tools(page, dashboard_url):
    section = open_dashboard(page, dashboard_url)
    section.get_by_role("button", name="Sign out of Claude account perso@claude.ai", exact=True).click()
    dialog = page.locator("#ov-logout-dialog")
    playwright.expect(dialog).to_contain_text("Other tools using this default Claude Code sign-in are also signed out")
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    assert page.evaluate("window.claudeCalls") == []
