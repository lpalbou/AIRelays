// Models: every model id this endpoint accepts, grouped by provider, with
// one-click copy of the exact id for use in curl / SDKs / external tools.

import { api, copyText } from "../api.js";
import { icon } from "../icons.js";

let root = null;
let models = [];
let filterText = "";
let loadedOnce = false;

const PROVIDER_NAMES = { openai: "OpenAI", claude: "Claude" };

export const modelsView = {
  async mount(container, ctx) {
    root = document.createElement("div");
    root.innerHTML = `
      <h1 class="view-title">Models</h1>
      <p class="view-subtitle">
        Model ids this endpoint accepts — use them as <code>model</code> in requests to
        <span class="mono" id="mo-endpoint"></span>.
      </p>
      <div class="row" style="margin-bottom:10px">
        <div class="search-wrap">
          ${icon("search", 14)}
          <input type="text" id="mo-filter" class="filter-input" placeholder="Filter models…" aria-label="Filter models" />
        </div>
        <span class="spacer"></span>
        <button class="btn btn-small btn-ghost" id="mo-refresh">${icon("refresh", 13)} Refresh</button>
      </div>
      <div id="mo-list"><div class="empty">Loading…</div></div>
    `;
    container.appendChild(root);
    const state = ctx.getState();
    root.querySelector("#mo-endpoint").textContent = state?.local_endpoint ?? "the relay";
    root.querySelector("#mo-filter").addEventListener("input", (event) => {
      filterText = event.target.value.toLowerCase();
      renderList();
    });
    root.querySelector("#mo-refresh").addEventListener("click", load);
    loadedOnce = false;
    await load();
    loadedOnce = true;
  },
  async update(state) {
    // The relay may come up after the tab was opened on a stopped relay.
    if (state?.reachable && loadedOnce && models.length === 0) {
      loadedOnce = false; // avoid re-entry while the load runs
      await load();
      loadedOnce = true;
    }
  },
  unmount() {
    root = null;
    models = [];
    filterText = "";
    loadedOnce = false;
  },
};

async function load() {
  if (!root) return;
  const list = root.querySelector("#mo-list");
  let payload;
  try {
    payload = await api.getModels();
  } catch (error) {
    if (!root) return;
    models = [];
    list.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    const title = document.createElement("div");
    title.className = "empty-title";
    title.textContent = "No models available";
    const detail = document.createElement("div");
    detail.textContent = String(error);
    empty.append(title, detail);
    list.appendChild(empty);
    return;
  }
  if (!root) return;
  models = Array.isArray(payload?.data) ? payload.data : [];
  renderList();
}

function visibleModels() {
  if (!filterText) return models;
  return models.filter((model) =>
    [model.id, model.airelays?.provider ?? ""].join(" ").toLowerCase().includes(filterText)
  );
}

function renderList() {
  if (!root) return;
  const list = root.querySelector("#mo-list");
  list.innerHTML = "";
  const visible = visibleModels();
  if (visible.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = models.length === 0
      ? "Start the relay to list models."
      : "No models match the filter.";
    list.appendChild(empty);
    return;
  }

  // Group by provider, preserving the relay's order within each group.
  const groups = new Map();
  for (const model of visible) {
    const provider = model.airelays?.provider ?? "other";
    if (!groups.has(provider)) groups.set(provider, []);
    groups.get(provider).push(model);
  }

  for (const [provider, entries] of groups) {
    const card = document.createElement("section");
    card.className = "card";
    const head = document.createElement("div");
    head.className = "provider-head";
    head.style.marginTop = "0";
    const title = document.createElement("h3");
    title.textContent = PROVIDER_NAMES[provider] ?? provider;
    const count = document.createElement("span");
    count.className = "account-plan";
    count.textContent = `${entries.length} model${entries.length === 1 ? "" : "s"}`;
    head.append(title, count);
    card.appendChild(head);
    for (const model of entries) {
      card.appendChild(modelRow(model));
    }
    list.appendChild(card);
  }
}

function modelRow(model) {
  const row = document.createElement("div");
  row.className = "model-row";
  const id = document.createElement("code");
  id.className = "model-id";
  id.textContent = model.id;
  row.appendChild(id);
  if (model.airelays?.experimental) {
    const badge = document.createElement("span");
    badge.className = "badge badge-neutral";
    badge.textContent = "Experimental";
    row.appendChild(badge);
  }
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  row.appendChild(spacer);
  const copy = document.createElement("button");
  copy.className = "copy-btn";
  copy.innerHTML = `${icon("copy", 13)} Copy`;
  copy.setAttribute("aria-label", `Copy model id ${model.id}`);
  copy.title = "Copy the exact model id";
  copy.addEventListener("click", () => copyText(model.id, `Copied ${model.id}`));
  row.appendChild(copy);
  return row;
}
