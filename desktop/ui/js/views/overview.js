// Overview: relay state, connect-your-app credentials, access modes,
// providers.

import { api, call, copyText, toast } from "../api.js";
import { icon } from "../icons.js";

let root = null;
let renderCache = { endpoints: "", providers: "" };
// Fetched on demand, never polled; cleared when hidden again.
let revealedToken = null;

let usageLoadedOnce = false;

export const overviewView = {
  async mount(container, ctx) {
    root = document.createElement("div");
    root.innerHTML = template();
    container.appendChild(root);
    bindActions(ctx);
    usageLoadedOnce = false;
    render(ctx.getState());
  },
  async update(state) {
    render(state);
    // Load usage automatically once the relay is reachable.
    if (state?.reachable && !usageLoadedOnce) {
      usageLoadedOnce = true;
      loadUsage();
    }
  },
  unmount() {
    root = null;
    revealedToken = null;
    renderCache = { endpoints: "", providers: "" };
  },
};

function template() {
  return `
    <h1 class="view-title">Overview</h1>
    <p class="view-subtitle">
      AIRelays gives your apps a local OpenAI-compatible endpoint backed by your own subscription login.
    </p>

    <section class="card" aria-label="Relay state">
      <div class="row">
        <span class="dot dot-neutral" id="ov-dot"></span>
        <div>
          <div class="hero-state" id="ov-state">Checking…</div>
          <div id="ov-endpoints"></div>
        </div>
        <span class="spacer"></span>
        <button class="btn btn-primary" id="ov-start">${icon("play")} Start</button>
        <button class="btn" id="ov-stop">${icon("stop")} Stop</button>
        <button class="btn" id="ov-restart">${icon("restart")} Restart</button>
      </div>
      <div class="hint" id="ov-unmanaged" hidden>
        <span>ⓘ</span>
        <span>Another AIRelays process is already answering on this address (started outside this app, e.g. from a terminal). The Stop button here cannot control it.</span>
      </div>
      <div class="hint warn" id="ov-mismatch" hidden>
        <span>⚠</span>
        <span>A relay is running on this address but rejects this app's key. If it was started with different settings, stop that process or rotate the key.</span>
      </div>
    </section>

    <section class="card" aria-label="Connect your app">
      <h2>Connect Your App</h2>
      <p class="card-caption">
        In your app or SDK, set the <strong>Base URL</strong> and <strong>API key</strong> below.
      </p>
      <div id="ov-connect-endpoints"></div>
      <div class="row" style="margin-top:10px" id="ov-key-row">
        <span class="tag-inline">API key</span>
        <code class="token-value" id="ov-token-value" aria-live="polite">••••••••••••••••</code>
        <button class="btn btn-small" id="ov-token-toggle" aria-label="Show API key" title="Show / hide">${icon("eye", 14)}</button>
        <button class="btn btn-small" id="ov-token-copy" aria-label="Copy API key" title="Copy key">${icon("copy", 14)} Copy</button>
        <span class="spacer"></span>
        <button class="btn btn-small" id="ov-token-rotate" title="Create a new key (invalidates the old one)">${icon("key", 14)} New key…</button>
        <button class="btn btn-small" id="ov-token-custom" aria-label="Set a custom API key" title="Set a custom key">${icon("pencil", 14)}</button>
      </div>
      <div class="hint" id="ov-key-open-hint" hidden>
        <span>ⓘ</span>
        <span>Open mode is active: clients connect without any key. Most SDKs still require a non-empty value — any placeholder works.</span>
      </div>
    </section>

    <div class="grid-2">
      <section class="card" aria-label="Access">
        <h2>Access</h2>
        <p class="card-caption">Changes apply immediately and restart a running relay.</p>

        <span class="control-label" id="ov-auth-label">Authentication</span>
        <div class="segmented" role="group" aria-labelledby="ov-auth-label">
          <button id="ov-auth-protected">Protected (API key)</button>
          <button id="ov-auth-open">Open (no key)</button>
        </div>

        <div style="height:12px"></div>

        <span class="control-label" id="ov-net-label">Who can connect</span>
        <div class="segmented" role="group" aria-labelledby="ov-net-label">
          <button id="ov-net-loopback">This machine only</button>
          <button id="ov-net-lan">Devices on my network</button>
        </div>

        <div class="hint danger" id="ov-open-lan-warning" hidden>
          <span>⚠</span>
          <span><strong>Open + network access:</strong> anyone on your network can use the relay — and your subscription quota — without a key.</span>
        </div>
        <div class="hint warn" id="ov-open-warning" hidden>
          <span>⚠</span>
          <span>Without a key, any program on this machine can use the relay and your subscription quota.</span>
        </div>
        <div class="hint" id="ov-claude-note" hidden>
          <span>ⓘ</span>
          <span>The experimental Claude provider only works in "This machine only" mode; it stays off while network access is on.</span>
        </div>
      </section>

      <section class="card" aria-label="Providers">
        <h2>Providers</h2>
        <p class="card-caption">The subscriptions this relay can route requests to.</p>
        <div id="ov-providers"><div class="empty">Start the relay to see provider status.</div></div>
        <div class="row" style="margin-top:12px">
          <button class="btn" id="ov-login-openai">${icon("logIn")} OpenAI</button>
          <button class="btn" id="ov-login-claude" hidden>${icon("logIn")} Claude</button>
          <span class="spacer"></span>
          <button class="btn" id="ov-doctor" title="Verifies config, sign-ins, and upstream connectivity; report in the Console tab.">${icon("checkCircle")} Check Setup</button>
        </div>
      </section>
    </div>

    <section class="card" aria-label="Usage">
      <div class="row">
        <h2 style="margin:0">Usage</h2>
        <span class="spacer"></span>
        <button class="btn btn-small" id="ov-usage-refresh" aria-label="Refresh usage" title="Refresh">${icon("refresh", 14)}</button>
      </div>
      <p class="card-caption">Subscription usage for the signed-in account.</p>
      <div id="ov-usage"><div class="empty">Start the relay, then refresh to load usage.</div></div>
    </section>

    <dialog id="ov-rotate-dialog">
      <h3>Create a new API key?</h3>
      <p class="dialog-text">
        The current key stops working immediately. Every app where you pasted
        it will need the new key.
      </p>
      <div class="row" style="margin-top:14px">
        <span class="spacer"></span>
        <button class="btn" id="ov-rotate-cancel">Cancel</button>
        <button class="btn btn-primary" id="ov-rotate-confirm">Create new key</button>
      </div>
    </dialog>

    <dialog id="ov-token-dialog">
      <h3>Set a custom API key</h3>
      <div class="field">
        <label for="ov-token-input">API key</label>
        <input type="password" id="ov-token-input" autocomplete="off" />
      </div>
      <div class="row" style="margin-top:14px">
        <span class="spacer"></span>
        <button class="btn" id="ov-token-cancel">Cancel</button>
        <button class="btn btn-primary" id="ov-token-save">Save key</button>
      </div>
    </dialog>
  `;
}

