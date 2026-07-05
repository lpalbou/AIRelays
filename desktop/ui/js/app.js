// Shell: sidebar navigation, shared state polling, view lifecycle.
//
// Views receive a context object: { getState() } returning the latest
// polled state (or null when the backend is unavailable). Each view module
// implements mount(container, ctx, mountToken), optional update(state),
// optional canLeave() -> bool, and unmount().

import { api } from "./api.js";
import { icon } from "./icons.js";
import { overviewView } from "./views/overview.js";
import { trafficView } from "./views/traffic.js";
import { consoleView } from "./views/console.js";
import { settingsView } from "./views/settings.js";

const views = {
  overview: overviewView,
  traffic: trafficView,
  console: consoleView,
  settings: settingsView,
};

let activeName = "overview";
let activeView = null;
let latestState = null;
let switching = false;
// Incremented on every switch; stale async mounts check it and bail.
let mountToken = 0;

const content = document.getElementById("content");
const ctx = { getState: () => latestState };

async function switchView(name) {
  if (switching || !views[name] || name === activeName) {
    return;
  }
  if (activeView?.canLeave && !activeView.canLeave()) {
    // The view refused (e.g. unsaved settings); restore the hash.
    if (location.hash !== `#${activeName}`) {
      location.hash = activeName;
    }
    return;
  }
  switching = true;
  try {
    if (activeView?.unmount) {
      activeView.unmount();
    }
    mountToken += 1;
    activeName = name;
    activeView = views[name];
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.view === name);
      item.setAttribute("aria-current", item.dataset.view === name ? "page" : "false");
    });
    if (location.hash !== `#${name}`) {
      location.hash = name;
    }
    content.innerHTML = "";
    await activeView.mount(content, ctx, mountToken);
    content.focus();
  } finally {
    switching = false;
  }
}

export function isCurrentMount(token) {
  return token === mountToken;
}

function updateSidebarStatus(state) {
  const dot = document.getElementById("sidebar-dot");
  const label = document.getElementById("sidebar-state");
  if (!state) {
    dot.className = "dot dot-neutral";
    label.textContent = "Unavailable";
    return;
  }
  if (state.reachable) {
    dot.className = "dot dot-good";
    label.textContent = "Running";
  } else if (state.auth_mismatch) {
    dot.className = "dot dot-warn";
    label.textContent = "Running (token mismatch)";
  } else if (state.lifecycle === "starting") {
    dot.className = "dot dot-warn";
    label.textContent = "Starting…";
  } else if (state.lifecycle === "failed") {
    dot.className = "dot dot-bad";
    label.textContent = "Failed";
  } else {
    dot.className = "dot dot-neutral";
    label.textContent = "Stopped";
  }
}

async function poll() {
  try {
    latestState = await api.getState();
  } catch {
    latestState = null;
  }
  updateSidebarStatus(latestState);
  if (activeView?.update) {
    try {
      await activeView.update(latestState);
    } catch {
      // A view rendering error must not kill the poll loop.
    }
  }
  // Re-arm only after completion so slow responses cannot overlap and
  // apply out of order.
  setTimeout(poll, 1500);
}

document.querySelectorAll(".nav-item").forEach((item) => {
  item.insertAdjacentHTML("afterbegin", icon(item.dataset.icon, 16));
  item.addEventListener("click", () => switchView(item.dataset.view));
});

window.addEventListener("hashchange", () => {
  const name = location.hash.replace("#", "");
  if (views[name]) {
    switchView(name);
  }
});

const initialView = location.hash.replace("#", "");
await poll();
activeName = ""; // force the first switch
await switchView(views[initialView] ? initialView : "overview");
