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
// The sign-out the confirmation dialog is about: {provider, email}.
let pendingLogoutTarget = null;
let pendingClaudeLogout = false;
// Claude method chosen while the provider was off (network mode); resumed
// after the user confirms the mode switch.
let pendingClaudeMethod = null;
// The outside-click menu closer, removed on unmount.
let documentClickHandler = null;

let usageLoadedOnce = false;
// Last usage payload, keyed by account email, merged into the Accounts card.
let usageByEmail = new Map();
// Claude usage (same normalized shape as one OpenAI account's status).
let claudeUsage = null;
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
    if (documentClickHandler) {
      document.removeEventListener("click", documentClickHandler);
      documentClickHandler = null;
    }
    root = null;
    lastState = null;
    revealedToken = null;
    renderCache = { endpoints: "", accounts: "" };
    pendingLogout.clear();
    pendingLogoutTarget = null;
    pendingClaudeLogout = false;
    usageByEmail = new Map();
    claudeUsage = null;
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

    <section class="card" aria-label="OpenAI accounts">
      <div class="row">
        <h2 style="margin:0">OpenAI</h2>
        <span class="spacer"></span>
        <button class="btn btn-small btn-ghost" id="ov-refresh" title="Re-check limits and reload usage">${icon("refresh", 13)} Refresh</button>
        <div class="split-btn">
          <button class="btn btn-small" id="ov-login-openai">${icon("logIn", 13)} Sign in</button>
          <button class="btn btn-small split-btn-toggle" id="ov-login-openai-menu" aria-label="Choose OpenAI sign-in method" aria-haspopup="true">${icon("chevronDown", 12)}</button>
          <div class="split-menu" id="ov-login-menu" hidden role="menu">
            <button role="menuitem" data-method="browser">In a browser (this machine)</button>
            <button role="menuitem" data-method="device">With a code (any device)</button>
          </div>
        </div>
      </div>
      <div id="ov-accounts"><div class="empty">Start the relay to see accounts.</div></div>
    </section>

    <section class="card" id="ov-claude-section" aria-label="Claude account" hidden>
      <div class="row">
        <h2 style="margin:0">Claude</h2>
        <span class="badge badge-warn" id="ov-claude-off-badge" hidden>Off in network mode</span>
        <span class="spacer"></span>
        <button class="btn btn-small btn-ghost" id="ov-refresh-claude" title="Reload Claude usage">${icon("refresh", 13)} Refresh</button>
        <div class="split-btn">
          <button class="btn btn-small" id="ov-login-claude">${icon("logIn", 13)} Sign in</button>
          <button class="btn btn-small split-btn-toggle" id="ov-login-claude-menu" aria-label="Choose Claude sign-in method" aria-haspopup="true">${icon("chevronDown", 12)}</button>
          <div class="split-menu" id="ov-claude-menu" hidden role="menu">
            <button role="menuitem" data-method="browser">In a browser (this machine)</button>
            <button role="menuitem" data-method="token">With a token (any device)</button>
          </div>
        </div>
      </div>
      <div id="ov-claude-account"></div>
    </section>

    <section class="card" aria-label="Sign-in progress" id="ov-login-card" hidden>
      <div class="login-banner" id="ov-login-banner">
        <div class="row">
          <span class="dot dot-warn"></span>
          <strong>Waiting for sign-in…</strong>
          <span class="spacer"></span>
          <button class="btn btn-small" id="ov-login-open">${icon("logIn", 13)} Open page</button>
          <button class="btn btn-small" id="ov-login-copy">${icon("copy", 13)} Copy URL</button>
          <button class="btn btn-small btn-ghost" id="ov-login-cancel">Cancel</button>
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
        <div class="row" id="ov-login-paste-row" hidden style="margin-top:8px">
          <input type="text" id="ov-login-paste-input" class="filter-input"
                 placeholder="Paste the code from the browser page…"
                 aria-label="Sign-in code" />
          <button class="btn btn-small" id="ov-login-paste-send">Submit code</button>
        </div>
      </div>
    </section>

    <section class="card" aria-label="Access">
      <h2>Access</h2>
      <p class="card-caption">Changes apply immediately and restart a running relay.</p>
      <div class="control-row">
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
        <span>The Claude provider only works in "This machine only" mode; it stays off while network access is on.</span>
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

    <dialog id="ov-claude-mode-dialog">
      <h3>Claude needs "This machine only" mode</h3>
      <p class="dialog-text">
        For security, the Claude provider only runs while the
        relay is not exposed to your network. Switching now restarts the
        relay on this machine only — <strong>devices on your network lose
        access</strong> until you switch back.
      </p>
      <div class="row" style="margin-top:14px">
        <span class="spacer"></span>
        <button class="btn" id="ov-claude-mode-cancel">Cancel</button>
        <button class="btn btn-primary" id="ov-claude-mode-switch">Switch &amp; continue</button>
      </div>
    </dialog>

    <dialog id="ov-claude-token-dialog">
      <h3>Sign in to Claude with a token</h3>
      <p class="dialog-text">
        On any machine with a browser, run <code>claude setup-token</code>
        in a terminal, approve the sign-in, then paste the token it prints.
      </p>
      <div class="field" style="margin-top:12px">
        <label for="ov-claude-token-input">Token</label>
        <input type="password" id="ov-claude-token-input" autocomplete="off"
               placeholder="sk-ant-oat…" />
      </div>
      <p class="dialog-text" style="margin-top:10px; font-size:12px">
        A stored token overrides the claude CLI's own sign-in for relay
        requests. Removing it keeps the CLI's sign-in; the account row's
        sign-out removes everything.
      </p>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-ghost" id="ov-claude-token-clear">Remove stored token</button>
        <span class="spacer"></span>
        <button class="btn" id="ov-claude-token-cancel">Cancel</button>
        <button class="btn btn-primary" id="ov-claude-token-save">Save token</button>
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
  el("ov-net-lan").addEventListener("click", async () => {
    const claudeWasOn = Boolean(lastState?.claude_effective);
    const done = await call(api.setNetworkExposure(true), "Change failed");
    // The reverse trap: switching to network mode silently pauses Claude.
    if (done !== undefined && claudeWasOn) {
      toast("Claude paused", "It stays off while network access is on.", "info");
    }
  });

  el("ov-token-toggle").addEventListener("click", async () => {
    if (revealedToken) {
      setRevealed(null);
      return;
    }
    const token = await fetchToken();
    if (token && root) setRevealed(token);
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
      // The toast must fire even if the view unmounted mid-rotation: the
      // old key is already invalid and the user has to know.
      if (root) setRevealed(token);
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
      if (!root) return;
      setRevealed(null);
      el("ov-token-dialog").close();
    }
  });

  el("ov-login-openai").addEventListener("click", () => startOpenAiSignIn());
  el("ov-login-openai-menu").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = el("ov-login-menu");
    el("ov-claude-menu").hidden = true;
    menu.hidden = !menu.hidden;
  });
  root.querySelectorAll("#ov-login-menu button").forEach((item) => {
    item.addEventListener("click", async () => {
      el("ov-login-menu").hidden = true;
      await call(api.setLoginMethod(item.dataset.method), "Change failed");
      startOpenAiSignIn();
    });
  });
  el("ov-login-claude").addEventListener("click", () => beginClaudeMethod("browser"));
  el("ov-login-claude-menu").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = el("ov-claude-menu");
    el("ov-login-menu").hidden = true;
    menu.hidden = !menu.hidden;
  });
  root.querySelectorAll("#ov-claude-menu button").forEach((item) => {
    item.addEventListener("click", () => {
      el("ov-claude-menu").hidden = true;
      beginClaudeMethod(item.dataset.method);
    });
  });
  el("ov-claude-mode-cancel").addEventListener("click", () => {
    pendingClaudeMethod = null;
    el("ov-claude-mode-dialog").close();
  });
  el("ov-claude-mode-switch").addEventListener("click", async () => {
    const method = pendingClaudeMethod;
    pendingClaudeMethod = null;
    const button = el("ov-claude-mode-switch");
    button.disabled = true;
    // Blocks through the relay restart; when it returns, loopback mode is
    // live and Claude is effective.
    const done = await call(api.setNetworkExposure(false), "Mode switch failed");
    button.disabled = false;
    if (!root) return;
    el("ov-claude-mode-dialog").close();
    if (done === undefined) return;
    toast("Switched to \u201CThis machine only\u201D", "Claude is available; network devices are disconnected.", "success");
    if (method) runClaudeMethod(method);
  });
  el("ov-claude-token-cancel").addEventListener("click", () => el("ov-claude-token-dialog").close());
  el("ov-claude-token-clear").addEventListener("click", async () => {
    const existed = await call(api.clearClaudeToken(), "Token removal failed");
    if (existed === undefined || !root) return;
    el("ov-claude-token-dialog").close();
    toast(
      existed ? "Stored token removed" : "No stored token",
      existed ? "The relay now uses the claude CLI's own sign-in." : "Nothing to remove.",
      existed ? "success" : "info"
    );
  });
  el("ov-claude-token-save").addEventListener("click", async () => {
    const input = el("ov-claude-token-input");
    const saved = await call(api.setClaudeToken(input.value), "Token save failed");
    if (saved !== undefined) {
      input.value = "";
      toast("Claude token saved", "The relay uses it on the next Claude request.", "success");
      if (root) el("ov-claude-token-dialog").close();
    }
  });
  // Close any open method menu on an outside click. Registered once per
  // mount and removed on unmount — accumulating document listeners across
  // view switches was a slow leak.
  documentClickHandler = () => {
    for (const id of ["ov-login-menu", "ov-claude-menu"]) {
      const menu = root?.querySelector(`#${id}`);
      if (menu) menu.hidden = true;
    }
  };
  document.addEventListener("click", documentClickHandler);

  // Sign-out confirm dialog wiring (shared by both providers).
  el("ov-logout-cancel").addEventListener("click", () => el("ov-logout-dialog").close());
  el("ov-logout-confirm").addEventListener("click", async () => {
    const target = pendingLogoutTarget;
    pendingLogoutTarget = null;
    el("ov-logout-dialog").close();
    if (!target) return;
    if (target.provider === "claude") {
      pendingClaudeLogout = true;
      renderCache.accounts = "";
      render(ctx.getState());
      const outcome = await call(api.logoutClaude(), "Claude sign-out failed");
      pendingClaudeLogout = false;
      renderCache.accounts = "";
      if (outcome !== undefined) {
        if (outcome.cli_signed_out) {
          toast("Signed out of Claude", "", "success");
        } else {
          // Partial: the stored token is gone but CLI credentials may
          // remain, so the relay could still answer Claude requests.
          toast(
            "Claude sign-out incomplete",
            `${outcome.cli_error ?? "The claude CLI sign-out failed."} Run \u201Cclaude auth logout\u201D in a terminal to finish.`,
            "error"
          );
        }
        loadUsage();
      }
      render(ctx.getState());
      return;
    }
    const email = target.email;
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
  // Refresh re-checks account capacity on the relay (releases are
  // evidence-gated there) and reloads usage. The button shows a busy
  // spinner for the several seconds the upstream probes take — without
  // it the action read as doing nothing.
  el("ov-refresh").addEventListener("click", () =>
    withRefreshSpinner(el("ov-refresh"), async () => {
      const openaiReady = lastState?.relay_status?.providers?.openai?.ready_for_requests;
      if (lastState?.reachable && openaiReady) {
        await call(api.refreshAccounts(), "Refresh failed");
      }
      await loadUsage();
    })
  );
  el("ov-refresh-claude").addEventListener("click", () =>
    withRefreshSpinner(el("ov-refresh-claude"), () => loadUsage())
  );

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
  el("ov-login-cancel").addEventListener("click", () => {
    call(api.cancelLogin(), "Cancel failed");
  });
  el("ov-login-paste-send").addEventListener("click", async () => {
    const input = el("ov-login-paste-input");
    const sent = await call(api.submitLoginCode(input.value), "Code delivery failed");
    if (sent !== undefined) {
      input.value = "";
      toast("Code submitted", "Finishing the sign-in…", "success");
    }
  });
}

// Entry point for both Claude methods: when the provider is off because
// network access is on, explain and offer the switch instead of hiding or
// silently no-op'ing (both previous iterations confused users).
function beginClaudeMethod(method) {
  if (lastState && !lastState.claude_effective) {
    pendingClaudeMethod = method;
    el("ov-claude-mode-dialog").showModal();
    return;
  }
  runClaudeMethod(method);
}

function runClaudeMethod(method) {
  if (method === "token") {
    el("ov-claude-token-dialog").showModal();
  } else {
    startClaudeSignIn();
  }
}

// Runs a sign-in; a deliberate cancel is informational, never an error.
async function runLoginFlow(provider, failTitle) {
  try {
    await api.runLogin(provider);
    return true;
  } catch (error) {
    const message = String(error);
    if (message.includes("cancelled")) {
      toast("Sign-in cancelled", "", "info");
    } else {
      toast(failTitle, message, "error");
    }
    return false;
  }
}

async function startClaudeSignIn() {
  toast(
    "Claude sign-in started",
    "A browser opens for the Anthropic sign-in. If the final page shows a code, paste it in the banner below."
  );
  if (await runLoginFlow("claude", "Claude sign-in failed")) {
    toast("Claude sign-in finished", "", "success");
  }
}

async function startOpenAiSignIn() {
  toast("OpenAI sign-in started", "Follow the prompt below or in your browser; progress appears in the Console tab.");
  if (await runLoginFlow("openai", "OpenAI sign-in failed")) {
    toast(
      "OpenAI account ready",
      "It appears above within a few seconds and joins load balancing automatically.",
      "success"
    );
    loadUsage();
  }
}

function openLogoutDialog(email, isLast) {
  pendingLogoutTarget = { provider: "openai", email };
  root.querySelector("#ov-logout-title").textContent = `Sign out ${email}?`;
  root.querySelector("#ov-logout-text").textContent = isLast
    ? "Requests will fail until you sign in again. This removes the stored sign-in from this machine; your OpenAI account itself is unaffected."
    : "Requests will use your remaining account(s). This removes the stored sign-in from this machine; your OpenAI account itself is unaffected.";
  root.querySelector("#ov-logout-dialog").showModal();
}

function openClaudeLogoutDialog() {
  pendingLogoutTarget = { provider: "claude", email: null };
  root.querySelector("#ov-logout-title").textContent = "Sign out of Claude?";
  root.querySelector("#ov-logout-text").textContent =
    "This signs the claude CLI out on this machine and removes any token stored in AIRelays. " +
    "Claude requests will fail until you sign in again, and other tools that use the claude CLI here " +
    "— including Claude Code — are signed out too. Your Anthropic account itself is unaffected.";
  root.querySelector("#ov-logout-dialog").showModal();
}

// Disables a refresh button and spins its icon while the async task runs,
// so slow upstream probes are visibly in progress instead of looking dead.
async function withRefreshSpinner(button, task) {
  if (button.disabled) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spin">${icon("refresh", 13)}</span> Refreshing…`;
  try {
    await task();
  } finally {
    button.innerHTML = original;
    button.disabled = false;
  }
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
  claudeUsage = usage?.claude ?? null;
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
  const detail = document.createElement("span");
  detail.className = "usage-detail";

  // No active window right now: the upstream anchors the 5h bucket at the
  // first request, so between windows there is nothing to measure yet.
  if (window.idle) {
    detail.textContent = "0% used · window starts with the next request";
    row.append(name, bar, detail);
    return row;
  }

  // A null used_percent means the window rolled over while we couldn't
  // reach the endpoint (stale snapshot): the old numbers are meaningless,
  // so show an empty bar and say so rather than fabricating "0% · <1m".
  if (window.used_percent == null) {
    detail.textContent = "awaiting fresh data";
    row.append(name, bar, detail);
    return row;
  }

  const fill = document.createElement("div");
  fill.className = "usage-fill";
  const used = Math.max(0, Math.min(100, window.used_percent));
  fill.style.width = `${used}%`;
  if (used >= 100) fill.classList.add("full");
  else if (used >= 80) fill.classList.add("high");
  bar.appendChild(fill);

  const resets =
    window.reset_after_seconds > 0 ? formatDuration(window.reset_after_seconds) : null;
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
  // Between 5h buckets the upstream reports no short window at all (it
  // anchors the bucket at the first request — the weekly window may even
  // arrive alone in the primary slot). Hiding the row read as a bug: show
  // an explicit idle 5h row whenever only long windows are present.
  const defaults = [limits.default?.primary_window, limits.default?.secondary_window]
    .filter(Boolean);
  const hasShortWindow = defaults.some(
    (w) => (w.window_seconds ?? w.limit_window_seconds ?? 0) < 86400
  );
  if (defaults.length > 0 && !hasShortWindow) {
    windows.push(["5h window", { idle: true }]);
  }
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
  } else if (state.lifecycle === "stopping") {
    dot.className = "dot dot-neutral";
    stateLabel.textContent = "Stopping…";
  } else if (state.lifecycle === "failed") {
    dot.className = "dot dot-bad";
    stateLabel.textContent = "Failed — see Console tab";
  } else if (state.managed) {
    // The child process is alive but the endpoint isn't answering the
    // health probe (overload, hang). "Stopped" here would be a lie.
    dot.className = "dot dot-warn";
    stateLabel.textContent = "Running — not responding";
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
  el("ov-claude-note").hidden = loopback || !state.settings.enableClaude;

  // Key row: masked value only meaningful in protected mode.
  el("ov-key-row").hidden = !requireAuth;
  el("ov-key-open-hint").hidden = requireAuth;

  // Claude section: always visible while the feature flag is on (hiding it
  // read as "the feature is gone"), with an explicit badge when the
  // provider is off because network access is on. Clicking sign-in while
  // off opens the mode-switch dialog instead of a sign-in that would do
  // nothing.
  el("ov-claude-section").hidden = !state.settings.enableClaude;
  el("ov-claude-off-badge").hidden = state.claude_effective;

  // Sign-in stays enabled while signed in: the claude CLI treats a repeat
  // sign-in as a credential refresh / account switch, both legitimate.
  const claudeSignedIn = Boolean(
    state.claude_effective && state.relay_status?.providers?.claude?.ready_for_requests
  );
  el("ov-login-claude").title = claudeSignedIn
    ? "Signing in again refreshes credentials or switches the account."
    : "";

  // Both sign-in buttons carry the provider name; repeated OpenAI sign-ins
  // are additive (the CLI guard keeps existing accounts), so one label
  // covers first sign-in and adding more.
  const openaiReady = state.relay_status?.providers?.openai?.ready_for_requests;
  el("ov-login-openai").title = openaiReady
    ? "Add another OpenAI account"
    : "Sign in to OpenAI";

  // Sign-in in progress: the banner appears as soon as the flow starts —
  // before the URL is printed it still offers Cancel (a flow stuck ahead
  // of its URL was previously uncancellable from the UI).
  const banner = el("ov-login-banner");
  const loginActive = Boolean(state.login_url) || state.login_running;
  banner.hidden = !loginActive;
  el("ov-login-card").hidden = !loginActive;
  if (loginActive) {
    const hasUrl = Boolean(state.login_url);
    el("ov-login-url").textContent = state.login_url ?? "";
    el("ov-login-open").disabled = !hasUrl;
    el("ov-login-copy").disabled = !hasUrl;
    const isClaude = state.login_provider === "claude";
    const hasCode = Boolean(state.login_code);
    el("ov-login-code-row").hidden = !hasCode;
    if (hasCode) {
      el("ov-login-code").textContent = state.login_code;
    }
    // Claude's browser flow ends with a code shown on the callback page
    // that must be sent back to the CLI — collect it right here.
    el("ov-login-paste-row").hidden = !(isClaude && state.login_accepts_code);
    el("ov-login-hint-browser").hidden = !hasUrl || hasCode || isClaude;
    el("ov-login-hint-device").hidden = !hasUrl || !hasCode || isClaude;
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

function accountStatusBadge(account, index, total, balance) {
  // One consolidated badge (precedence): Not ready > Limit > position.
  // A reached quota that resets on schedule is normal operation → amber, not
  // red; red is reserved for real failures (relay down, auth broken).
  // "Active"/"Standby" only describe reality in ordered mode; balanced mode
  // serves every healthy account, so they are all simply "Ready".
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
  } else if (balance === "ordered" && total > 1) {
    badge.className = index === 0 ? "badge badge-accent" : "badge badge-neutral";
    badge.textContent = index === 0 ? "Active" : "Standby";
  } else {
    badge.className = "badge badge-accent";
    badge.textContent = "Ready";
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
function accountBlock(account, index, total, balance) {
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
  head.append(emailEl, plan, accountStatusBadge(account, index, total, balance));
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
  block.appendChild(windowTokensDetail(account.window_tokens));
  return block;
}

function formatTokens(n) {
  if (!Number.isFinite(n)) return "0";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

// Ground truth behind the usage bars: what this relay served on the account
// during the current 5h window, per model — revealed on hover of "more".
// Every account gets the affordance; without data the panel says so
// instead of the trigger silently missing.
function windowTokensDetail(windowTokens) {
  const models = Array.isArray(windowTokens?.models) ? windowTokens.models : [];
  const wrap = document.createElement("div");
  wrap.className = "account-more";
  const trigger = document.createElement("span");
  trigger.className = "account-more-trigger";
  trigger.textContent = "more";
  const panel = document.createElement("div");
  panel.className = "account-more-panel";
  const title = document.createElement("div");
  title.className = "account-more-title";
  title.textContent = "This 5h window, via this relay";
  panel.appendChild(title);
  if (models.length === 0) {
    const empty = document.createElement("div");
    empty.className = "account-more-note";
    empty.textContent = "No requests served through AIRelays on this account in the current window yet.";
    panel.appendChild(empty);
    wrap.append(trigger, panel);
    return wrap;
  }
  const table = document.createElement("table");
  table.className = "account-more-table";
  const header = document.createElement("tr");
  for (const text of ["model", "tokens in", "tokens out"]) {
    const th = document.createElement("th");
    th.textContent = text;
    header.appendChild(th);
  }
  table.appendChild(header);
  for (const m of models) {
    const row = document.createElement("tr");
    for (const value of [m.model, formatTokens(m.input_tokens), formatTokens(m.output_tokens)]) {
      const td = document.createElement("td");
      td.textContent = value;
      row.appendChild(td);
    }
    table.appendChild(row);
  }
  const totals = windowTokens.totals ?? {};
  const totalRow = document.createElement("tr");
  totalRow.className = "account-more-total";
  for (const value of ["total", formatTokens(totals.input_tokens ?? 0), formatTokens(totals.output_tokens ?? 0)]) {
    const td = document.createElement("td");
    td.textContent = value;
    totalRow.appendChild(td);
  }
  table.appendChild(totalRow);
  panel.appendChild(table);
  const note = document.createElement("div");
  note.className = "account-more-note";
  note.textContent = "Counts requests served through AIRelays only; usage from other apps on this account is not included.";
  panel.appendChild(note);
  wrap.append(trigger, panel);
  return wrap;
}

function renderAccounts(state) {
  const providers = state.relay_status?.providers;
  const cacheKey =
    JSON.stringify(providers ?? null) + "|" + usageStamp + "|" + [...pendingLogout].join(",") +
    "|" + (lastState?.claude_effective ?? "") + "|" + (lastState?.claude_token_present ?? "") +
    "|" + pendingClaudeLogout;
  if (cacheKey === renderCache.accounts) {
    return;
  }
  renderCache.accounts = cacheKey;

  const container = el("ov-accounts");
  if (!providers) {
    container.innerHTML = `<div class="empty">Start the relay to see accounts.</div>`;
    el("ov-claude-account").innerHTML = "";
    return;
  }
  container.innerHTML = "";

  const openai = providers.openai;
  // The status endpoint reports a lone account at the top level; the
  // accounts array only appears with 2+ accounts.
  const accounts = Array.isArray(openai?.accounts) && openai.accounts.length > 0
    ? openai.accounts
    : openai?.enabled && openai?.email
      ? [{ email: openai.email, plan_type: openai.plan_type, ready_for_requests: openai.ready_for_requests, limited: false, window_tokens: openai.window_tokens }]
      : [];

  if (openai?.enabled && accounts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No OpenAI account signed in yet — use Sign in above.";
    container.appendChild(empty);
  }
  const balance = openai?.balance ?? "balanced";
  accounts.forEach((account, index) => {
    container.appendChild(accountBlock(account, index, accounts.length, balance));
  });
  if (accounts.length > 1) {
    const caption = document.createElement("p");
    caption.className = "card-caption";
    caption.style.margin = "8px 0 0";
    caption.textContent = balance === "ordered"
      ? "Requests go to the first account with capacity."
      : balance === "round_robin"
        ? "Requests are spread evenly across accounts with capacity."
        : "Requests are balanced by remaining capacity across accounts.";
    container.appendChild(caption);
  }

  // Claude renders into its own section (own header + sign-in button); the
  // paused state persists the row so a signed-in Claude never silently
  // vanishes after a network-mode switch.
  const claude = providers.claude;
  const claudePaused = Boolean(
    lastState?.settings?.enableClaude && !lastState?.claude_effective
  );
  const claudeContainer = el("ov-claude-account");
  claudeContainer.innerHTML = "";
  claudeContainer.appendChild(claudeBlock(claude ?? {}, claudePaused));
}

// Claude rendered with the exact same block as an OpenAI account: identity
// grid on top (email / plan / badge / sign-out), usage bars with reset
// times underneath (from the same endpoint shape Claude Code's /usage
// command reads).
function claudeBlock(claude, paused) {
  const block = document.createElement("div");
  block.className = "account-block";
  if (pendingClaudeLogout) block.classList.add("pending");
  const head = document.createElement("div");
  head.className = "account-head";

  const emailEl = document.createElement("span");
  emailEl.className = "account-email";
  emailEl.textContent = claude.email ?? claudeUsage?.account?.email ?? "Not signed in";
  if (claude.cli_version) {
    emailEl.title = `Served by the local claude CLI ${claude.cli_version}`;
  }

  const plan = document.createElement("span");
  plan.className = "account-plan";
  plan.textContent = claude.subscription_type ?? claudeUsage?.account?.plan_type ?? "";

  const badge = document.createElement("span");
  const atLimit = Boolean(claudeUsage?.rate_limit_reached_type);
  // Usage state is part of the row's health: "Ready" with a broken usage
  // meter would be a half-truth.
  const usageBlocked = Boolean(claudeUsage?.error);
  if (paused) {
    badge.className = "badge badge-neutral";
    badge.textContent = "Paused";
    badge.title = "Off while network access is on — switch to \u201CThis machine only\u201D to use it.";
  } else if (atLimit) {
    badge.className = "badge badge-warn";
    badge.textContent = "At limit";
    badge.title = "Usage limit reached; it resets on schedule.";
  } else if (claude.ready_for_requests && usageBlocked) {
    badge.className = "badge badge-warn";
    badge.textContent = "Usage unavailable";
    badge.title = "Requests work; the usage meter is temporarily unavailable (see the note below).";
  } else if (claude.ready_for_requests) {
    badge.className = "badge badge-accent";
    badge.textContent = "Ready";
  } else {
    // A deliberate signed-out state is not a fault: neutral, not amber.
    badge.className = "badge badge-neutral";
    badge.textContent = "Not signed in";
  }

  head.append(emailEl, plan, badge);

  // Sign-out parity with OpenAI rows. Also offered when only a stored
  // token exists: a stale token silently masks CLI auth, and sign-out is
  // exactly the escape hatch for that state.
  const canSignOut =
    !paused && (claude.ready_for_requests || Boolean(lastState?.claude_token_present));
  if (pendingClaudeLogout) {
    const pending = document.createElement("span");
    pending.className = "account-plan";
    pending.textContent = "…";
    head.append(pending);
  } else if (canSignOut) {
    const button = document.createElement("button");
    button.className = "copy-btn";
    button.innerHTML = icon("logOut", 14);
    button.setAttribute("aria-label", "Sign out of Claude");
    button.title = "Sign out of Claude";
    button.addEventListener("click", () => openClaudeLogoutDialog());
    head.append(button);
  }
  block.appendChild(head);

  if (paused) {
    const note = document.createElement("div");
    note.className = "account-plan";
    note.textContent = "Off while network access is on — switch to \u201CThis machine only\u201D to use it.";
    block.appendChild(note);
    return block;
  }
  if (!claude.ready_for_requests) {
    const note = document.createElement("div");
    note.className = "account-plan";
    // A usage-fetch error (e.g. the relay process predates a settings
    // change) is more accurate than assuming the user is signed out.
    note.textContent = claudeUsage?.error
      ? `Usage unavailable — ${claudeUsage.error}`
      : "Not signed in — use Sign in above.";
    block.appendChild(note);
    return block;
  }
  // Same renderer as OpenAI usage: "5h window / Weekly" bars with
  // "x% used · resets in …" details.
  if (claudeUsage?.rate_limits) {
    for (const [label, window] of usageWindows(claudeUsage)) {
      block.appendChild(usageWindowRow(label, window));
    }
    // Stale snapshot: show the bars (better than nothing) but say they are
    // cached, and why a fresh read isn't possible yet.
    if (claudeUsage.stale) {
      const note = document.createElement("div");
      note.className = "account-plan";
      const age = claudeUsage.as_of_epoch
        ? formatDuration(Math.max(0, Date.now() / 1000 - claudeUsage.as_of_epoch))
        : null;
      const retry = claudeUsage.retry_after_seconds
        ? formatDuration(claudeUsage.retry_after_seconds)
        : null;
      let why;
      if (claudeUsage.stale_reason === "credential_rejected_file") {
        why = "stored token rejected — replace it with a new token or remove it";
      } else if (claudeUsage.stale_reason === "credential_rejected") {
        why = "updates on Claude's next served request";
      } else if (retry) {
        why = `refreshes in ~${retry} (rate-limited upstream)`;
      } else {
        why = "refreshes shortly";
      }
      note.textContent = `Cached usage${age ? ` from ${age} ago` : ""} — ${why}.`;
      block.appendChild(note);
    }
  } else if (claudeUsage?.error) {
    // No usable snapshot: explain why the bars are missing instead of
    // leaving the row silently blank.
    const note = document.createElement("div");
    note.className = "account-plan";
    note.textContent = claudeUsage.error;
    block.appendChild(note);
  }
  return block;
}
