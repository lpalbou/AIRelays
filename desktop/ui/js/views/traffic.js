// Traffic: request table over the relay's JSONL logs, with a detail pane.

import { api, copyText } from "../api.js";
import { icon } from "../icons.js";
import { isCurrentMount } from "../app.js";

let root = null;
// Rows are one per request (the relay groups log records by request_id), so
// the request id is the row's stable identity across polls. The selection is
// keyed by it, never by row position, and the selected detail is cached so it
// survives the entry scrolling out of the retained window.
let selectedId = null;
let selectedDetail = null;
let summaries = [];
let timer = null;
let lastDataKey = "";
let filterText = "";

const PLACEHOLDER = "Select a request to inspect its raw log records.";

export const trafficView = {
  async mount(container, _ctx, mountToken) {
    root = document.createElement("div");
    root.innerHTML = `
      <h1 class="view-title">Traffic</h1>
      <p class="view-subtitle">Recent requests handled by the relay, reconstructed from its logs.</p>
      <div class="row" style="margin-bottom:10px">
        <div class="search-wrap">
          ${icon("search", 14)}
          <input type="text" id="tr-filter" class="filter-input" placeholder="Filter by route, model, provider…" aria-label="Filter requests" />
        </div>
      </div>
      <div class="table-wrap">
        <table aria-label="Recent requests">
          <thead>
            <tr>
              <th>Time</th><th>Route</th><th>Provider</th>
              <th id="tr-account-head" hidden>Account</th>
              <th>Model</th><th>Status</th><th>Tokens (in/out)</th><th>Stage</th>
            </tr>
          </thead>
          <tbody id="tr-body"></tbody>
        </table>
        <div class="empty" id="tr-empty" hidden>
          <div class="empty-title">No requests yet</div>
          Requests appear here as apps use the relay.
        </div>
      </div>
      <div class="detail-card" id="tr-detail-card">
        <button type="button" class="copy-btn detail-copy" id="tr-copy" aria-label="Copy request detail" title="Copy request detail" hidden>${icon("copy", 14)}</button>
        <p class="detail-note" id="tr-detail-note" hidden>No longer in the recent list</p>
        <div class="detail" id="tr-detail">${PLACEHOLDER}</div>
      </div>
    `;
    container.appendChild(root);

    root.querySelector("#tr-copy").addEventListener("click", () => {
      if (selectedDetail != null) copyText(selectedDetail, "Request detail copied");
    });

    root.querySelector("#tr-filter").addEventListener("input", (event) => {
      filterText = event.target.value.toLowerCase();
      renderTable();
    });

    await refresh();
    if (isCurrentMount(mountToken)) {
      timer = setInterval(refresh, 3000);
    }
  },
  async update() {},
  unmount() {
    clearInterval(timer);
    timer = null;
    root = null;
    summaries = [];
    lastDataKey = "";
    filterText = "";
  },
};

async function refresh() {
  if (!root) return;
  let fetched;
  try {
    fetched = await api.getTraffic();
  } catch {
    return;
  }
  if (!root) return;
  // Fingerprint every row, not just the newest: a selected request lower in
  // the list can still be gaining records while it streams.
  const dataKey = fetched.map((s) => `${s.id}:${s.event_count}:${s.last_seen}`).join("|");
  if (dataKey === lastDataKey) return;
  lastDataKey = dataKey;
  summaries = fetched;
  renderTable();
}

function visibleSummaries() {
  if (!filterText) return summaries;
  return summaries.filter((summary) =>
    [summary.path, summary.method, summary.provider, summary.model, summary.account, String(summary.status_code)]
      .join(" ")
      .toLowerCase()
      .includes(filterText)
  );
}

