"""Connection instructions and access policy belong to a single card."""

import pytest

from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401


@pytest.mark.parametrize("width", [1000, 760])
def test_connection_card_contains_working_access_controls_and_warnings(page, dashboard_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script("""
        window.accessCalls = [];
        window.__TAURI__ = {core: {invoke: async (command, args) => {
            if (command === 'set_auth_mode' || command === 'set_network_exposure') {
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
    playwright.expect(protected).to_have_attribute("aria-pressed", "true")
    playwright.expect(network).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-key-row")).to_be_visible()

    local.click()
    playwright.expect(local).to_have_attribute("aria-pressed", "true")
    playwright.expect(card.locator("#ov-claude-note")).to_be_hidden()
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
    playwright.expect(card.locator("#ov-claude-note")).to_be_visible()
    playwright.expect(page.locator("#ov-claude-off-badge")).to_be_visible()
    assert page.evaluate("window.accessCalls") == [
        {"command": "set_network_exposure", "args": {"exposed": False}},
        {"command": "set_auth_mode", "args": {"requireToken": False}},
        {"command": "set_network_exposure", "args": {"exposed": True}},
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
    playwright.expect(card.locator("#ov-claude-note")).to_be_hidden()
    page.screenshot(path=f"/tmp/airelays-connection-access-{width}.png")


def test_settings_card_headings_share_page_heading_size(page, dashboard_url):
    page.goto(dashboard_url)
    page.get_by_role("button", name="Settings", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Traffic log retention")).to_be_visible()
    assert page.locator(".card h2").evaluate_all("els => els.every(e => getComputedStyle(e).fontSize === getComputedStyle(document.querySelector('.view-title')).fontSize)")
