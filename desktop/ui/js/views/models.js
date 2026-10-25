// Models: every model id this endpoint accepts, one table per provider:
// the id (one-click copy), account availability, reasoning modes,
// structured output, the API list-price reference and catalog notes.

import { api, copyText } from "../api.js";
import { icon } from "../icons.js";

let root = null;
let models = [];
let filterText = "";
let lastReachable = null;
let loading = false;
let lastLoadedAt = 0;

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
    root.querySelector("#mo-refresh").addEventListener("click", () => load(true));
    lastReachable = Boolean(state?.reachable);
    await load();
  },
  async update(state) {
    // Reload only on the unreachable→reachable TRANSITION (relay started
    // after this tab opened, or restarted with new accounts). Reloading
    // whenever the list is empty would poll /v1/models every 1.5s forever
    // on a relay with no models — real requests that also blink the tray.
    const reachable = Boolean(state?.reachable);
    const cameUp = reachable && lastReachable === false;
    lastReachable = reachable;
    if ((cameUp || (reachable && Date.now() - lastLoadedAt >= 300_000)) && !loading) {
      await load();
    }
  },
  unmount() {
    root = null;
    models = [];
    filterText = "";
    lastReachable = null;
    loading = false;
    lastLoadedAt = 0;
  },
};

async function load(refresh = false) {
  if (!root || loading) return;
  loading = true;
  const currentRoot = root;
  const button = root.querySelector("#mo-refresh");
  button.disabled = true;
  try {
    await loadInner(refresh, currentRoot);
  } finally {
    if (root === currentRoot) {
      loading = false;
      lastLoadedAt = Date.now();
    }
    button.disabled = false;
  }
}

async function loadInner(refresh, currentRoot) {
  const list = root.querySelector("#mo-list");
  let payload;
  try {
    payload = await api.getModels(refresh);
  } catch (error) {
    if (root !== currentRoot) return;
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
  if (root !== currentRoot) return;
  models = Array.isArray(payload?.data) ? payload.data : [];
  renderList();
}

function visibleModels() {
  if (!filterText) return models;
  return models.filter((model) =>
    [model.id, model.airelays?.provider, model.airelays?.resolved_model, model.airelays?.display_name]
      .join(" ").toLowerCase().includes(filterText)
  );
}

// Columns of each provider table. `narrow` columns hide on small windows
// (CSS); their content moves under the model id where it matters.
const COLUMNS = [
  { key: "model", label: "Model" },
  {
    key: "accounts", label: "Accounts",
    title: "Signed-in accounts whose catalog includes this model. Balancing and failover use only those accounts; \"all\" means every account serves it.",
  },
  {
    key: "reasoning", label: "Reasoning",
    title: "Values accepted by \"reasoning_effort\". The highlighted mode is the default when the request omits it.",
  },
  {
    key: "structured", label: "Structured output",
    title: "\"response_format\" types honored on chat completions (plain text is always accepted).",
  },
  {
    key: "cost", label: "Cost / 1M",
    title: "Provider API list price per 1M tokens, input / output, in USD. A relative cost indicator only: subscription requests are not billed per token.",
  },
  { key: "notes", label: "Notes" },
];

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

  // Group by provider, in the relay's provider order.
  const groups = new Map();
  for (const model of visible) {
    const provider = model.airelays?.provider ?? "other";
    if (!groups.has(provider)) groups.set(provider, []);
    groups.get(provider).push(model);
  }

  for (const [provider, entries] of groups) {
    const card = document.createElement("section");
    card.className = "card";
    const name = PROVIDER_NAMES[provider] ?? provider;
    card.setAttribute("aria-label", `${name} models`);
    const head = document.createElement("div");
    head.className = "provider-head";
    head.style.marginTop = "0";
    const title = document.createElement("h3");
    title.textContent = name;
    const count = document.createElement("span");
    count.className = "account-plan";
    count.textContent = `${entries.length} model${entries.length === 1 ? "" : "s"}`;
    head.append(title, count);
    card.append(head, modelTable(name, [...entries].sort(compareModels)));
    list.appendChild(card);
  }
}

// Listed models first, then hidden ones; Claude aliases (claude:sonnet)
// lead their group; then by id, numbers compared as numbers.
function compareModels(a, b) {
  const rank = (model) => [
    model.airelays?.catalog_visibility === "hide" ? 1 : 0,
    model.id.startsWith("claude:") ? 0 : 1,
  ];
  const [hiddenA, aliasA] = rank(a);
  const [hiddenB, aliasB] = rank(b);
  return hiddenA - hiddenB || aliasA - aliasB
    || a.id.localeCompare(b.id, undefined, { numeric: true, sensitivity: "base" });
}