function renderTable() {
  const body = root.querySelector("#tr-body");
  const empty = root.querySelector("#tr-empty");
  const visible = visibleSummaries();
  empty.hidden = visible.length > 0;

  // The Account column only exists on multi-account installs.
  const showAccount = summaries.some((summary) => summary.account);
  root.querySelector("#tr-account-head").hidden = !showAccount;

  // Preserve keyboard focus across rebuilds.
  const focusedId = document.activeElement?.dataset?.requestId ?? null;
  const anchor = scrollAnchor(body);

  body.innerHTML = "";
  for (const summary of visible) {
    const row = document.createElement("tr");
    row.className = "selectable" + (summary.id === selectedId ? " selected" : "");
    row.tabIndex = 0;
    row.dataset.requestId = summary.id;
    row.setAttribute("role", "button");
    row.addEventListener("click", () => select(summary.id));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select(summary.id);
      }
    });
    const cells = [
      cell(shortTime(summary.last_seen)),
      cell(`${summary.method} ${summary.path}`),
      cell(summary.provider),
    ];
    if (showAccount) {
      cells.push(cell(summary.account || "—"));
    }
    cells.push(
      cell(summary.model),
      statusCell(summary.status_code),
      tokensCell(summary.input_tokens, summary.output_tokens),
      cell(summary.last_phase)
    );
    row.append(...cells);
    body.appendChild(row);
  }
  restoreScroll(body, anchor);
  if (focusedId) {
    body.querySelector(`tr[data-request-id="${CSS.escape(focusedId)}"]`)?.focus({ preventScroll: true });
  }
  renderDetail();
}

// When the user has scrolled the table, remember the first visible row and
// its offset so new rows arriving at the top do not shift what they read.
// At the very top the table keeps following the newest requests.
function scrollAnchor(body) {
  const wrap = body.closest(".table-wrap");
  if (!wrap || wrap.scrollTop <= 0) return null;
  const top = wrap.getBoundingClientRect().top + (body.parentElement.tHead?.offsetHeight ?? 0);
  for (const row of body.rows) {
    const rect = row.getBoundingClientRect();
    if (rect.bottom > top) {
      return { wrap, id: row.dataset.requestId, offset: rect.top - top, scrollTop: wrap.scrollTop };
    }
  }
  return { wrap, id: null, offset: 0, scrollTop: wrap.scrollTop };
}

function restoreScroll(body, anchor) {
  if (!anchor) return;
  const { wrap } = anchor;
  const row = anchor.id ? body.querySelector(`tr[data-request-id="${CSS.escape(anchor.id)}"]`) : null;
  if (!row) {
    wrap.scrollTop = anchor.scrollTop;
    return;
  }
  const top = wrap.getBoundingClientRect().top + (body.parentElement.tHead?.offsetHeight ?? 0);
  wrap.scrollTop += row.getBoundingClientRect().top - top - anchor.offset;
}

function select(id) {
  const changed = id !== selectedId;
  selectedId = id;
  if (changed) selectedDetail = null;
  root.querySelectorAll("tr.selectable").forEach((row) => {
    row.classList.toggle("selected", row.dataset.requestId === id);
  });
  renderDetail();
  if (changed) root.querySelector("#tr-detail").scrollTop = 0;
}

// Nothing auto-selects: with no selection the pane shows the placeholder, and
// once the user picks a request it stays put whatever arrives later.
function renderDetail() {
  const detail = root.querySelector("#tr-detail");
  const note = root.querySelector("#tr-detail-note");
  const copy = root.querySelector("#tr-copy");
  const summary = selectedId == null ? null : summaries.find((s) => s.id === selectedId);
  if (summary) selectedDetail = summary.details;
  const text = selectedDetail ?? PLACEHOLDER;
  note.hidden = !(selectedDetail != null && !summary);
  copy.hidden = selectedDetail == null;
  // Only touch the DOM when the text really changed, so the user's scroll
  // position inside the detail survives every poll. A request that is still
  // streaming grows at the end; keep the reader where they were.
  if (detail.textContent !== text) {
    const scrollTop = detail.scrollTop;
    detail.textContent = text;
    detail.scrollTop = scrollTop;
  }
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function statusCell(code) {
  const td = document.createElement("td");
  td.textContent = code ?? "—";
  if (code >= 500) td.className = "status-5xx";
  else if (code >= 400) td.className = "status-4xx";
  else if (code) td.className = "status-2xx";
  return td;
}

function tokensCell(input, output) {
  const td = document.createElement("td");
  if (input == null && output == null) {
    td.textContent = "—";
  } else {
    td.textContent = `${input ?? "?"} / ${output ?? "?"}`;
  }
  return td;
}

function shortTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
}
