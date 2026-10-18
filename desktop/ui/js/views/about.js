// About: what AIRelays is, the installed version, project links, and
// update check / in-place update.

import { api, call, toast } from "../api.js";
import { icon } from "../icons.js";
import { isCurrentMount } from "../app.js";

const REPO = "https://github.com/lpalbou/AIRelays";

const LINKS = [
  { label: "Documentation", detail: "Setup, configuration, API and troubleshooting guides.", url: "https://www.lpalbou.info/AIRelays/", icon: "fileText" },
  { label: "Changelog", detail: "What changed in each release.", url: `${REPO}/blob/main/CHANGELOG.md`, icon: "layers" },
  { label: "Report an issue", detail: "Bugs, questions and feature requests on GitHub.", url: `${REPO}/issues`, icon: "bug" },
  { label: "Source code", detail: "The AIRelays repository on GitHub.", url: REPO, icon: "code" },
  { label: "License", detail: "MIT.", url: `${REPO}/blob/main/LICENSE`, icon: "shield" },
  { label: "Disclaimer", detail: "Independent project; your provider terms still apply.", url: `${REPO}/blob/main/DISCLAIMER.md`, icon: "info" },
];

let root = null;
// One check per app session unless the user asks again: GitHub limits
// anonymous API calls, and the result rarely changes within minutes.
let lastCheck = null; // { info } | { error }
let checking = false;
let installing = false;
// The outcome of the update that reopened this app: { version, failure? }.
let reopened = null;

export function noteUpdateResult(result) {
  reopened = result;
}

const el = (id) => root?.querySelector(`#${id}`);

function formatDate(iso) {
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })
    : "";
}

function openUrl(url) {
  call(api.openUrl(url), "Cannot open browser");
}