function modelTable(providerName, entries) {
  const wrap = document.createElement("div");
  wrap.className = "mt-wrap";
  const table = document.createElement("table");
  table.className = "models-table";
  const caption = document.createElement("caption");
  caption.className = "mt-caption";
  caption.textContent = `${providerName} models`;
  table.appendChild(caption);
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const column of COLUMNS) {
    const th = document.createElement("th");
    th.scope = "col";
    th.className = `mt-col-${column.key}`;
    th.textContent = column.label;
    if (column.title) th.title = column.title;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  const tbody = document.createElement("tbody");
  for (const model of entries) tbody.appendChild(modelRow(model));
  table.append(thead, tbody);
  wrap.appendChild(table);
  return wrap;
}

function cell(key) {
  const td = document.createElement("td");
  td.className = `mt-col-${key}`;
  return td;
}

function dash(title) {
  const span = document.createElement("span");
  span.className = "mt-none";
  span.textContent = "—";
  span.title = title;
  return span;
}

function modelRow(model) {
  const meta = model.airelays ?? {};
  const row = document.createElement("tr");
  row.className = "mt-row";
  row.tabIndex = 0;
  row.dataset.modelId = model.id;
  // Keyboard: Enter copies the id; arrows move between rows.
  row.addEventListener("keydown", (event) => {
    if (event.target !== row) return;
    if (event.key === "Enter") {
      event.preventDefault();
      copyText(model.id, `Copied ${model.id}`);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const next = event.key === "ArrowDown" ? row.nextElementSibling : row.previousElementSibling;
      if (next) {
        event.preventDefault();
        next.focus();
      }
    }
  });

  const modelCell = cell("model");
  const idLine = document.createElement("div");
  idLine.className = "mt-id-line";
  const id = document.createElement("code");
  id.className = "model-id";
  id.textContent = model.id;
  const described = [meta.display_name, meta.description].filter(Boolean).join(" — ");
  if (described) id.title = described;
  const copy = document.createElement("button");
  copy.className = "copy-btn";
  copy.type = "button";
  copy.tabIndex = -1;
  copy.innerHTML = icon("copy", 13);
  copy.setAttribute("aria-label", `Copy model id ${model.id}`);
  copy.title = "Copy the exact model id (or press Enter on the row)";
  copy.addEventListener("click", () => copyText(model.id, `Copied ${model.id}`));
  idLine.append(id, copy);
  modelCell.appendChild(idLine);
  // Narrow windows hide the Notes column; the same badges move here.
  const inlineNotes = notesList(model);
  if (inlineNotes) {
    inlineNotes.classList.add("mt-notes-inline");
    modelCell.appendChild(inlineNotes);
  }

  const notesCell = cell("notes");
  notesCell.appendChild(notesList(model) ?? dash("Nothing to note"));

  row.append(
    modelCell,
    accountsCell(meta.account_availability),
    reasoningCell(meta.reasoning),
    structuredCell(meta.structured_output),
    costCell(meta.pricing),
    notesCell,
  );
  return row;
}

function accountsCell(availability) {
  const td = cell("accounts");
  const total = Number(availability?.total);
  if (!availability || !(total > 1)) {
    const all = document.createElement("span");
    all.className = "mt-none";
    all.textContent = "all";
    all.title = "Every signed-in account serves this model.";
    td.appendChild(all);
    return td;
  }
  const supported = Math.max(0, Math.min(total, Number(availability.supported) || 0));
  const wrap = document.createElement("span");
  wrap.className = "mt-accounts";
  wrap.title = `${supported} of ${total} signed-in accounts list this model. Balancing and failover use only those accounts.`;
  const text = document.createElement("span");
  text.className = "mt-accounts-text";
  text.textContent = `${supported} / ${total}`;
  const bar = document.createElement("span");
  bar.className = "mt-bar";
  bar.setAttribute("aria-hidden", "true");
  const fill = document.createElement("span");
  fill.className = `mt-bar-fill ${supported === total ? "is-full" : supported === 0 ? "is-empty" : "is-partial"}`;
  fill.style.width = `${(supported / total) * 100}%`;
  bar.appendChild(fill);
  wrap.append(text, bar);
  td.appendChild(wrap);
  return td;
}

