"""Optional browser checks: pytest desktop/tests/test_log_retention_browser.py.

Requires Playwright and its Chromium browser; uses only the UI's mock backend.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def dashboard_url():
    directory = Path(__file__).resolve().parents[1] / "ui"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def page(dashboard_url):
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 900})
        yield page
        browser.close()


def open_settings(page, url):
    page.goto(url)
    page.get_by_role("button", name="Settings", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Traffic log retention")).to_be_visible()


def test_presets_apply_and_keep_saved_policy_across_navigation(page, dashboard_url):
    open_settings(page, dashboard_url)
    days = page.get_by_label("Keep up to (days; 0 = no age limit)")
    playwright.expect(days).to_have_value("7")
    playwright.expect(page.get_by_label("Total disk limit (MiB)")).to_have_value("1024")
    page.get_by_role("button", name="1 month (30 days)", exact=True).click()
    playwright.expect(days).to_have_value("30")
    page.get_by_role("button", name="Apply log retention", exact=True).click()
    playwright.expect(page.get_by_text("Log retention applied", exact=True)).to_be_visible()
    page.get_by_role("button", name="Overview", exact=True).click()
    page.get_by_role("button", name="Settings", exact=True).click()
    playwright.expect(days).to_have_value("30")
    page.locator("#se-log-retention").screenshot(path="/tmp/airelays-log-retention.png")


def test_invalid_limits_keep_draft_and_saved_policy(page, dashboard_url):
    open_settings(page, dashboard_url)
    total = page.get_by_label("Total disk limit (MiB)")
    playwright.expect(total).to_have_value("1024")
    total.fill("10")
    page.get_by_role("button", name="Apply log retention", exact=True).click()
    playwright.expect(page.get_by_text("The file limit must not exceed the total disk limit.")).to_be_visible()
    playwright.expect(total).to_have_value("10")
    total.fill("-1")
    page.get_by_role("button", name="Apply log retention", exact=True).click()
    assert total.evaluate("element => element.validity.rangeUnderflow")


def test_unavailable_relay_disables_apply_and_explains_recovery(page, dashboard_url):
    page.add_init_script("""
        window.__TAURI__ = { core: { invoke: async (command, args) => {
            if (command === 'get_log_retention') throw new Error('Cannot reach the relay.');
            return (await import('/js/mock.js')).mockInvoke(command, args);
        } } };
    """)
    open_settings(page, dashboard_url)
    playwright.expect(page.locator("#lr-status")).to_contain_text("while the relay is stopped")
    playwright.expect(page.get_by_role("button", name="Apply log retention", exact=True)).to_be_disabled()
    playwright.expect(page.get_by_role("button", name="Reload policy & usage", exact=True)).to_be_enabled()
