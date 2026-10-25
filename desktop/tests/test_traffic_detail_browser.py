"""Optional browser checks: pytest desktop/tests/test_traffic_detail_browser.py.

Requires Playwright and its Chromium browser; uses only the UI's mock backend,
whose traffic list gains one new newest request (req-live-N) on every poll.
"""

from test_log_retention_browser import dashboard_url, page, playwright  # noqa: F401

POLL_TIMEOUT_MS = 10_000  # the view polls every 3 s

STUB_CLIPBOARD = """
    window.__copied = [];
    Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: async (text) => { window.__copied.push(text); } },
    });
"""


def open_traffic(page, url):
    page.goto(url)
    page.get_by_role("button", name="Traffic", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Traffic", exact=True)).to_be_visible()
    playwright.expect(page.locator("#tr-body tr")).to_have_count(4)


def wait_for_rows(page, count):
    page.wait_for_function(
        f"document.querySelectorAll('#tr-body tr').length >= {count}", timeout=POLL_TIMEOUT_MS)


def test_selection_and_detail_survive_new_requests_and_copy_the_json(page, dashboard_url):
    page.add_init_script(STUB_CLIPBOARD)
    open_traffic(page, dashboard_url)
    rows = page.locator("#tr-body tr")
    playwright.expect(rows.nth(0)).to_have_attribute("data-request-id", "req-live-1")
    # Nothing is selected until the user picks a row: no auto-follow.
    playwright.expect(page.locator("#tr-body tr.selected")).to_have_count(0)
    playwright.expect(page.locator("#tr-copy")).to_be_hidden()

    rows.nth(1).click()
    selected = page.locator("#tr-body tr.selected")
    playwright.expect(selected).to_have_attribute("data-request-id", "req-1")
    detail = page.locator("#tr-detail")
    playwright.expect(detail).to_contain_text('"request_id": "req-1"')
    page.evaluate("window.__detailNode = document.querySelector('#tr-detail').firstChild")

    wait_for_rows(page, 6)  # two more polls, each adding a newer request on top
    playwright.expect(rows.nth(0)).to_have_attribute("data-request-id", "req-live-3")
    playwright.expect(selected).to_have_count(1)
    playwright.expect(selected).to_have_attribute("data-request-id", "req-1")
    playwright.expect(detail).to_contain_text('"request_id": "req-1"')
    playwright.expect(page.locator("#tr-detail-note")).to_be_hidden()
    # Identical content is not rewritten, so the reader's scroll is kept.
    assert page.evaluate("document.querySelector('#tr-detail').firstChild === window.__detailNode")

    copy = page.get_by_role("button", name="Copy request detail")
    playwright.expect(copy).to_have_count(1)
    page.locator("#tr-detail-card").hover()
    playwright.expect(copy).to_have_css("opacity", "1")
    copy.click()
    page.wait_for_function("window.__copied.length === 1")
    copied = page.evaluate("window.__copied[0]")
    assert copied == detail.text_content()
    assert '"request_id": "req-1"' in copied
    playwright.expect(page.get_by_text("Request detail copied", exact=True)).to_be_visible()


def test_copy_button_shows_on_keyboard_focus(page, dashboard_url):
    open_traffic(page, dashboard_url)
    page.locator("#tr-body tr").nth(1).click()
    page.mouse.move(0, 0)
    copy = page.get_by_role("button", name="Copy request detail")
    playwright.expect(copy).to_have_css("opacity", "0")
    copy.focus()
    playwright.expect(copy).to_have_css("opacity", "1")


def test_detail_stays_with_a_note_once_the_request_leaves_the_window(page, dashboard_url):
    page.add_init_script("""
        let calls = 0;
        window.__TAURI__ = { core: { invoke: async (command, args) => {
            const result = await (await import('/js/mock.js')).mockInvoke(command, args);
            if (command !== 'get_traffic') return result;
            calls += 1;
            return calls > 1 ? result.filter((entry) => entry.id !== 'req-1') : result;
        } } };
    """)
    open_traffic(page, dashboard_url)
    page.locator("#tr-body tr").nth(1).click()
    playwright.expect(page.locator("#tr-detail")).to_contain_text('"request_id": "req-1"')

    note = page.locator("#tr-detail-note")
    playwright.expect(note).to_be_visible(timeout=POLL_TIMEOUT_MS)
    playwright.expect(note).to_have_text("No longer in the recent list")
    playwright.expect(page.locator('#tr-body tr[data-request-id="req-1"]')).to_have_count(0)
    playwright.expect(page.locator("#tr-body tr.selected")).to_have_count(0)
    playwright.expect(page.locator("#tr-detail")).to_contain_text('"request_id": "req-1"')
    playwright.expect(page.get_by_role("button", name="Copy request detail")).to_have_count(1)


def test_table_scroll_position_holds_when_new_rows_arrive(page, dashboard_url):
    open_traffic(page, dashboard_url)
    wrap = page.locator(".table-wrap")
    wrap.evaluate("el => { el.style.maxHeight = '110px'; }")  # make 4 rows scroll
    assert wrap.evaluate("el => el.scrollHeight > el.clientHeight + 40")
    wrap.evaluate("el => { el.scrollTop = 40; }")
    anchor = page.evaluate("""() => {
        const wrap = document.querySelector('.table-wrap');
        const top = wrap.getBoundingClientRect().top + wrap.querySelector('thead').offsetHeight;
        const row = [...document.querySelectorAll('#tr-body tr')]
            .find((r) => r.getBoundingClientRect().bottom > top);
        return { id: row.dataset.requestId, offset: row.getBoundingClientRect().top - top };
    }""")
    wait_for_rows(page, 5)
    position = page.evaluate(f"""() => {{
        const wrap = document.querySelector('.table-wrap');
        const top = wrap.getBoundingClientRect().top + wrap.querySelector('thead').offsetHeight;
        const row = document.querySelector('#tr-body tr[data-request-id="{anchor["id"]}"]');
        return {{ scrollTop: wrap.scrollTop, offset: row.getBoundingClientRect().top - top }};
    }}""")
    assert position["scrollTop"] > 40  # moved down by the new row's height
    assert abs(position["offset"] - anchor["offset"]) < 1.5


def test_no_horizontal_overflow_and_copy_button_clears_the_json_at_760px(page, dashboard_url):
    # A long unbroken value wraps across the full width of the pane.
    page.add_init_script("""
        window.__TAURI__ = { core: { invoke: async (command, args) => {
            const result = await (await import('/js/mock.js')).mockInvoke(command, args);
            if (command !== 'get_traffic') return result;
            return result.map((entry) => entry.id !== 'req-1' ? entry : { ...entry,
                details: entry.details.replace('"model"', '"note": "' + 'x'.repeat(400) + '",\n  "model"') });
        } } };
    """)
    page.set_viewport_size({"width": 760, "height": 900})
    open_traffic(page, dashboard_url)
    page.locator("#tr-body tr").nth(1).click()
    playwright.expect(page.get_by_role("button", name="Copy request detail")).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    overlaps = page.evaluate("""() => {
        const button = document.querySelector('#tr-copy').getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(document.querySelector('#tr-detail'));
        return [...range.getClientRects()].filter((r) => r.width > 0 &&
            r.left < button.right && r.right > button.left &&
            r.top < button.bottom && r.bottom > button.top).length;
    }""")
    assert overlaps == 0
