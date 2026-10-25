"""Optional browser checks: pytest desktop/tests/test_connect_examples_browser.py.

The "Try it" panel of "Connect Your App": code examples that never show the
API key on screen but copy it. Uses only the UI's mock backend.
"""

import pytest

from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401

MOCK_KEY = "airtk_mock_2f9c8e1a7b3d4c5e6f708192a3b4c5d6"
ROTATED_KEY = "airtk_rotated_9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a"
MASK = "••••••••••••••••"
LOCAL = "http://127.0.0.1:8317/v1"
LAN = "http://192.168.1.146:8317/v1"
STUB_CLIPBOARD = """
    window.copied = [];
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
        writeText: async (text) => { window.copied.push(text); },
    }});
"""


def open_examples(page, url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(STUB_CLIPBOARD)
    page.goto(url)
    card = page.get_by_role("region", name="Connect your app", exact=True)
    panel = card.get_by_role("region", name="Try it", exact=True)
    playwright.expect(panel).to_be_visible()
    return card, panel


def last_copied(page, count):
    page.wait_for_function(f"window.copied.length >= {count}")
    return page.evaluate("window.copied[window.copied.length - 1]")


@pytest.mark.parametrize("width", [1280, 760])
def test_examples_mask_the_key_and_copy_it(page, dashboard_url, width):
    card, panel = open_examples(page, dashboard_url, width)
    tablist = panel.get_by_role("tablist", name="Example language", exact=True)
    playwright.expect(tablist.get_by_role("tab")).to_have_text(
        ["curl", "Python", "JavaScript / TypeScript", "Rust"])
    curl_tab = tablist.get_by_role("tab", name="curl", exact=True)
    playwright.expect(curl_tab).to_have_attribute("aria-selected", "true")
    tabpanel = panel.get_by_role("tabpanel")
    playwright.expect(tabpanel).to_have_attribute("aria-labelledby", "ov-ex-tab-curl")

    code = panel.locator("#ov-ex-code")
    playwright.expect(code).to_contain_text(f"curl {LOCAL}/chat/completions")
    # The cheapest chat model of the catalog (lowest output, then input price), once it has loaded.
    playwright.expect(code).to_contain_text('"model": "claude:haiku"')
    playwright.expect(code).to_contain_text(f"Bearer {MASK}")
    assert MOCK_KEY not in panel.text_content()
    playwright.expect(panel.locator("#ov-ex-note")).to_have_text(
        "The key is hidden on screen; Copy includes it.")

    panel.get_by_role("button", name="Copy curl example with the API key", exact=True).click()
    copied = last_copied(page, 1)
    assert f"Authorization: Bearer {MOCK_KEY}" in copied
    assert f"{LOCAL}/chat/completions" in copied
    assert MASK not in copied
    playwright.expect(page.get_by_text("Example copied with your API key", exact=True)).to_be_visible()

    # The base URL choice lists the card's addresses and drives the code.
    bases = panel.get_by_role("group", name="Base URL", exact=True)
    playwright.expect(bases.get_by_role("button")).to_have_text(["Local", "LAN"])
    playwright.expect(bases.get_by_role("button", name="Local", exact=True)).to_have_attribute("aria-pressed", "true")
    bases.get_by_role("button", name="LAN", exact=True).click()
    playwright.expect(bases.get_by_role("button", name="LAN", exact=True)).to_have_attribute("aria-pressed", "true")
    playwright.expect(code).to_contain_text(f"curl {LAN}/chat/completions")
    playwright.expect(code).not_to_contain_text(LOCAL)

    # Tabs switch the language; the base URL choice is kept.
    tablist.get_by_role("tab", name="JavaScript / TypeScript", exact=True).click()
    playwright.expect(code).to_contain_text('import OpenAI from "openai";')
    playwright.expect(code).to_contain_text(f'baseURL: "{LAN}"')
    playwright.expect(code).to_contain_text(f'apiKey: "{MASK}"')
    tablist.get_by_role("tab", name="Python", exact=True).click()
    playwright.expect(code).to_contain_text("from openai import OpenAI")
    playwright.expect(code).to_contain_text("# pip install openai")
    tablist.get_by_role("tab", name="Rust", exact=True).click()
    rust_tab = tablist.get_by_role("tab", name="Rust", exact=True)
    playwright.expect(rust_tab).to_have_attribute("aria-selected", "true")
    playwright.expect(rust_tab).to_have_attribute("tabindex", "0")
    playwright.expect(curl_tab).to_have_attribute("tabindex", "-1")
    playwright.expect(code).to_contain_text("async_openai")
    playwright.expect(code).to_contain_text(f'.with_api_base("{LAN}")')
    playwright.expect(code).to_contain_text(f'.with_api_key("{MASK}")')
    assert MOCK_KEY not in panel.text_content()

    panel.get_by_role("button", name="Copy Rust example with the API key", exact=True).click()
    copied = last_copied(page, 2)
    assert f'.with_api_key("{MOCK_KEY}")' in copied and f'.with_api_base("{LAN}")' in copied

    # Arrow keys move between tabs and wrap around.
    rust_tab.focus()
    page.keyboard.press("ArrowRight")
    playwright.expect(curl_tab).to_be_focused()
    playwright.expect(curl_tab).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowLeft")
    playwright.expect(rust_tab).to_be_focused()
    page.keyboard.press("Home")
    playwright.expect(curl_tab).to_be_focused()
    page.keyboard.press("End")
    playwright.expect(rust_tab).to_have_attribute("aria-selected", "true")

    # No horizontal overflow: the code scrolls inside its own block.
    assert card.evaluate("e => e.scrollWidth <= e.clientWidth")
    assert page.locator("#content").evaluate("e => e.scrollWidth <= e.clientWidth")
    assert panel.locator(".ex-code").evaluate("e => getComputedStyle(e).overflowX") == "auto"
    side_by_side = panel.evaluate(
        "e => e.getBoundingClientRect().top < document.querySelector('.connect-main').getBoundingClientRect().bottom")
    assert side_by_side == (width >= 1100)
    assert page.evaluate("""() => {
        const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
        return ids.length === new Set(ids).size;
    }""")

    # The language is remembered across reloads.
    page.reload()
    playwright.expect(page.get_by_role("tab", name="Rust", exact=True)).to_have_attribute("aria-selected", "true")


def test_examples_follow_auth_mode_rotation_and_scope(page, dashboard_url):
    card, panel = open_examples(page, dashboard_url, 1280)
    code = panel.locator("#ov-ex-code")
    tablist = panel.get_by_role("tablist", name="Example language", exact=True)
    bases = panel.get_by_role("group", name="Base URL", exact=True)
    tablist.get_by_role("tab", name="Python", exact=True).click()
    bases.get_by_role("button", name="LAN", exact=True).click()
    playwright.expect(code).to_contain_text(f'base_url="{LAN}"')

    # Open mode: a visible placeholder key with its explanation; the tab and
    # base URL choice survive the re-render.
    card.get_by_role("button", name="Open (no key)", exact=True).click()
    playwright.expect(code).to_contain_text('api_key="any-value"')
    playwright.expect(code).to_contain_text("relay ignores the key")
    playwright.expect(code).to_contain_text(f'base_url="{LAN}"')
    playwright.expect(tablist.get_by_role("tab", name="Python", exact=True)).to_have_attribute("aria-selected", "true")
    panel.get_by_role("button", name="Copy Python example", exact=True).click()
    assert 'api_key="any-value"' in last_copied(page, 1)
    tablist.get_by_role("tab", name="curl", exact=True).click()
    playwright.expect(code).not_to_contain_text("Authorization")
    card.get_by_role("button", name="Protected (API key)", exact=True).click()
    playwright.expect(code).to_contain_text(f"Bearer {MASK}")

    # A new key is what Copy puts in the example afterwards.
    card.locator("#ov-token-rotate").click()
    page.locator("#ov-rotate-confirm").click()
    playwright.expect(page.get_by_text("New API key created", exact=True)).to_be_visible()
    panel.get_by_role("button", name="Copy curl example with the API key", exact=True).click()
    copied = last_copied(page, 2)
    assert ROTATED_KEY in copied and MOCK_KEY not in copied
    assert ROTATED_KEY not in panel.text_content()

    # Limiting to this machine drops the LAN address: back to Local.
    card.get_by_role("button", name="This machine only", exact=True).click()
    playwright.expect(bases.get_by_role("button")).to_have_text(["Local"])
    playwright.expect(code).to_contain_text(f"curl {LOCAL}/chat/completions")
    # Internet access adds the public address to the choice.
    card.get_by_role("button", name="Anyone on the internet", exact=True).click()
    card.get_by_role("button", name="Enable internet access", exact=True).click()
    playwright.expect(bases.get_by_role("button")).to_have_text(["Local", "LAN", "Internet"])
    bases.get_by_role("button", name="Internet", exact=True).click()
    playwright.expect(code).to_contain_text("curl http://203.0.113.10:8317/v1/chat/completions")


def test_unreadable_key_copies_the_mask_and_stopped_relay_uses_default_model(page, dashboard_url):
    page.add_init_script("""
        window.__TAURI__ = {core: {invoke: async (command, args) => {
            if (command === 'token_action') throw new Error('key file missing');
            return (await import('/js/mock.js')).mockInvoke(command, args);
        }}};
    """)
    card, panel = open_examples(page, dashboard_url, 1000)
    code = panel.locator("#ov-ex-code")
    playwright.expect(code).to_contain_text('"model": "claude:haiku"')
    panel.get_by_role("button", name="Copy curl example with the API key", exact=True).click()
    copied = last_copied(page, 1)
    assert f"Bearer {MASK}" in copied
    playwright.expect(page.get_by_text("Example copied without the key", exact=True)).to_be_visible()
    playwright.expect(page.get_by_text("The API key could not be read", exact=False)).to_be_visible()

    page.locator("#ov-stop").click()
    playwright.expect(code).to_contain_text('"model": "gpt-6-luna"')
