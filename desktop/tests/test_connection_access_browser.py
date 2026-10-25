"""Connection instructions and access policy belong to a single card."""

import re

import pytest

from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401


@pytest.mark.parametrize("width", [1000, 760])
def test_connection_card_contains_working_access_controls_and_warnings(page, dashboard_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script("""
        window.accessCalls = [];
        window.__TAURI__ = {core: {invoke: async (command, args) => {
            if (command === 'set_auth_mode' || command === 'set_network_scope') {
                window.accessCalls.push({command, args});
            }
            return (await import('/js/mock.js')).mockInvoke(command, args);
        }}};
    """)
    page.goto(dashboard_url)
    card = page.get_by_role("region", name="Connect your app", exact=True)
    access = card.get_by_role("group", name="Connection access", exact=True)
    playwright.expect(page.get_by_role("region", name="Access", exact=True)).to_have_count(0)
    protected = access.get_by_role("button", name="Protected (API key)", exact=True)
    open_mode = access.get_by_role("button", name="Open (no key)", exact=True)
    local = access.get_by_role("button", name="This machine only", exact=True)
    network = access.get_by_role("button", name="Devices on my network", exact=True)
    internet = access.get_by_role("button", name="Anyone on the internet", exact=True)
    playwright.expect(protected).to_have_attribute("aria-pressed", "true")
    playwright.expect(network).to_have_attribute("aria-pressed", "true")
    playwright.expect(internet).to_have_attribute("aria-pressed", "false")
    playwright.expect(card.locator("#ov-key-row")).to_be_visible()
    playwright.expect(card.locator("#ov-internet-confirm")).to_be_hidden()
    playwright.expect(card.locator("#ov-internet-warning")).to_be_hidden()

    local.click()
    playwright.expect(local).to_have_attribute("aria-pressed", "true")
    open_mode.click()
    playwright.expect(open_mode).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-key-row")).to_be_hidden()
    playwright.expect(card.locator("#ov-key-open-hint")).to_be_visible()
    playwright.expect(card.locator("#ov-open-warning")).to_be_visible()
    playwright.expect(card.locator("#ov-open-lan-warning")).to_be_hidden()

    network.click()
    playwright.expect(network).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-open-lan-warning")).to_be_visible()
    playwright.expect(card.locator("#ov-open-warning")).to_be_hidden()
    # Claude follows the user's network choice: still on in network mode.
    playwright.expect(page.locator("#ov-claude-section")).to_be_visible()
    playwright.expect(page.locator("#ov-claude-off-badge")).to_be_hidden()
    playwright.expect(page.locator("#ov-claude-account")).not_to_contain_text("Paused")
    assert page.evaluate("window.accessCalls") == [
        {"command": "set_network_scope", "args": {"scope": "loopback"}},
        {"command": "set_auth_mode", "args": {"requireToken": False}},
        {"command": "set_network_scope", "args": {"scope": "private"}},
    ]
    assert card.evaluate("e => e.scrollWidth <= e.clientWidth")
    assert page.locator("#content").evaluate("e => e.scrollWidth <= e.clientWidth")
    assert page.evaluate("""() => {
        const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
        return ids.length === new Set(ids).size;
    }""")
    page.screenshot(path=f"/tmp/airelays-connection-access-warnings-{width}.png")

    protected.click()
    playwright.expect(protected).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-open-lan-warning")).to_be_hidden()
    playwright.expect(card.locator("#ov-key-row")).to_be_visible()
    local.click()
    playwright.expect(page.locator("#ov-claude-off-badge")).to_be_hidden()
    page.screenshot(path=f"/tmp/airelays-connection-access-{width}.png")


@pytest.mark.parametrize("width", [1000, 760])
def test_internet_access_needs_confirmation_and_forces_the_api_key(page, dashboard_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script("""
        window.accessCalls = [];
        window.__TAURI__ = {core: {invoke: async (command, args) => {
            if (command === 'set_auth_mode' || command === 'set_network_scope') {
                window.accessCalls.push({command, args});
            }
            return (await import('/js/mock.js')).mockInvoke(command, args);
        }}};
    """)
    page.goto(dashboard_url)
    card = page.get_by_role("region", name="Connect your app", exact=True)
    access = card.get_by_role("group", name="Connection access", exact=True)
    open_mode = access.get_by_role("button", name="Open (no key)", exact=True)
    protected = access.get_by_role("button", name="Protected (API key)", exact=True)
    network = access.get_by_role("button", name="Devices on my network", exact=True)
    internet = access.get_by_role("button", name="Anyone on the internet", exact=True)
    confirm = card.get_by_role("group", name="Confirm internet access", exact=True)

    # Start from open auth: enabling internet access must turn the key on.
    open_mode.click()
    playwright.expect(open_mode).to_have_attribute("aria-pressed", "true")
    # No internet row (and no public-IP lookup) before internet access is on.
    playwright.expect(card.locator("#ov-public-endpoint")).to_have_count(0)

    internet.click()
    playwright.expect(confirm).to_be_visible()
    playwright.expect(confirm).to_contain_text("plain HTTP")
    playwright.expect(confirm).to_contain_text("API key")
    page.screenshot(path=f"/tmp/airelays-connection-internet-confirm-{width}.png")
    playwright.expect(internet).to_have_attribute("aria-pressed", "false")
    confirm.get_by_role("button", name="Cancel", exact=True).click()
    playwright.expect(confirm).to_be_hidden()
    playwright.expect(network).to_have_attribute("aria-pressed", "true")
    playwright.expect(open_mode).to_have_attribute("aria-pressed", "true")

    internet.click()
    confirm.get_by_role("button", name="Enable internet access", exact=True).click()
    playwright.expect(internet).to_have_attribute("aria-pressed", "true")
    playwright.expect(confirm).to_be_hidden()
    playwright.expect(protected).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-key-row")).to_be_visible()
    playwright.expect(card.locator("#ov-internet-warning")).to_be_visible()
    playwright.expect(card.locator("#ov-public-address-note")).to_be_visible()
    playwright.expect(card.locator("#ov-public-address-note")).to_contain_text("forwards port")
    # The Internet row sits under Local and LAN, copyable like them.
    public_row = card.locator("#ov-public-endpoint")
    playwright.expect(public_row).to_be_visible()
    playwright.expect(public_row.locator(".tag")).to_have_text("Internet")
    playwright.expect(public_row.locator(".endpoint-url")).to_have_text(re.compile(r"^http://203\.0\.113\.10:\d+/v1$"))
    public_url = public_row.locator(".endpoint-url").inner_text()
    playwright.expect(public_row.get_by_role("button", name=f"Copy {public_url}", exact=True)).to_be_visible()
    tags = card.locator("#ov-connect-endpoints .endpoint .tag").all_inner_texts()
    assert tags[0] == "Local" and tags[-1] == "Internet", tags
    playwright.expect(card.locator("#ov-open-lan-warning")).to_be_hidden()
    playwright.expect(open_mode).to_be_disabled()
    assert "API key is mandatory" in open_mode.get_attribute("title")
    # The backend refuses open auth too, not only the disabled button.
    refused = page.evaluate("""async () => {
        try { await window.__TAURI__.core.invoke('set_auth_mode', {requireToken: false}); return null; }
        catch (error) { return String(error); }
    }""")
    assert "requires the API key" in refused
    assert card.evaluate("e => e.scrollWidth <= e.clientWidth")
    page.screenshot(path=f"/tmp/airelays-connection-internet-{width}.png")

    network.click()
    playwright.expect(network).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-internet-warning")).to_be_hidden()
    playwright.expect(card.locator("#ov-public-address-note")).to_have_count(0)
    playwright.expect(card.locator("#ov-public-endpoint")).to_have_count(0)
    playwright.expect(open_mode).to_be_enabled()
    assert page.evaluate("window.accessCalls") == [
        {"command": "set_auth_mode", "args": {"requireToken": False}},
        {"command": "set_network_scope", "args": {"scope": "any"}},
        {"command": "set_auth_mode", "args": {"requireToken": False}},
        {"command": "set_network_scope", "args": {"scope": "private"}},
    ]


def test_settings_card_headings_share_page_heading_size(page, dashboard_url):
    page.goto(dashboard_url)
    page.get_by_role("button", name="Settings", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Traffic log retention")).to_be_visible()
    assert page.locator(".card h2").evaluate_all("els => els.every(e => getComputedStyle(e).fontSize === getComputedStyle(document.querySelector('.view-title')).fontSize)")
