import { api, toast } from "../api.js";

// This editor uses the relay API directly. Its saved policy survives the
// desktop's generated config and applies independently of Save & Restart.
export function mountLogRetention(container) {
  const editor = { dirty: false, disposed: false };
  container.innerHTML = `
    <h2>Traffic log retention</h2>
    <p class="card-caption">Applied immediately, without restarting. Oldest logs are permanently removed when either the age or disk limit is reached. A busy relay may retain fewer days.</p>
    <p id="lr-status" role="status">Loading log usage…</p>
    <form id="lr-form">
      <fieldset id="lr-fields" disabled style="border:0;padding:0;margin:0">
        <div class="row" style="margin-bottom:14px">
          <span>Retention presets:</span>
          <button type="button" class="btn btn-sm" data-days="7">1 week</button>
          <button type="button" class="btn btn-sm" data-days="30">1 month (30 days)</button>
        </div>
        <div class="form-grid">
          <div class="field"><label for="lr-days">Keep up to (days; 0 = no age limit)</label><input id="lr-days" type="number" min="0" max="36500" step="1" required></div>
          <div class="field"><label for="lr-total">Total disk limit (MiB)</label><input id="lr-total" type="number" min="1" max="1048576" step="1" required></div>
          <div class="field"><label for="lr-file">Rotate each file at (MiB)</label><input id="lr-file" type="number" min="1" max="10240" step="1" required></div>
        </div>
        <p class="card-caption">1024 MiB = 1 GiB. Files also rotate hourly. Applies to traffic logs, including existing logs; console output and stored conversations are separate.</p>
        <button class="btn btn-primary" type="submit">Apply log retention</button>
      </fieldset>
    </form>
    <button class="btn btn-sm" type="button" id="lr-refresh" style="margin-top:12px">Reload policy &amp; usage</button>
  `;
  const status = container.querySelector("#lr-status");
  const fields = container.querySelector("#lr-fields");
  const days = container.querySelector("#lr-days");
  const total = container.querySelector("#lr-total");
  const file = container.querySelector("#lr-file");
  const refresh = container.querySelector("#lr-refresh");
  let busy = false;

  function show(result) {
    days.value = result.policy.retention_days;
    total.value = result.policy.max_total_mb;
    file.value = result.policy.max_file_mb;
    const usage = `${(result.usage_bytes / 1048576).toFixed(1)} MiB used of ${result.policy.max_total_mb} MiB · ${result.file_count} files`;
    status.textContent = result.last_error
      ? `${usage}. Cleanup error: ${result.last_error}. Logging may be paused until cleanup succeeds.`
      : `${usage}${result.over_budget ? " · Disk limit exceeded" : ""}`;
    if (result.oversized_records) status.textContent += ` · ${result.oversized_records} oversized records omitted`;
    if (result.dropped_records) status.textContent += ` · ${result.dropped_records} records skipped due to storage errors`;
    editor.dirty = false;
  }

  async function load() {
    if (busy) return;
    busy = true;
    fields.disabled = true;
    refresh.disabled = true;
    try {
      const result = await api.getLogRetention();
      if (editor.disposed) return;
      show(result);
      fields.disabled = false;
    } catch (error) {
      if (!editor.disposed) status.textContent = `${error} Retention can also be configured with “airelays logs” while the relay is stopped.`;
    } finally {
      busy = false;
      refresh.disabled = false;
    }
  }

  fields.addEventListener("input", () => { editor.dirty = true; });
  for (const button of container.querySelectorAll("[data-days]")) {
    button.addEventListener("click", () => {
      days.value = button.dataset.days;
      editor.dirty = true;
    });
  }
  refresh.addEventListener("click", () => {
    if (!editor.dirty || window.confirm("Discard unsaved log retention changes and reload?")) load();
  });
  container.querySelector("#lr-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const policy = {
      retention_days: Number(days.value),
      max_total_mb: Number(total.value),
      max_file_mb: Number(file.value),
    };
    if (policy.max_file_mb > policy.max_total_mb) {
      toast("Invalid retention", "The file limit must not exceed the total disk limit.", "error");
      return;
    }
    busy = true;
    fields.disabled = true;
    refresh.disabled = true;
    try {
      const result = await api.setLogRetention(policy);
      if (editor.disposed) return;
      show(result);
      toast(result.last_error ? "Policy saved; cleanup needs attention" : "Log retention applied", result.last_error || "", result.last_error ? "error" : "success");
    } catch (error) {
      if (!editor.disposed) toast("Cannot apply retention", String(error), "error");
    } finally {
      busy = false;
      fields.disabled = false;
      refresh.disabled = false;
    }
  });
  load();
  return editor;
}
