// Console: output of app-run commands (serve, sign-ins, setup checks).

import { api, call, copyText, toast } from "../api.js";
import { icon } from "../icons.js";
import { isCurrentMount } from "../app.js";

let root = null;
let timer = null;
let lastStamp = "";
let entries = [];
let filterText = "";

export const consoleView = {
  async mount(container, ctx, mountToken) {
    root = document.createElement("div");
    root.innerHTML = `
      <h1 class="view-title">Console</h1>
      <p class="view-subtitle">Output from the relay process and commands run by this app.</p>
      <div class="row" style="margin-bottom:10px">
        <button class="btn btn-small" id="co-doctor" title="Runs the relay's setup checks.">${icon("checkCircle", 14)} Check Setup</button>
        <button class="btn btn-small" id="co-doctor-skip" title="Setup checks without the small live test request.">${icon("checkCircle", 14)} Quick</button>
        <input type="text" id="co-filter" class="filter-input" placeholder="Filter…" aria-label="Filter console output" />
        <span class="spacer"></span>
        <button class="btn btn-small" id="co-copy" aria-label="Copy all output" title="Copy all">${icon("copy", 14)}</button>
        <button class="btn btn-small" id="co-logs" aria-label="Open logs folder" title="Logs folder">${icon("folder", 14)}</button>
        <button class="btn btn-small" id="co-config" aria-label="Open config file" title="Config file">${icon("fileText", 14)}</button>
        <button class="btn btn-small" id="co-clear" aria-label="Clear console" title="Clear">${icon("trash", 14)}</button>
      </div>
      <div class="console" id="co-list" role="log" aria-label="Console output"></div>
    `;
    container.appendChild(root);

    root.querySelector("#co-doctor").addEventListener("click", () => {
      toast("Checking setup", "Results will appear below.");
      call(api.runDoctor(false), "Setup check failed");
    });
    root.querySelector("#co-doctor-skip").addEventListener("click", () => {
      toast("Checking setup (quick)", "Results will appear below.");
      call(api.runDoctor(true), "Setup check failed");
    });
    root.querySelector("#co-filter").addEventListener("input", (event) => {
      filterText = event.target.value.toLowerCase();
      renderList(true);
    });
    root.querySelector("#co-copy").addEventListener("click", () => {
      const text = visibleEntries()
        .map((entry) => `${timeOf(entry)} ${entry.source} ${entry.text}`)
        .join("\n");
      copyText(text, "Console copied");
    });
    root.querySelector("#co-logs").addEventListener("click", () => {
      const state = ctx.getState();
      if (state) call(api.openPath(state.logs_dir), "Cannot open logs folder");
    });
    root.querySelector("#co-config").addEventListener("click", () => {
      const state = ctx.getState();
      if (state) call(api.openPath(state.config_path), "Cannot open config file");
    });
    root.querySelector("#co-clear").addEventListener("click", async () => {
      await call(api.clearConsole(), "Clear failed");
      lastStamp = "";
      await refresh();
    });

    await refresh();
    if (isCurrentMount(mountToken)) {
      timer = setInterval(refresh, 1200);
    }
  },
  async update() {},
  unmount() {
    clearInterval(timer);
    timer = null;
    root = null;
    lastStamp = "";
    entries = [];
    filterText = "";
  },
};

function timeOf(entry) {
  return new Date(entry.at_ms).toLocaleTimeString();
}

function visibleEntries() {
  if (!filterText) return entries;
  return entries.filter(
    (entry) =>
      entry.text.toLowerCase().includes(filterText) ||
      entry.source.toLowerCase().includes(filterText)
  );
}

async function refresh() {
  if (!root) return;
  let fetched;
  try {
    fetched = await api.getConsole();
  } catch {
    return;
  }
  if (!root) return;
  // Content-aware change detection: length alone is wrong once the backend
  // cap (500 entries) is reached and old lines are drained.
  const stamp = `${fetched.length}:${fetched[fetched.length - 1]?.at_ms ?? 0}:${fetched[0]?.at_ms ?? 0}`;
  if (stamp === lastStamp) return;
  lastStamp = stamp;
  entries = fetched;
  renderList(false);
}

function renderList(force) {
  const list = root.querySelector("#co-list");
  const stickToBottom =
    force || list.scrollTop + list.clientHeight >= list.scrollHeight - 40;
  list.innerHTML = "";
  for (const entry of visibleEntries()) {
    const line = document.createElement("div");
    line.className = "console-line" + (entry.is_error ? " error" : "");
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = timeOf(entry);
    const source = document.createElement("span");
    source.className = "source";
    source.textContent = entry.source;
    const text = document.createElement("span");
    text.className = "text";
    text.textContent = entry.text;
    line.append(time, source, text);
    list.appendChild(line);
  }
  if (stickToBottom) {
    list.scrollTop = list.scrollHeight;
  }
}