function renderUpdate() {
  if (!root) return;
  const status = el("ab-update-status");
  const detail = el("ab-update-detail");
  const button = el("ab-check");
  button.disabled = checking || installing;
  button.querySelector(".label").textContent = checking ? "Checking…" : "Check for updates";
  button.querySelector(".icon")?.classList.toggle("spin", checking);
  detail.replaceChildren();
  renderReopened();

  if (installing) {
    status.innerHTML = `<span class="badge badge-accent">Updating</span> AIRelays is closing to install the update and reopens when it is done, usually within a minute.`;
    return;
  }
  if (!lastCheck) {
    status.textContent = checking ? "Looking for a newer release on GitHub…" : "";
    return;
  }
  if (lastCheck.error) {
    status.innerHTML = `<span class="badge badge-warn">Check failed</span> `;
    status.append(lastCheck.error);
    detail.append(linkButton("Open releases page", `${REPO}/releases`));
    return;
  }

  const info = lastCheck.info;
  if (!info.latest) {
    status.textContent = "No desktop release for this system was found on GitHub.";
    detail.append(linkButton("Open releases page", `${REPO}/releases`));
    return;
  }
  if (!info.update_available) {
    status.innerHTML = `<span class="badge badge-good">Up to date</span> `;
    status.append(
      info.current === info.latest
        ? `You have the newest release (${info.latest}).`
        : `You have ${info.current}; the newest published release is ${info.latest}.`
    );
    return;
  }

  status.innerHTML = `<span class="badge badge-accent">Update available</span> `;
  const strong = document.createElement("strong");
  strong.textContent = `AIRelays ${info.latest}`;
  const published = formatDate(info.published_at);
  status.append(strong, ` is available${published ? `, released ${published}` : ""}. You have ${info.current}.`);

  if (info.release_notes) {
    const notes = document.createElement("div");
    notes.className = "detail about-notes";
    // Plain text, not rendered markdown: headings lose their markers.
    notes.textContent = info.release_notes.replace(/^#{1,6}\s+/gm, "");
    notes.setAttribute("aria-label", `What's new in ${info.latest}`);
    detail.append(notes);
  }

  const actions = document.createElement("div");
  actions.className = "card-actions";
  if (info.can_install) {
    const update = document.createElement("button");
    update.className = "btn btn-primary";
    update.id = "ab-install";
    update.innerHTML = `${icon("download")} <span>Update to ${info.latest}</span>`;
    update.addEventListener("click", () => confirmInstall(info));
    actions.append(update);
  }
  if (info.release_url) actions.append(linkButton("Release page", info.release_url));
  detail.append(actions);
  if (!info.can_install && info.install_hint) {
    const hint = document.createElement("p");
    hint.className = "card-caption about-hint";
    hint.textContent = info.install_hint;
    detail.append(hint);
  }
}

function renderReopened() {
  const banner = el("ab-reopened");
  banner.hidden = !reopened;
  if (!reopened) return;
  banner.className = reopened.failure ? "hint warn about-reopened" : "hint about-reopened";
  banner.replaceChildren();
  const text = document.createElement("span");
  if (reopened.failure) {
    const strong = document.createElement("strong");
    strong.textContent = "The update did not install.";
    text.append(strong, ` ${reopened.failure} `, `You are still on AIRelays ${reopened.version}; nothing else changed.`);
  } else {
    const strong = document.createElement("strong");
    strong.textContent = `Updated to AIRelays ${reopened.version}.`;
    text.append(strong, " Your accounts, API key and settings were kept.");
  }
  const mark = document.createElement("span");
  mark.innerHTML = icon(reopened.failure ? "info" : "checkCircle", 14);
  banner.append(mark, text);
}

function linkButton(label, url) {
  const button = document.createElement("button");
  button.className = "btn";
  button.innerHTML = `${icon("external")} <span></span>`;
  button.querySelector("span").textContent = label;
  button.addEventListener("click", () => openUrl(url));
  return button;
}

async function checkNow() {
  if (checking || installing) return;
  checking = true;
  renderUpdate();
  try {
    lastCheck = { info: await api.checkForUpdate() };
  } catch (error) {
    lastCheck = { error: String(error) };
  } finally {
    checking = false;
    renderUpdate();
  }
}

function confirmInstall(info) {
  const dialog = el("ab-install-dialog");
  el("ab-install-title").textContent = `Update to AIRelays ${info.latest}?`;
  el("ab-install-log").textContent = info.log_path;
  dialog.showModal();
  el("ab-install-confirm").onclick = async () => {
    dialog.close();
    installing = true;
    renderUpdate();
    try {
      await api.installUpdate(info.latest);
    } catch (error) {
      installing = false;
      renderUpdate();
      toast("Update could not start", String(error), "error");
    }
  };
}

export const aboutView = {
  async mount(container, ctx, mountToken) {
    const state = ctx.getState();
    const version = state?.app_version ?? "";
    const relayVersion = state?.relay_status?.version;
    root = document.createElement("div");
    root.innerHTML = `
      <h1 class="view-title">About</h1>
      <p class="view-subtitle">Version, project links and updates.</p>

      <section class="card about-hero">
        <img src="icon.png" alt="" width="56" height="56" />
        <div>
          <div class="about-name">AIRelays <span class="badge badge-neutral mono" id="ab-version"></span></div>
          <p class="card-caption about-tagline">
            A local OpenAI-compatible endpoint backed by your own OpenAI and
            Claude subscription logins, with a tray app to run and watch it.
          </p>
          <p class="about-credit">Created by <strong>Laurent-Philippe Albou, PhD</strong> · MIT License</p>
          <div class="about-meta" id="ab-relay-version" hidden></div>
        </div>
      </section>

      <section class="card">
        <h2>Updates</h2>
        <p class="card-caption">Releases are published on GitHub. Updating keeps your accounts, API key and settings.</p>
        <div id="ab-reopened" role="status" hidden></div>
        <div class="row">
          <button class="btn" id="ab-check">${icon("refresh")} <span class="label">Check for updates</span></button>
          <div class="about-status" id="ab-update-status" role="status" aria-live="polite"></div>
        </div>
        <div id="ab-update-detail"></div>
      </section>

      <section class="card">
        <h2>Resources</h2>
        <div class="about-links" id="ab-links"></div>
      </section>

      <dialog id="ab-install-dialog" aria-labelledby="ab-install-title">
        <h3 id="ab-install-title">Update AIRelays?</h3>
        <p class="dialog-text">
          AIRelays closes, stops the relay, installs the update and reopens.
          Apps using the relay lose access for about a minute. If the update
          fails, this version reopens.
        </p>
        <p class="dialog-text" style="font-size:12px">Progress is written to <span class="mono" id="ab-install-log"></span>.</p>
        <div class="row" style="margin-top:14px">
          <span class="spacer"></span>
          <button class="btn" id="ab-install-cancel">Cancel</button>
          <button class="btn btn-primary" id="ab-install-confirm">Update and restart</button>
        </div>
      </dialog>
    `;
    el("ab-version").textContent = version;
    if (relayVersion && relayVersion !== version) {
      // Only differs with a relay command override; worth surfacing.
      const meta = el("ab-relay-version");
      meta.textContent = `The running relay reports version ${relayVersion}.`;
      meta.hidden = false;
    }
    const links = el("ab-links");
    for (const link of LINKS) {
      const button = document.createElement("button");
      button.className = "about-link";
      button.innerHTML = `${icon(link.icon, 16)}<span class="about-link-text"><strong></strong><small></small></span>${icon("external", 13)}`;
      button.querySelector("strong").textContent = link.label;
      button.querySelector("small").textContent = link.detail;
      button.title = link.url;
      button.addEventListener("click", () => openUrl(link.url));
      links.append(button);
    }
    el("ab-check").addEventListener("click", checkNow);
    el("ab-install-cancel").addEventListener("click", () => el("ab-install-dialog").close());
    container.appendChild(root);
    renderUpdate();
    // Not awaited: the tab must stay responsive while GitHub answers.
    if (!lastCheck && isCurrentMount(mountToken)) checkNow();
  },
  async update() {},
  unmount() {
    root = null;
  },
};