function el(id) {
  return root.querySelector(`#${id}`);
}

async function fetchToken() {
  return call(api.tokenAction("show"), "Could not read the API key");
}

function setRevealed(token) {
  revealedToken = token;
  const value = el("ov-token-value");
  const toggle = el("ov-token-toggle");
  if (token) {
    value.textContent = token;
    toggle.innerHTML = icon("eyeOff", 14);
    toggle.setAttribute("aria-label", "Hide API key");
    toggle.title = "Hide";
  } else {
    value.textContent = "••••••••••••••••";
    toggle.innerHTML = icon("eye", 14);
    toggle.setAttribute("aria-label", "Show API key");
    toggle.title = "Show";
  }
}

function bindActions(ctx) {
  el("ov-start").addEventListener("click", () => call(api.startRelay(), "Start failed"));
  el("ov-stop").addEventListener("click", () => call(api.stopRelay(), "Stop failed"));
  el("ov-restart").addEventListener("click", () => call(api.restartRelay(), "Restart failed"));

  el("ov-auth-protected").addEventListener("click", () => call(api.setAuthMode(true), "Change failed"));
  el("ov-auth-open").addEventListener("click", () => call(api.setAuthMode(false), "Change failed"));
  el("ov-net-loopback").addEventListener("click", () => call(api.setNetworkExposure(false), "Change failed"));
  el("ov-net-lan").addEventListener("click", () => call(api.setNetworkExposure(true), "Change failed"));

  el("ov-token-toggle").addEventListener("click", async () => {
    if (revealedToken) {
      setRevealed(null);
      return;
    }
    const token = await fetchToken();
    if (token) setRevealed(token);
  });
  el("ov-token-copy").addEventListener("click", async () => {
    const token = revealedToken ?? (await fetchToken());
    if (token) await copyText(token, "API key copied");
  });

  el("ov-token-rotate").addEventListener("click", () => el("ov-rotate-dialog").showModal());
  el("ov-rotate-cancel").addEventListener("click", () => el("ov-rotate-dialog").close());
  el("ov-rotate-confirm").addEventListener("click", async () => {
    el("ov-rotate-dialog").close();
    const token = await call(api.tokenAction("rotate"), "Key rotation failed");
    if (token) {
      setRevealed(token);
      toast("New API key created", "Copy it into your apps — the old key no longer works.", "success");
    }
  });

  el("ov-token-custom").addEventListener("click", () => el("ov-token-dialog").showModal());
  el("ov-token-cancel").addEventListener("click", () => el("ov-token-dialog").close());
  el("ov-token-save").addEventListener("click", async () => {
    const input = el("ov-token-input");
    const saved = await call(api.setCustomToken(input.value), "Key save failed");
    if (saved !== undefined) {
      toast("API key saved", "", "success");
      input.value = "";
      setRevealed(null);
      el("ov-token-dialog").close();
    }
  });

  el("ov-login-openai").addEventListener("click", async () => {
    toast("OpenAI sign-in started", "A browser window should open. Progress appears in the Console tab.");
    const done = await call(api.runLogin("openai"), "OpenAI sign-in failed");
    if (done !== undefined) {
      toast("OpenAI sign-in finished", "", "success");
      loadUsage();
    }
  });
  el("ov-login-claude").addEventListener("click", async () => {
    toast("Claude sign-in started", "The Claude CLI handles the sign-in. Progress appears in the Console tab.");
    const done = await call(api.runLogin("claude"), "Claude sign-in failed");
    if (done !== undefined) {
      toast("Claude sign-in finished", "", "success");
    }
  });
  el("ov-doctor").addEventListener("click", async () => {
    toast("Checking setup", "This can take a few seconds…");
    const allPassed = await call(api.runDoctor(false), "Setup check could not run");
    if (allPassed === true) {
      toast("Setup check passed", "Everything looks good.", "success");
    } else if (allPassed === false) {
      toast(
        "Setup check found problems",
        "Open the Console tab for the full report. A reached usage limit shows here too — that is an upstream quota, not a setup fault.",
        "error"
      );
    }
  });
  el("ov-usage-refresh").addEventListener("click", loadUsage);
}

