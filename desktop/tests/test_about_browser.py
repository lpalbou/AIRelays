"""Optional browser checks: pytest desktop/tests/test_about_browser.py.

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


def open_about(page, url):
    page.goto(url)
    page.get_by_role("button", name="About", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="About", exact=True)).to_be_visible()


def test_about_shows_version_links_and_checks_for_updates_once(page, dashboard_url):
    open_about(page, dashboard_url)
    playwright.expect(page.locator("#ab-version")).to_have_text("0.0.0-mock")
    credit = page.locator(".about-credit")
    playwright.expect(credit).to_contain_text("Created by Laurent-Philippe Albou, PhD")
    playwright.expect(credit).to_contain_text("MIT License")
    playwright.expect(page.get_by_role("button", name="contact@abstractframework.ai")).to_be_visible()
    for label in ("Documentation", "Changelog", "Report an issue", "Source code", "License", "Disclaimer"):
        playwright.expect(page.get_by_role("button", name=label)).to_be_visible()
    playwright.expect(page.get_by_text("Update available", exact=True)).to_be_visible()
    playwright.expect(page.get_by_role("button", name="Update to 0.19.0")).to_be_visible()
    playwright.expect(page.get_by_label("What's new in 0.19.0")).to_contain_text("About page")
    # Returning to the tab reuses the result instead of asking GitHub again.
    page.get_by_role("button", name="Overview", exact=True).click()
    page.get_by_role("button", name="About", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Update to 0.19.0")).to_be_visible()


def test_update_asks_for_confirmation_before_installing(page, dashboard_url):
    open_about(page, dashboard_url)
    page.get_by_role("button", name="Update to 0.19.0").click()
    dialog = page.get_by_role("dialog")
    playwright.expect(dialog).to_contain_text("Update to AIRelays 0.19.0?")
    dialog.get_by_role("button", name="Cancel").click()
    playwright.expect(dialog).to_be_hidden()
    page.get_by_role("button", name="Update to 0.19.0").click()
    page.get_by_role("button", name="Update and restart").click()
    playwright.expect(page.get_by_role("status")).to_contain_text("closing to install the update")
    playwright.expect(page.get_by_role("button", name="Check for updates")).to_be_disabled()


def test_a_reopen_after_a_failed_update_lands_on_about_with_the_reason(page, dashboard_url):
    page.goto(f"{dashboard_url}/?mockUpdate=failed")
    playwright.expect(page.get_by_role("heading", name="About", exact=True)).to_be_visible()
    banner = page.locator("#ab-reopened")
    playwright.expect(banner).to_contain_text("The update did not install.")
    playwright.expect(banner).to_contain_text("Checksum mismatch")
    playwright.expect(banner).to_contain_text("You are still on AIRelays 0.0.0-mock")
    playwright.expect(page.get_by_text("The update did not install", exact=True)).to_be_visible()


def test_a_reopen_after_a_successful_update_confirms_the_new_version(page, dashboard_url):
    page.goto(f"{dashboard_url}/?mockUpdate=ok")
    playwright.expect(page.locator("#ab-reopened")).to_contain_text("Updated to AIRelays 0.0.0-mock.")
    playwright.expect(page.get_by_role("button", name="Update to 0.19.0")).to_be_visible()
    playwright.expect(page.locator("#ab-reopened")).to_be_visible()
