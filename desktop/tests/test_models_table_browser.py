"""Optional browser checks: pytest desktop/tests/test_models_table_browser.py.

Requires Playwright and its Chromium browser; uses only the UI's mock backend.
"""

import pytest

from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401


def open_models(page, url):
    page.goto(url)
    page.get_by_role("button", name="Models", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Models", exact=True)).to_be_visible()


def row(page, model_id):
    return page.locator(f'tr.mt-row[data-model-id="{model_id}"]')


def test_models_render_as_tables_with_headers_accounts_and_costs(page, dashboard_url):
    open_models(page, dashboard_url)
    openai = page.get_by_role("table", name="OpenAI models")
    playwright.expect(openai).to_be_visible()
    headers = openai.locator("thead th")
    playwright.expect(headers).to_have_text(
        ["Model", "Accounts", "Reasoning", "Structured output", "Cost / 1M", "Notes"])
    playwright.expect(page.get_by_role("table", name="Claude models")).to_be_visible()
    playwright.expect(page.locator(".provider-head").first).to_contain_text("8 models")

    astra = row(page, "gpt-6-astra")
    playwright.expect(astra.locator(".mt-col-accounts")).to_have_text("2 / 2")
    playwright.expect(astra.locator(".mt-price")).to_have_text("$10 / $50")
    assert "cached input $1" in astra.locator(".mt-cost").get_attribute("title")
    assert "not billed per token" in astra.locator(".mt-cost").get_attribute("title")
    playwright.expect(astra.locator(".mt-mode-default")).to_contain_text("medium")
    playwright.expect(row(page, "gpt-5.6-sol").locator(".mt-col-accounts")).to_have_text("1 / 2")

    hidden = row(page, "gpt-reserve")
    playwright.expect(hidden.locator(".mt-col-cost")).to_have_text("—")
    assert hidden.locator(".mt-col-cost .mt-none").get_attribute("title") == "No published price"
    playwright.expect(hidden.locator(".mt-col-notes")).to_contain_text("upstream-hidden")

    # Listed models before hidden ones; Claude aliases lead their group.
    openai_ids = openai.locator("tbody tr").evaluate_all("rows => rows.map(r => r.dataset.modelId)")
    assert openai_ids[-2:] == ["codex-auto-review", "gpt-reserve"]
    claude_ids = page.get_by_role("table", name="Claude models").locator("tbody tr").evaluate_all(
        "rows => rows.map(r => r.dataset.modelId)")
    assert claude_ids[:3] == ["claude:haiku", "claude:opus", "claude:sonnet"]
    alias = row(page, "claude:sonnet")
    playwright.expect(alias.locator(".mt-col-accounts")).to_have_text("all")
    playwright.expect(alias.locator(".mt-col-notes")).to_contain_text("resolves to claude-sonnet-5-5")
    playwright.expect(alias.locator(".mt-price")).to_have_text("$2 / $10")
    playwright.expect(row(page, "gpt-image-2").locator(".mt-col-reasoning")).to_have_text("—")


def test_filter_narrows_rows_and_reports_no_match(page, dashboard_url):
    open_models(page, dashboard_url)
    page.get_by_label("Filter models").fill("luna")
    playwright.expect(page.locator("tr.mt-row")).to_have_count(1)
    playwright.expect(row(page, "gpt-5.6-luna")).to_be_visible()
    playwright.expect(page.get_by_role("table", name="Claude models")).to_have_count(0)
    page.get_by_label("Filter models").fill("no-such-model")
    playwright.expect(page.locator("tr.mt-row")).to_have_count(0)
    playwright.expect(page.get_by_text("No models match the filter.")).to_be_visible()
    page.get_by_label("Filter models").fill("claude-sonnet-5-5")
    playwright.expect(page.locator("tr.mt-row")).to_have_count(2)


def test_rows_are_keyboard_reachable_and_enter_copies_the_id(page, dashboard_url):
    open_models(page, dashboard_url)
    first = page.locator("tr.mt-row").first
    first.focus()
    page.keyboard.press("ArrowDown")
    second_id = page.locator("tr.mt-row").nth(1).get_attribute("data-model-id")
    assert page.evaluate("document.activeElement.dataset.modelId") == second_id
    page.keyboard.press("Enter")
    playwright.expect(page.locator("#toasts")).to_contain_text(f"Copied {second_id}")


@pytest.mark.parametrize("width", [1280, 1000, 760])
def test_models_table_never_overflows(page, dashboard_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_models(page, dashboard_url)
    playwright.expect(row(page, "gpt-6-astra")).to_be_visible()
    assert page.locator("#content").evaluate("e => e.scrollWidth <= e.clientWidth")
    for wrap in page.locator(".mt-wrap").all():
        assert wrap.evaluate("e => e.scrollWidth <= e.clientWidth")
    notes_column = page.locator("table.models-table th.mt-col-notes").first
    inline_notes = row(page, "gpt-reserve").locator(".mt-notes-inline")
    if width >= 1200:
        playwright.expect(notes_column).to_be_visible()
        playwright.expect(inline_notes).to_be_hidden()
    else:
        playwright.expect(notes_column).to_be_hidden()
        playwright.expect(inline_notes).to_contain_text("upstream-hidden")
    page.screenshot(path=f"/tmp/airelays-models-table-{width}.png", full_page=True)