async function loadUsage() {
  if (!root) return;
  const container = el("ov-usage");
  container.innerHTML = `<div class="empty">Loading…</div>`;
  let usage;
  try {
    usage = await api.getUsage();
  } catch (error) {
    container.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = String(error);
    container.appendChild(empty);
    return;
  }
  if (!root) return;
  renderUsage(container, usage);
}

function formatDuration(seconds) {
  if (seconds == null) return null;
  const units = [
    [86400, "d"],
    [3600, "h"],
    [60, "m"],
  ];
  const parts = [];
  let rest = Math.max(0, Math.floor(seconds));
  for (const [size, label] of units) {
    if (rest >= size) {
      parts.push(`${Math.floor(rest / size)}${label}`);
      rest %= size;
    }
    if (parts.length === 2) break;
  }
  return parts.length > 0 ? parts.join(" ") : "<1m";
}

function usageWindowRow(label, window) {
  const row = document.createElement("div");
  row.className = "usage-row";
  const name = document.createElement("span");
  name.className = "usage-label";
  const windowLength = formatDuration(window.limit_window_seconds);
  name.textContent = windowLength ? `${label} (${windowLength} window)` : label;

  const bar = document.createElement("div");
  bar.className = "usage-bar";
  const fill = document.createElement("div");
  fill.className = "usage-fill";
  const used = Math.max(0, Math.min(100, window.used_percent ?? 0));
  fill.style.width = `${used}%`;
  if (used >= 100) fill.classList.add("full");
  else if (used >= 80) fill.classList.add("high");
  bar.appendChild(fill);

  const detail = document.createElement("span");
  detail.className = "usage-detail";
  const resets = formatDuration(window.reset_after_seconds);
  detail.textContent =
    `${used.toFixed(0)}% used` + (resets ? ` · resets in ${resets}` : "");

  row.append(name, bar, detail);
  return row;
}

