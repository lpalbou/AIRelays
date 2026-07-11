// Settings: full relay config surface, saved to the app settings file and
// rendered into the relay's config.toml.

import { api, call, toast } from "../api.js";
import { icon } from "../icons.js";

let root = null;
let draft = null;
let dirty = false;

// kind: "text" | "bool" | "select" | number with {min, max}
const FIELDS = [
  {
    title: "Listener & Auth",
    caption: "Where the relay listens and whether apps need the API key. The Overview tab has one-click switches for the common cases.",
    items: [
      { key: "host", label: "Host", kind: "text" },
      { key: "port", label: "Port", kind: "number", min: 1, max: 65535 },
      { key: "requireBearerAuth", label: "Require API key", kind: "bool" },
      { key: "autoGenerateBearerToken", label: "Create a key automatically when missing", kind: "bool" },
      { key: "authStorageMode", label: "Login storage", kind: "select", options: ["auto", "file", "keyring"] },
      { key: "browserOpen", label: "Open browser automatically on sign-in", kind: "bool" },
      { key: "loginTimeoutSeconds", label: "Sign-in timeout (seconds)", kind: "number", min: 30, max: 7200 },
    ],
  },
  {
    title: "Launch",
    caption: "How and when the app starts the relay. Leave the override empty to use the runtime bundled with this app.",
    items: [
      { key: "startRelayOnLaunch", label: "Start the relay when this app opens", kind: "bool" },
      { key: "autoRestartRelay", label: "Restart the relay automatically if it crashes", kind: "bool" },
      { key: "relayCommandOverride", label: "Relay command override (advanced)", kind: "text" },
      { key: "extraServeArgs", label: "Extra serve arguments (advanced)", kind: "text" },
    ],
  },
  {
    title: "Upstream",
    caption: "The subscription-backed upstream this relay talks to. Defaults are correct for normal use.",
    items: [
      { key: "upstreamBaseUrl", label: "Upstream base URL", kind: "text" },
      { key: "issuerBaseUrl", label: "Issuer base URL", kind: "text" },
      { key: "clientId", label: "Client ID", kind: "text" },
      { key: "clientVersion", label: "Client version", kind: "text" },
      { key: "requestTimeoutSeconds", label: "Request timeout (seconds)", kind: "number", min: 1, max: 3600 },
    ],
  },
  {
    title: "Security Limits",
    caption: "Abuse protection applied per client address.",
    items: [
      { key: "rateLimitPerMinute", label: "Rate limit (requests/minute)", kind: "number", min: 1, max: 100000 },
      { key: "rateLimitBurst", label: "Rate limit burst", kind: "number", min: 1, max: 100000 },
      { key: "concurrentRequestsPerIp", label: "Concurrent requests per address", kind: "number", min: 1, max: 1000 },
      { key: "failedAuthWindowSeconds", label: "Failed-auth window (seconds)", kind: "number", min: 1, max: 86400 },
      { key: "failedAuthMaxAttempts", label: "Failed-auth max attempts", kind: "number", min: 1, max: 1000 },
      { key: "failedAuthBlockSeconds", label: "Failed-auth block (seconds)", kind: "number", min: 1, max: 86400 },
      { key: "trustXForwardedFor", label: "Trust X-Forwarded-For header", kind: "bool" },
      { key: "maxUploadBytes", label: "Max upload size (bytes)", kind: "number", min: 1024, max: 10 * 1024 * 1024 * 1024 },
      { key: "maxTotalUploadBytes", label: "Max total upload size (bytes)", kind: "number", min: 1024, max: 100 * 1024 * 1024 * 1024 },
    ],
  },
  {
    title: "Providers",
    caption: "The Claude provider requires 'This machine only' network mode.",
    items: [
      { key: "enableOpenaiProvider", label: "Enable OpenAI provider", kind: "bool" },
      { key: "modelsCacheTtlSeconds", label: "OpenAI models cache TTL (seconds)", kind: "number", min: 0, max: 86400 },
      { key: "openaiBalance", label: "OpenAI account balancing", kind: "select", options: ["balanced", "round_robin", "ordered"] },
      { key: "openaiExtraModelsCsv", label: "OpenAI extra models (comma-separated)", kind: "text" },
      { key: "enableClaude", label: "Enable Claude", kind: "bool" },
      { key: "claudeBin", label: "Claude CLI binary", kind: "text" },
      { key: "claudeTimeoutSeconds", label: "Claude timeout (seconds)", kind: "number", min: 1, max: 7200 },
      { key: "claudeMaxConcurrentRequests", label: "Claude max concurrent requests", kind: "number", min: 1, max: 64 },
      { key: "claudeStripApiKeyEnv", label: "Strip API key env for Claude", kind: "bool" },
      { key: "claudeModelsCsv", label: "Claude models (comma-separated)", kind: "text" },
    ],
  },
];