function reasoningCell(reasoning) {
  const td = cell("reasoning");
  const modes = Array.isArray(reasoning?.modes) ? reasoning.modes : [];
  if (modes.length === 0) {
    td.appendChild(dash("No reasoning effort control on this model."));
    return td;
  }
  const parameter = reasoning.parameter ?? "reasoning_effort";
  const list = document.createElement("span");
  list.className = "mt-modes";
  list.title = reasoning.default
    ? `Set "${parameter}" in requests. Default when omitted: ${reasoning.default}.`
    : `Set "${parameter}" in requests. Default when omitted: the model's adaptive default.`;
  for (const mode of modes) {
    const item = document.createElement(mode === reasoning.default ? "strong" : "span");
    item.className = mode === reasoning.default ? "mt-mode mt-mode-default" : "mt-mode";
    item.textContent = mode;
    if (mode === reasoning.default) {
      const hint = document.createElement("span");
      hint.className = "mt-sr";
      hint.textContent = " (default)";
      item.appendChild(hint);
    }
    list.appendChild(item);
  }
  td.appendChild(list);
  return td;
}

function structuredCell(structured) {
  const td = cell("structured");
  const types = Array.isArray(structured?.types) ? structured.types : [];
  if (types.length === 0) {
    td.appendChild(dash("No structured output on this model."));
    return td;
  }
  const list = document.createElement("span");
  list.className = "mt-chips";
  list.title = `Set "${structured.parameter ?? "response_format"}" on chat completions requests.`;
  for (const type of types) {
    const chip = document.createElement("span");
    chip.className = "mt-chip";
    chip.textContent = type;
    list.appendChild(chip);
  }
  td.appendChild(list);
  return td;
}

// $4, $1.25, $0.20, $0.075: whole dollars bare, cents kept.
function usd(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "?";
  if (Number.isInteger(value)) return `$${value}`;
  const [whole, cents = ""] = value.toFixed(3).replace(/0+$/, "").split(".");
  return `$${whole}.${cents.padEnd(2, "0")}`;
}

function costCell(pricing) {
  const td = cell("cost");
  if (!pricing || typeof pricing.output_per_million !== "number") {
    td.appendChild(dash("No published price"));
    return td;
  }
  const wrap = document.createElement("span");
  wrap.className = "mt-cost";
  const lines = [
    `API list price per 1M tokens (${pricing.currency ?? "USD"}): input ${usd(pricing.input_per_million)}`
      + (typeof pricing.cached_input_per_million === "number" ? `, cached input ${usd(pricing.cached_input_per_million)}` : "")
      + `, output ${usd(pricing.output_per_million)}.`,
  ];
  if (pricing.basis) lines.push(pricing.basis);
  if (pricing.matched_family) lines.push(`Priced as ${pricing.matched_family}.`);
  lines.push(`As of ${pricing.as_of ?? "unknown date"}${pricing.source ? ` (${pricing.source})` : ""}.`);
  lines.push("A pay-as-you-go API proxy for relative cost only: your subscription is not billed per token.");
  wrap.title = lines.join("\n");
  const price = document.createElement("span");
  price.className = "mt-price";
  price.textContent = `${usd(pricing.input_per_million)} / ${usd(pricing.output_per_million)}`;
  wrap.appendChild(price);
  const tier = Number(pricing.cost_tier);
  if (tier >= 1 && tier <= 5) {
    const tierEl = document.createElement("span");
    tierEl.className = "mt-tier";
    tierEl.setAttribute("aria-label", `cost tier ${tier} of 5`);
    const on = document.createElement("span");
    on.className = "mt-tier-on";
    on.textContent = "$".repeat(tier);
    const off = document.createElement("span");
    off.className = "mt-tier-off";
    off.textContent = "$".repeat(5 - tier);
    tierEl.append(on, off);
    wrap.appendChild(tierEl);
  }
  td.appendChild(wrap);
  return td;
}

function notesList(model) {
  const meta = model.airelays ?? {};
  const list = document.createElement("span");
  list.className = "mt-notes";
  const badge = (text, title, tone = "neutral") => {
    const span = document.createElement("span");
    span.className = `badge badge-${tone} mt-badge`;
    span.textContent = text;
    span.title = title;
    list.appendChild(span);
  };
  if (meta.catalog_visibility === "hide") {
    badge("upstream-hidden", "Returned by the provider catalog, but hidden in its model picker. Its underlying identity is not inferred.", "warn");
  }
  const resolved = meta.resolved_model;
  if (resolved && resolved !== model.id) {
    badge(`resolves to ${resolved}`, "Concrete model reported by the installed Claude CLI. Aliases follow CLI updates.", "accent");
  } else if (meta.discovery_source === "configured") {
    badge("configured", "Configured override; availability has not been confirmed by the provider catalog.");
  }
  return list.childElementCount ? list : null;
}