function renderUsage(container, usage) {
  container.innerHTML = "";
  const account = usage?.account ?? {};
  const header = document.createElement("div");
  header.className = "row";
  const who = document.createElement("span");
  who.className = "provider-name";
  who.style.width = "auto";
  who.textContent = [account.email, account.plan_type].filter(Boolean).join(" · ") || "OpenAI account";
  header.appendChild(who);
  const reachedType = usage?.rate_limit_reached_type;
  if (reachedType) {
    const badge = document.createElement("span");
    badge.className = "badge badge-bad";
    badge.textContent = "Usage limit reached";
    header.appendChild(badge);
  }
  container.appendChild(header);

  const limits = usage?.rate_limits ?? {};
  const windows = [];
  const defaultLimit = limits.default;
  if (defaultLimit?.primary_window) windows.push(["Requests", defaultLimit.primary_window]);
  if (defaultLimit?.secondary_window) windows.push(["Requests", defaultLimit.secondary_window]);
  for (const extra of limits.additional ?? []) {
    const label = extra.limit_name || extra.metered_feature || "Other";
    if (extra.rate_limit?.primary_window) windows.push([label, extra.rate_limit.primary_window]);
    if (extra.rate_limit?.secondary_window) windows.push([label, extra.rate_limit.secondary_window]);
  }
  if (windows.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No usage windows reported by the upstream.";
    container.appendChild(empty);
    return;
  }
  for (const [label, window] of windows) {
    container.appendChild(usageWindowRow(label, window));
  }
  const note = document.createElement("p");
  note.className = "card-caption";
  note.style.marginTop = "8px";
  note.textContent = "Claude does not expose usage figures through its CLI, so only OpenAI usage is shown.";
  container.appendChild(note);
}

function render(state) {
  if (!root) return;
  if (!state) {
    el("ov-state").textContent = "App backend unavailable";
    el("ov-dot").className = "dot dot-bad";
    return;
  }

  const dot = el("ov-dot");
  const stateLabel = el("ov-state");
  if (state.reachable) {
    dot.className = "dot dot-good";
    stateLabel.textContent = "Running";
  } else if (state.auth_mismatch) {
    dot.className = "dot dot-warn";
    stateLabel.textContent = "Running — key mismatch";
  } else if (state.lifecycle === "starting") {
    dot.className = "dot dot-warn";
    stateLabel.textContent = "Starting…";
  } else if (state.lifecycle === "failed") {
    dot.className = "dot dot-bad";
    stateLabel.textContent = "Failed — see Console tab";
  } else {
    dot.className = "dot dot-neutral";
    stateLabel.textContent = "Stopped";
  }

  el("ov-start").disabled = state.managed;
  el("ov-stop").disabled = !state.managed;
  el("ov-restart").disabled = !state.managed;
  el("ov-unmanaged").hidden = !(state.reachable && !state.managed);
  el("ov-mismatch").hidden = !state.auth_mismatch;

  renderEndpoints(state);

  const requireAuth = state.settings.requireBearerAuth;
  const loopback = ["127.0.0.1", "localhost", "::1"].includes(state.settings.host);

  setSegment("ov-auth-protected", requireAuth);
  setSegment("ov-auth-open", !requireAuth);
  setSegment("ov-net-loopback", loopback);
  setSegment("ov-net-lan", !loopback);

  el("ov-open-lan-warning").hidden = requireAuth || loopback;
  el("ov-open-warning").hidden = requireAuth || !loopback;
  el("ov-claude-note").hidden = loopback || !state.settings.enableClaudeExperimental;

  // Key row: masked value only meaningful in protected mode.
  el("ov-key-row").hidden = !requireAuth;
  el("ov-key-open-hint").hidden = requireAuth;

  // Claude sign-in only matters when the provider is enabled at all.
  el("ov-login-claude").hidden = !state.settings.enableClaudeExperimental;

  renderProviders(state);
}

