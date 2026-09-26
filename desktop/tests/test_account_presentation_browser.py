"""Provider hierarchy and compact account details, against the local mock UI."""

import pytest

from test_claude_accounts_browser import open_dashboard as open_accounts
from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401


def open_dashboard(page, url):
    section = open_accounts(page, url)
    # Account identities arrive before the asynchronous usage request.
    playwright.expect(section.locator(".account-block").filter(has_text="work@claude.ai")).to_contain_text("41%")
    return section


@pytest.mark.parametrize("width", [1000, 760])
def test_provider_headings_and_inline_help_layout(page, dashboard_url, width):
    page.set_viewport_size({"width": width, "height": 1000})
    anthropic = open_dashboard(page, dashboard_url)
    for name in ("OpenAI", "Anthropic"):
        section = page.get_by_role("region", name=f"{name} accounts", exact=True)
        title = section.get_by_role("heading", name=name, exact=True)
        playwright.expect(title).to_be_visible()
        heading_size = page.get_by_role("heading", name="Overview", exact=True).evaluate("e => getComputedStyle(e).fontSize")
        assert heading_size == "17px"
        assert title.evaluate("e => getComputedStyle(e).fontSize") == heading_size
        assert page.get_by_role("heading", name="Connect Your App", exact=True).evaluate("e => getComputedStyle(e).fontSize") == heading_size
        for row in section.locator(".account-block").all():
            trigger = row.get_by_role("button", name="Account details for")
            playwright.expect(trigger).to_have_text("?")
            assert row.locator(":scope > .account-more").count() == 0
            geometry = row.evaluate("""row => {
                const email = row.querySelector('.account-email').getBoundingClientRect();
                const help = row.querySelector('.account-more-trigger').getBoundingClientRect();
                const head = row.querySelector('.account-head').getBoundingClientRect();
                return {gap: help.left-email.right, offset: Math.abs((help.top+help.height/2)-(email.top+email.height/2)),
                        height: head.height, width: help.width};
            }""")
            assert 0 <= geometry["gap"] <= 8
            assert geometry["offset"] <= 1
            assert geometry["height"] <= 26
            assert geometry["width"] >= 24
    assert page.locator("#content").evaluate("e => e.scrollWidth <= e.clientWidth")
    page.get_by_role("region", name="OpenAI accounts", exact=True).evaluate("""e => {
        const content = document.getElementById('content');
        content.scrollTop += e.getBoundingClientRect().top-content.getBoundingClientRect().top-12;
    }""")
    page.screenshot(path=f"/tmp/airelays-account-layout-{width}.png")
    playwright.expect(anthropic.get_by_role("heading", name="Anthropic")).to_be_visible()


def test_details_support_hover_click_keyboard_and_outside_dismissal(page, dashboard_url):
    section = open_dashboard(page, dashboard_url)
    name = "Account details for work@claude.ai"
    trigger = section.get_by_role("button", name=name, exact=True)
    panel = section.get_by_role("region", name=name, exact=True)
    trigger.hover()
    playwright.expect(panel).to_be_visible()
    playwright.expect(panel).to_contain_text("Weekly resets")
    # Moving across the gap to the popup must not collapse it.
    box = panel.bounding_box()
    page.mouse.move(box["x"]+12, box["y"]+12, steps=12)
    playwright.expect(panel).to_be_visible()
    page.get_by_role("heading", name="Anthropic", exact=True).hover()
    playwright.expect(panel).to_be_hidden()
    trigger.click()
    page.get_by_role("heading", name="Anthropic", exact=True).hover()
    playwright.expect(panel).to_be_visible()
    playwright.expect(trigger).to_have_attribute("aria-expanded", "true")
    trigger.press("Escape")
    playwright.expect(panel).to_be_hidden()
    playwright.expect(trigger).to_be_focused()
    trigger.press("Enter")
    playwright.expect(panel).to_be_visible()
    # A normal status refresh rebuilds account rows; keep the open panel
    # and keyboard focus attached to the same account, not a removed node.
    page.evaluate("""async () => {
        const {mockInvoke} = await import('/js/mock.js');
        const {overviewView} = await import('/js/views/overview.js');
        const state = await mockInvoke('get_state');
        state.relay_status.providers.claude.accounts[1].cooldown_seconds = 30;
        await overviewView.update(state);
    }""")
    playwright.expect(panel).to_be_visible()
    playwright.expect(trigger).to_be_focused()
    trigger.press("Tab")
    playwright.expect(panel).to_be_focused()
    panel.press("Escape")
    playwright.expect(trigger).to_be_focused()
    trigger.press("Space")
    playwright.expect(panel).to_be_visible()
    page.get_by_role("heading", name="Anthropic", exact=True).click()
    playwright.expect(panel).to_be_hidden()
    assert page.evaluate("window.claudeCalls") == []


def test_long_account_name_keeps_help_visible_and_popup_in_content(page, dashboard_url):
    page.set_viewport_size({"width": 760, "height": 720})
    section = open_dashboard(page, dashboard_url)
    row = section.locator(".account-block").filter(has=page.get_by_role("button", name="Account details for work@claude.ai", exact=True))
    row.locator(".account-email").evaluate("e => e.textContent = 'a-very-long-account-name'.repeat(8)+'@example.com'")
    trigger = row.get_by_role("button", name="Account details for work@claude.ai", exact=True)
    trigger.click()
    panel = row.get_by_role("region", name="Account details for work@claude.ai", exact=True)
    playwright.expect(panel).to_be_visible()
    assert row.locator(".account-email").evaluate("e => e.scrollWidth > e.clientWidth")
    box = panel.bounding_box()
    content = page.locator("#content").bounding_box()
    assert box["x"] >= content["x"]
    assert box["x"] + box["width"] <= content["x"] + content["width"]
    assert box["y"] >= 0 and box["y"] + box["height"] <= 720
    page.screenshot(path="/tmp/airelays-account-details-narrow.png")
