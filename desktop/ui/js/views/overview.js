// Overview: relay state, connect-your-app credentials, accounts (identity,
// status and usage merged in one card), access modes.

import { api, call, copyText, toast } from "../api.js";
import { icon } from "../icons.js";

let root = null;
let lastState = null;
let renderCache = { endpoints: "", accounts: "" };
// Fetched on demand, never polled; cleared when hidden again.
let revealedToken = null;
// Emails with an in-flight sign-out, for optimistic row dimming.
const pendingLogout = new Set();
let pendingLogoutEmail = null;

let usageLoadedOnce = false;
// Last usage payload, keyed by account email, merged into the Accounts card.
let usageByEmail = new Map();
let usageStamp = 0;

export const overviewView = {
  async mount(container, ctx) {
    root = document.createElement("div");
    root.innerHTML = template();
    container.appendChild(root);
    bindActions(ctx);
    usageLoadedOnce = false;
    lastState = ctx.getState();
    render(lastState);
  },
  async update(state) {
    lastState = state;
    render(state);
    // Load usage automatically once the relay is reachable.
    if (state?.reachable && !usageLoadedOnce) {
      usageLoadedOnce = true;
      loadUsage();
    }
  },
  unmount() {
    root = null;
    lastState = null;
    revealedToken = null;
    renderCache = { endpoints: "", accounts: "" };
    pendingLogout.clear();
    pendingLogoutEmail = null;
    usageByEmail = new Map();
    usageStamp = 0;
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
          <div class="hero-endpoint" id="ov-hero-endpoint"></div>
        </div>
        <span class="spacer"></span>
        <button class="btn btn-ghost" id="ov-doctor" title="Verifies config, sign-ins, and upstream connectivity; report in the Console tab.">${icon("checkCircle", 14)} Check Setup</button>
        <button class="btn btn-primary" id="ov-start">${icon("play", 14)} Start</button>
        <button class="btn" id="ov-stop">${icon("stop", 14)} Stop</button>
        <button class="btn" id="ov-restart">${icon("restart", 14)} Restart</button>
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
      <div class="row" id="ov-key-row" style="margin-top:8px">
        <span class="tag-inline">API key</span>
        <code class="token-value" id="ov-token-value" aria-live="polite">••••••••••••••••</code>
        <button class="btn btn-small" id="ov-token-toggle" aria-label="Show API key" title="Show / hide">${icon("eye", 13)}</button>
        <button class="btn btn-small" id="ov-token-copy" aria-label="Copy API key" title="Copy key">${icon("copy", 13)} Copy</button>
        <span class="spacer"></span>
        <button class="btn btn-small btn-ghost" id="ov-token-rotate" title="Create a new key (invalidates the old one)">${icon("key", 13)} New key…</button>
        <button class="btn btn-small btn-ghost" id="ov-token-custom" aria-label="Set a custom API key" title="Set a custom key">${icon("pencil", 13)}</button>
      </div>
      <div class="hint" id="ov-key-open-hint" hidden>
        <span>ⓘ</span>
        <span>Open mode is active: clients connect without any key. Most SDKs still require a non-empty value — any placeholder works.</span>
      </div>
    </section>

    <section class="card" aria-label="Accounts">
      <div class="row">
        <h2 style="margin:0">Accounts</h2>
        <span class="spacer"></span>
        <button class="btn btn-small btn-ghost" id="ov-refresh" title="Re-check limits and reload usage">${icon("refresh", 13)} Refresh</button>
      </div>
      <div id="ov-accounts"><div class="empty">Start the relay to see accounts.</div></div>
      <div class="card-actions">
        <div class="split-btn">
          <button class="btn" id="ov-login-openai">${icon("logIn", 14)} OpenAI</button>
          <button class="btn split-btn-toggle" id="ov-login-openai-menu" aria-label="Choose sign-in method" aria-haspopup="true">${icon("chevronDown", 13)}</button>
          <div class="split-menu" id="ov-login-menu" hidden role="menu">
            <button role="menuitem" data-method="browser">In a browser (this machine)</button>
            <button role="menuitem" data-method="device">With a code (any device)</button>
          </div>
        </div>
        <button class="btn" id="ov-login-claude" hidden>${icon("logIn", 14)} Claude</button>
      </div>
      <div class="login-banner" id="ov-login-banner" hidden>
        <div class="row">
          <span class="dot dot-warn"></span>
          <strong>Waiting for sign-in…</strong>
          <span class="spacer"></span>
          <button class="btn btn-small" id="ov-login-open">${icon("logIn", 13)} Open page</button>
          <button class="btn btn-small" id="ov-login-copy">${icon("copy", 13)} Copy URL</button>
        </div>
        <div class="row" id="ov-login-code-row" hidden style="margin-top:8px">
          <span class="control-label" style="margin:0">Enter this code:</span>
          <code class="login-code" id="ov-login-code"></code>
          <button class="btn btn-small" id="ov-login-code-copy" aria-label="Copy code">${icon("copy", 13)}</button>
        </div>
        <div class="login-url" id="ov-login-url"></div>
        <p class="card-caption" style="margin:6px 0 0" id="ov-login-hint-browser">
          If no browser opened, copy this URL into the browser profile of
          the account you want to sign in with.
        </p>
        <p class="card-caption" style="margin:6px 0 0" id="ov-login-hint-device" hidden>
          Open the URL in a browser on any device (phone or laptop) and
          enter the code to approve the sign-in.
        </p>
      </div>
    </section>

    <section class="card" aria-label="Access">
      <h2>Access</h2>
      <p class="card-caption">Changes apply immediately and restart a running relay.</p>
      <div class="row" style="gap:28px">
        <div class="control-group">
          <span class="control-label" id="ov-auth-label">Authentication</span>
          <div class="segmented" role="group" aria-labelledby="ov-auth-label">
            <button id="ov-auth-protected">Protected (API key)</button>
            <button id="ov-auth-open">Open (no key)</button>
          </div>
        </div>
        <div class="control-group">
          <span class="control-label" id="ov-net-label">Who can connect</span>
          <div class="segmented" role="group" aria-labelledby="ov-net-label">
            <button id="ov-net-loopback">This machine only</button>
            <button id="ov-net-lan">Devices on my network</button>
          </div>
        </div>
      </div>
      <div class="hint warn" id="ov-open-lan-warning" hidden>
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

    <dialog id="ov-logout-dialog">
      <h3 id="ov-logout-title">Sign out?</h3>
      <p class="dialog-text" id="ov-logout-text"></p>
      <div class="row" style="margin-top:14px">
        <span class="spacer"></span>
        <button class="btn" id="ov-logout-cancel">Cancel</button>
        <button class="btn btn-danger" id="ov-logout-confirm">Sign out</button>
      </div>
    </dialog>

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
    toggle.innerHTML = icon("eyeOff", 13);
    toggle.setAttribute("aria-label", "Hide API key");
    toggle.title = "Hide";
  } else {
    value.textContent = "••••••••••••••••";
    toggle.innerHTML = icon("eye", 13);
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

  el("ov-login-openai").addEventListener("click", () => startOpenAiSignIn());
  el("ov-login-openai-menu").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = el("ov-login-menu");
    menu.hidden = !menu.hidden;
  });
  root.querySelectorAll("#ov-login-menu button").forEach((item) => {
    item.addEventListener("click", async () => {
      el("ov-login-menu").hidden = true;
      await call(api.setLoginMethod(item.dataset.method), "Change failed");
      startOpenAiSignIn();
    });
  });
  // Close the method menu on any outside click.
  document.addEventListener("click", () => {
    const menu = root?.querySelector("#ov-login-menu");
    if (menu) menu.hidden = true;
  });

  // Sign-out confirm dialog wiring.
  el("ov-logout-cancel").addEventListener("click", () => el("ov-logout-dialog").close());
  el("ov-logout-confirm").addEventListener("click", async () => {
    const email = pendingLogoutEmail;
    el("ov-logout-dialog").close();
    if (!email) return;
    pendingLogout.add(email);
    renderCache.accounts = ""; // force a redraw showing the dimmed row
    render(ctx.getState());
    const done = await call(api.logoutAccount(email), "Sign out failed");
    pendingLogout.delete(email);
    if (done !== undefined) {
      toast("Signed out", email, "success");
    }
    renderCache.accounts = "";
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
  // One refresh: clears usage-limit holds on the relay, then reloads usage.
  el("ov-refresh").addEventListener("click", async () => {
    const openaiReady = lastState?.relay_status?.providers?.openai?.ready_for_requests;
    if (lastState?.reachable && openaiReady) {
      await call(api.refreshAccounts(), "Refresh failed");
    }
    loadUsage();
  });

  el("ov-login-copy").addEventListener("click", () => {
    const url = el("ov-login-url").textContent;
    if (url) copyText(url, "Sign-in URL copied");
  });
  el("ov-login-open").addEventListener("click", () => {
    const url = el("ov-login-url").textContent;
    if (url) call(api.openPath(url), "Cannot open browser");
  });
  el("ov-login-code-copy").addEventListener("click", () => {
    const code = el("ov-login-code").textContent;
    if (code) copyText(code, "Code copied");
  });
}

async function startOpenAiSignIn() {
  toast("OpenAI sign-in started", "Follow the prompt below or in your browser; progress appears in the Console tab.");
  const done = await call(api.runLogin("openai"), "OpenAI sign-in failed");
  if (done !== undefined) {
    toast(
      "OpenAI account ready",
      "It appears above within a few seconds and joins load balancing automatically.",
      "success"
    );
    loadUsage();
  }
}

function openLogoutDialog(email, isLast) {
  pendingLogoutEmail = email;
  root.querySelector("#ov-logout-title").textContent = `Sign out ${email}?`;
  root.querySelector("#ov-logout-text").textContent = isLast
    ? "Requests will fail until you sign in again. This removes the stored sign-in from this machine; your OpenAI account itself is unaffected."
    : "Requests will use your remaining account(s). This removes the stored sign-in from this machine; your OpenAI account itself is unaffected.";
  root.querySelector("#ov-logout-dialog").showModal();
}

async function loadUsage() {
  if (!root) return;
  let usage;
  try {
    usage = await api.getUsage();
  } catch {
    usage = null; // account rows still render, just without bars
  }
  if (!root) return;
  usageByEmail = new Map();
  if (Array.isArray(usage?.accounts)) {
    for (const entry of usage.accounts) {
      usageByEmail.set(entry.email ?? entry.slug, entry);
    }
  } else if (usage) {
    // Single-account shape: the payload itself is the status.
    usageByEmail.set(usage?.account?.email ?? "", { status: usage });
  }
  usageStamp++;
  renderCache.accounts = "";
  if (lastState) render(lastState);
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

// The normalized relay payload labels each window ("5h", "weekly", "30d").
function windowLabel(window, fallback) {
  const label = window.window_label ?? formatDuration(window.window_seconds ?? window.limit_window_seconds);
  if (!label) return fallback;
  return label === "weekly" ? "Weekly" : `${label} window`;
}

function usageWindowRow(label, window) {
  const row = document.createElement("div");
  row.className = "usage-row";
  const name = document.createElement("span");
  name.className = "usage-label";
  name.textContent = label;

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

// Flattens a normalized subscription-status payload into labeled windows.
function usageWindows(status) {
  const limits = status?.rate_limits ?? {};
  const windows = [];
  const push = (window, baseLabel) => {
    if (window) windows.push([windowLabel(window, baseLabel), window]);
  };
  push(limits.default?.primary_window, "Requests");
  push(limits.default?.secondary_window, "Requests");
  for (const extra of limits.additional ?? []) {
    const name = extra.limit_name || extra.metered_feature || "Other";
    if (extra.rate_limit?.primary_window) {
      windows.push([`${name} · ${windowLabel(extra.rate_limit.primary_window, "")}`.replace(/ · $/, ""), extra.rate_limit.primary_window]);
    }
    if (extra.rate_limit?.secondary_window) {
      windows.push([`${name} · ${windowLabel(extra.rate_limit.secondary_window, "")}`.replace(/ · $/, ""), extra.rate_limit.secondary_window]);
    }
  }
  return windows;
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

  // Once one account works, the same button adds another of the user's own
  // accounts (the CLI guard makes a repeat sign-in additive, not destructive).
  const openaiReady = state.relay_status?.providers?.openai?.ready_for_requests;
  el("ov-login-openai").innerHTML = openaiReady
    ? `${icon("logIn", 14)} Add account`
    : `${icon("logIn", 14)} Sign in to OpenAI`;

  // Sign-in in progress: surface the URL (and pairing code) for copying.
  const banner = el("ov-login-banner");
  banner.hidden = !state.login_url;
  if (state.login_url) {
    el("ov-login-url").textContent = state.login_url;
    const hasCode = Boolean(state.login_code);
    el("ov-login-code-row").hidden = !hasCode;
    if (hasCode) {
      el("ov-login-code").textContent = state.login_code;
    }
    el("ov-login-hint-browser").hidden = hasCode;
    el("ov-login-hint-device").hidden = !hasCode;
  }

  renderAccounts(state);
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
  const endpoints = [["Local", state.local_endpoint]].concat(
    state.lan_endpoints.map((endpoint) => ["LAN", endpoint])
  );
  const cacheKey = JSON.stringify(endpoints);
  if (cacheKey === renderCache.endpoints) {
    return;
  }
  renderCache.endpoints = cacheKey;
  // One compact line in the hero; the full copyable list lives only in
  // "Connect Your App" — no duplication.
  el("ov-hero-endpoint").textContent = state.local_endpoint;
  const container = el("ov-connect-endpoints");
  container.innerHTML = "";
  for (const [tag, url] of endpoints) {
    container.appendChild(endpointLine(tag, url));
  }
}

function accountStatusBadge(account, index, total) {
  // One consolidated badge (precedence): Not ready > Limit > Active > Standby.
  // A reached quota that resets on schedule is normal operation → amber, not
  // red; red is reserved for real failures (relay down, auth broken).
  const badge = document.createElement("span");
  if (!account.ready_for_requests && !account.limited) {
    badge.className = "badge badge-warn";
    badge.textContent = "Not ready";
  } else if (account.limited) {
    badge.className = "badge badge-warn";
    const resets = account.limited_for_seconds
      ? ` · ${formatDuration(account.limited_for_seconds)}`
      : "";
    badge.textContent = `At limit${resets}`;
    badge.title = account.limited_for_seconds
      ? `Usage limit reached; back in rotation in ${formatDuration(account.limited_for_seconds)}.`
      : "Usage limit reached.";
  } else if (index === 0) {
    badge.className = "badge badge-accent";
    badge.textContent = total > 1 ? "Active" : "Ready";
  } else {
    badge.className = "badge badge-neutral";
    badge.textContent = "Standby";
  }
  return badge;
}

function signOutButton(email, isLast) {
  const button = document.createElement("button");
  button.className = "copy-btn";
  button.innerHTML = icon("logOut", 14);
  button.setAttribute("aria-label", `Sign out ${email}`);
  button.title = `Sign out ${email}`;
  button.addEventListener("click", () => openLogoutDialog(email, isLast));
  return button;
}

// One block per account: identity header (fixed grid) + usage bars.
function accountBlock(account, index, total) {
  const email = account.email ?? account.slug;
  const block = document.createElement("div");
  block.className = "account-block";
  if (pendingLogout.has(email)) block.classList.add("pending");

  const head = document.createElement("div");
  head.className = "account-head";
  const emailEl = document.createElement("span");
  emailEl.className = "account-email";
  emailEl.textContent = email;
  emailEl.title = email;
  const plan = document.createElement("span");
  plan.className = "account-plan";
  plan.textContent = account.plan_type ?? "";
  head.append(emailEl, plan, accountStatusBadge(account, index, total));
  if (pendingLogout.has(email)) {
    const pending = document.createElement("span");
    pending.className = "account-plan";
    pending.textContent = "…";
    head.append(pending);
  } else {
    head.append(signOutButton(email, total === 1));
  }
  block.appendChild(head);

  const usageEntry = usageByEmail.get(email);
  if (usageEntry?.status) {
    for (const [label, window] of usageWindows(usageEntry.status)) {
      block.appendChild(usageWindowRow(label, window));
    }
  } else if (usageEntry?.error) {
    const err = document.createElement("div");
    err.className = "account-plan";
    err.textContent = "Usage unavailable";
    err.title = usageEntry.error;
    block.appendChild(err);
  }
  return block;
}

function renderAccounts(state) {
  const providers = state.relay_status?.providers;
  const cacheKey =
    JSON.stringify(providers ?? null) + "|" + usageStamp + "|" + [...pendingLogout].join(",");
  if (cacheKey === renderCache.accounts) {
    return;
  }
  renderCache.accounts = cacheKey;

  const container = el("ov-accounts");
  if (!providers) {
    container.innerHTML = `<div class="empty">Start the relay to see accounts.</div>`;
    return;
  }
  container.innerHTML = "";

  const openai = providers.openai;
  // The status endpoint reports a lone account at the top level; the
  // accounts array only appears with 2+ accounts.
  const accounts = Array.isArray(openai?.accounts) && openai.accounts.length > 0
    ? openai.accounts
    : openai?.enabled && openai?.email
      ? [{ email: openai.email, plan_type: openai.plan_type, ready_for_requests: openai.ready_for_requests, limited: false }]
      : [];

  if (openai?.enabled && accounts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No OpenAI account signed in yet — add one below.";
    container.appendChild(empty);
  }
  accounts.forEach((account, index) => {
    container.appendChild(accountBlock(account, index, accounts.length));
  });
  if (accounts.length > 1) {
    const caption = document.createElement("p");
    caption.className = "card-caption";
    caption.style.margin = "8px 0 0";
    caption.textContent = "Requests go to the first account with capacity.";
    container.appendChild(caption);
  }

  // Claude appears only when enabled: a disabled experimental provider is
  // configuration noise, not status.
  const claude = providers.claude;
  if (claude?.enabled) {
    const row = document.createElement("div");
    row.className = "provider-row";
    const nameEl = document.createElement("span");
    nameEl.className = "provider-name";
    nameEl.textContent = "Claude";
    const ready = document.createElement("span");
    ready.className = `badge ${claude.ready_for_requests ? "badge-accent" : "badge-warn"}`;
    ready.textContent = claude.ready_for_requests ? "Ready" : "Not ready";
    const detail = document.createElement("span");
    detail.className = "provider-detail";
    detail.textContent =
      [claude.email, claude.plan_type, claude.cli_version].filter(Boolean).join("  ·  ") ||
      (claude.ready_for_requests ? "" : "Not signed in — use the Claude button below");
    row.append(nameEl, ready, detail);
    container.appendChild(row);
  }
}