export const settingsView = {
  async mount(container, ctx) {
    root = document.createElement("div");
    const state = ctx.getState();
    if (!state) {
      root.innerHTML = `
        <h1 class="view-title">Settings</h1>
        <div class="card"><div class="empty">
          <div class="empty-title">Backend unavailable</div>
          Settings cannot be loaded right now. Try again in a moment.
        </div></div>
      `;
      container.appendChild(root);
      return;
    }
    draft = structuredClone(state.settings);
    dirty = false;
    root.innerHTML = `
      <h1 class="view-title">Settings</h1>
      <p class="view-subtitle">
        Saving writes the relay's config file. A running relay keeps its old
        settings until it restarts — use "Save &amp; Restart" to apply now.
      </p>
      <div class="row" style="margin-bottom:14px">
        <button class="btn btn-primary" id="se-save">${icon("checkCircle")} Save</button>
        <button class="btn" id="se-save-restart">${icon("restart")} Save &amp; Restart</button>
        <span class="spacer"></span>
        <button class="btn" id="se-reset">${icon("trash")} Discard changes</button>
      </div>
      <section class="card">
        <h2>Desktop App</h2>
        <p class="card-caption">Applied immediately — not part of the saved relay settings.</p>
        <div class="field-toggle">
          <input type="checkbox" id="se-autostart" disabled />
          <label for="se-autostart">Start AIRelays at login</label>
        </div>
      </section>
      <div id="se-sections"></div>
    `;
    container.appendChild(root);
    renderSections();
    bindAutostart();

    root.querySelector("#se-save").addEventListener("click", () => save(false));
    root.querySelector("#se-save-restart").addEventListener("click", () => save(true));
    root.querySelector("#se-reset").addEventListener("click", () => {
      const current = ctx.getState();
      if (!current) {
        toast("Backend unavailable", "Cannot reload settings right now.", "error");
        return;
      }
      draft = structuredClone(current.settings);
      dirty = false;
      renderSections();
      toast("Changes discarded");
    });
  },
  // No live update while editing: the form is a draft until saved.
  async update() {},
  canLeave() {
    if (!dirty) return true;
    return window.confirm("You have unsaved settings changes. Leave and discard them?");
  },
  unmount() {
    root = null;
    draft = null;
    dirty = false;
  },
};

/// Start-at-login mirrors OS state (login items / registry / .desktop
/// entry), so it reads and writes through dedicated commands and applies
/// immediately instead of joining the settings draft.
async function bindAutostart() {
  const checkbox = root.querySelector("#se-autostart");
  const enabled = await call(api.getAutostart(), "Cannot read the login item");
  if (!root || enabled === undefined) return;
  checkbox.checked = Boolean(enabled);
  checkbox.disabled = false;
  checkbox.addEventListener("change", async () => {
    checkbox.disabled = true;
    const done = await call(api.setAutostart(checkbox.checked), "Change failed");
    if (!root) return;
    if (done === undefined) {
      checkbox.checked = !checkbox.checked; // revert on failure
    } else {
      toast(
        checkbox.checked ? "Start at login enabled" : "Start at login disabled",
        "",
        "success"
      );
    }
    checkbox.disabled = false;
  });
}

function renderSections() {
  const sections = root.querySelector("#se-sections");
  sections.innerHTML = "";
  for (const group of FIELDS) {
    const card = document.createElement("section");
    card.className = "card";
    const title = document.createElement("h2");
    title.textContent = group.title;
    const caption = document.createElement("p");
    caption.className = "card-caption";
    caption.textContent = group.caption;
    const grid = document.createElement("div");
    grid.className = "form-grid";
    for (const item of group.items) {
      grid.appendChild(field(item));
    }
    card.append(title, caption, grid);
    sections.appendChild(card);
  }
}

function field(item) {
  const { key, label, kind } = item;
  const wrap = document.createElement("div");

  if (kind === "bool") {
    wrap.className = "field-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = `se-${key}`;
    input.checked = Boolean(draft[key]);
    input.addEventListener("change", () => {
      draft[key] = input.checked;
      dirty = true;
    });
    const labelEl = document.createElement("label");
    labelEl.htmlFor = input.id;
    labelEl.textContent = label;
    wrap.append(input, labelEl);
    return wrap;
  }

  wrap.className = "field";
  const labelEl = document.createElement("label");
  labelEl.htmlFor = `se-${key}`;
  labelEl.textContent = label;

  if (kind === "select") {
    const select = document.createElement("select");
    select.id = `se-${key}`;
    for (const option of item.options) {
      const optionEl = document.createElement("option");
      optionEl.value = option;
      optionEl.textContent = option;
      optionEl.selected = draft[key] === option;
      select.appendChild(optionEl);
    }
    select.addEventListener("change", () => {
      draft[key] = select.value;
      dirty = true;
    });
    wrap.append(labelEl, select);
    return wrap;
  }

  const input = document.createElement("input");
  input.type = kind === "number" ? "number" : "text";
  input.id = `se-${key}`;
  input.value = draft[key] ?? "";
  if (kind === "number") {
    input.min = item.min;
    input.max = item.max;
  }
  input.addEventListener("input", () => {
    draft[key] = kind === "number" ? input.value : input.value;
    dirty = true;
  });
  wrap.append(labelEl, input);
  return wrap;
}

/// Validates and coerces numeric drafts; returns error text or null.
function validateDraft() {
  for (const group of FIELDS) {
    for (const item of group.items) {
      if (item.kind !== "number") continue;
      const raw = draft[item.key];
      const value = typeof raw === "number" ? raw : Number(String(raw).trim());
      if (String(raw).trim() === "" || Number.isNaN(value)) {
        return `"${item.label}" needs a number.`;
      }
      if (value < item.min || value > item.max) {
        return `"${item.label}" must be between ${item.min} and ${item.max}.`;
      }
      draft[item.key] = value;
    }
  }
  return null;
}

async function save(restart) {
  const error = validateDraft();
  if (error) {
    toast("Invalid settings", error, "error");
    return;
  }
  const saved = await call(api.saveSettings(draft), "Save failed");
  if (saved === undefined) return;
  dirty = false;
  if (restart) {
    await call(api.restartRelay(), "Restart failed");
    toast("Saved and restarted", "", "success");
  } else {
    toast("Settings saved", "A running relay applies them on its next restart.", "success");
  }
}
