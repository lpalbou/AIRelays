// Traffic: request table over the relay's JSONL logs, with a detail pane.

import { api } from "../api.js";
import { icon } from "../icons.js";
import { isCurrentMount } from "../app.js";

let root = null;
let selectedId = null;
let summaries = [];
let timer = null;
let lastDataKey = "";
let filterText = "";

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
              <th>Time</th><th>Route</th><th>Provider</th><th>Model</th>
              <th>Status</th><th>Stage</th><th>Events</th>
            </tr>
          </thead>
          <tbody id="tr-body"></tbody>
        </table>
        <div class="empty" id="tr-empty" hidden>
          <div class="empty-title">No requests yet</div>
          Requests appear here as apps use the relay.
        </div>
      </div>
      <div class="detail" id="tr-detail">Select a request to inspect its raw log records.</div>
    `;
    container.appendChild(root);

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
  const dataKey = `${fetched.length}:${fetched[0]?.id ?? ""}:${fetched[0]?.event_count ?? 0}:${fetched[0]?.last_seen ?? ""}`;
  if (dataKey === lastDataKey) return;
  lastDataKey = dataKey;
  summaries = fetched;
  renderTable();
}

function visibleSummaries() {
  if (!filterText) return summaries;
  return summaries.filter((summary) =>
    [summary.path, summary.method, summary.provider, summary.model, String(summary.status_code)]
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

  // Preserve keyboard focus across rebuilds.
  const focusedId = document.activeElement?.dataset?.requestId ?? null;

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
    row.append(
      cell(shortTime(summary.last_seen)),
      cell(`${summary.method} ${summary.path}`),
      cell(summary.provider),
      cell(summary.model),
      statusCell(summary.status_code),
      cell(summary.last_phase),
      cell(String(summary.event_count))
    );
    body.appendChild(row);
  }
  if (focusedId) {
    body.querySelector(`tr[data-request-id="${CSS.escape(focusedId)}"]`)?.focus();
  }
  renderDetail();
}

function select(id) {
  selectedId = id;
  root.querySelectorAll("tr.selectable").forEach((row) => {
    row.classList.toggle("selected", row.dataset.requestId === id);
  });
  renderDetail();
}

function renderDetail() {
  const detail = root.querySelector("#tr-detail");
  const summary = summaries.find((s) => s.id === selectedId);
  detail.textContent = summary
    ? summary.details
    : "Select a request to inspect its raw log records.";
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

function shortTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
}
