// Thin wrapper over the Tauri command layer. When opened in a plain browser
// (no Tauri runtime), a mock backend with representative data takes over so
// the UI can be developed and reviewed without the app shell.

import { mockInvoke } from "./mock.js";

const invoke = window.__TAURI__?.core?.invoke ?? mockInvoke;

export const api = {
  getState: () => invoke("get_state"),
  saveSettings: (settings) => invoke("save_settings", { settings }),
  startRelay: () => invoke("start_relay"),
  stopRelay: () => invoke("stop_relay"),
  restartRelay: () => invoke("restart_relay"),
  setAuthMode: (requireToken) => invoke("set_auth_mode", { requireToken }),
  setNetworkExposure: (exposed) => invoke("set_network_exposure", { exposed }),
  getConsole: () => invoke("get_console"),
  clearConsole: () => invoke("clear_console"),
  getTraffic: () => invoke("get_traffic"),
  runDoctor: (skipResponse) => invoke("run_doctor", { skipResponse }),
  runLogin: (provider) => invoke("run_login", { provider }),
  getUsage: () => invoke("get_usage"),
  tokenAction: (action) => invoke("token_action", { action }),
  setCustomToken: (token) => invoke("set_custom_token", { token }),
  setLoginMethod: (method) => invoke("set_login_method", { method }),
  logoutAccount: (email) => invoke("logout_account", { email }),
  refreshAccounts: () => invoke("refresh_accounts"),
  openPath: (path) => invoke("open_path", { path }),
};

const toasts = () => document.getElementById("toasts");

export function toast(title, body = "", kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const titleEl = document.createElement("div");
  titleEl.className = "toast-title";
  titleEl.textContent = title;
  el.appendChild(titleEl);
  if (body) {
    const bodyEl = document.createElement("div");
    bodyEl.className = "toast-body";
    bodyEl.textContent = body;
    el.appendChild(bodyEl);
  }
  toasts().appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 9000 : 4000);
}

export async function call(promise, errorTitle) {
  try {
    return await promise;
  } catch (error) {
    toast(errorTitle, String(error), "error");
    return undefined;
  }
}

export async function copyText(text, title = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(title, "", "success");
    return;
  } catch {
    // WebKitGTK builds without async clipboard fall through.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (!ok) throw new Error("execCommand failed");
    toast(title, "", "success");
  } catch {
    toast("Copy failed", "Select the text and copy it manually.", "error");
  }
}