function setSegment(id, selected) {
  const button = el(id);
  button.classList.toggle("selected", selected);
  button.setAttribute("aria-pressed", String(selected));
}

function endpointLine(tag, url) {
  const line = document.createElement("div");
  line.className = "endpoint";
  const tagEl = document.createElement("span");
  tagEl.className = "tag";
  tagEl.textContent = tag;
  const urlEl = document.createElement("span");
  urlEl.textContent = url;
  const copy = document.createElement("button");
  copy.className = "copy-btn";
  copy.innerHTML = icon("copy", 13);
  copy.setAttribute("aria-label", `Copy ${url}`);
  copy.title = "Copy URL";
  copy.addEventListener("click", () => copyText(url, "Base URL copied"));
  line.append(tagEl, urlEl, copy);
  return line;
}

function renderEndpoints(state) {
  const endpoints = [["local", state.local_endpoint]].concat(
    state.lan_endpoints.map((endpoint) => ["LAN", endpoint])
  );
  const cacheKey = JSON.stringify(endpoints);
  if (cacheKey === renderCache.endpoints) {
    return;
  }
  renderCache.endpoints = cacheKey;
  for (const containerId of ["ov-endpoints", "ov-connect-endpoints"]) {
    const container = el(containerId);
    container.innerHTML = "";
    for (const [tag, url] of endpoints) {
      container.appendChild(endpointLine(containerId === "ov-connect-endpoints" ? `${tag} URL` : tag, url));
    }
  }
}

function renderProviders(state) {
  const providers = state.relay_status?.providers;
  const cacheKey = JSON.stringify(providers ?? null);
  if (cacheKey === renderCache.providers) {
    return;
  }
  renderCache.providers = cacheKey;

  const container = el("ov-providers");
  if (!providers) {
    container.innerHTML = `<div class="empty">Start the relay to see provider status.</div>`;
    return;
  }
  container.innerHTML = "";
  const displayNames = { openai: "OpenAI", claude: "Claude" };
  for (const [name, info] of Object.entries(providers)) {
    const row = document.createElement("div");
    row.className = "provider-row";
    const nameEl = document.createElement("span");
    nameEl.className = "provider-name";
    nameEl.textContent = displayNames[name] ?? name;
    const enabled = document.createElement("span");
    enabled.className = `badge ${info.enabled ? "badge-good" : "badge-neutral"}`;
    enabled.textContent = info.enabled ? "Enabled" : "Disabled";
    const ready = document.createElement("span");
    ready.className = `badge ${info.ready_for_requests ? "badge-good" : "badge-warn"}`;
    ready.textContent = info.ready_for_requests ? "Ready" : "Not ready";
    const detail = document.createElement("span");
    detail.className = "provider-detail";
    detail.textContent = providerDetail(name, info);
    row.append(nameEl, enabled, ready, detail);
    container.appendChild(row);
  }
}

function providerDetail(name, info) {
  const parts = [info.email, info.plan_type, info.cli_version].filter(Boolean);
  if (parts.length > 0) {
    return parts.join("  ·  ");
  }
  if (info.enabled && !info.ready_for_requests) {
    return name === "openai"
      ? "Not signed in — use the OpenAI sign-in button below"
      : "Not signed in — use the Claude sign-in button below";
  }
  return info.enabled ? "" : "Turned off";
}
