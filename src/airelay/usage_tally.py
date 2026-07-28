"""Per-account, per-model token tallies for the current usage window.

The upstream usage endpoint reports quota only as a percentage; this tally
records the ground truth the relay itself observes — input/output tokens per
model served through each account — scoped to the account's longest-horizon
usage window (today: the weekly budget; identified by that window's upstream
`reset_at` anchor). Anchoring to the longest window is deliberate: which
windows a plan reports is upstream policy (current Plus plans carry no 5h
window at all), and anchoring to a short window wiped the breakdown every
few hours, leaving the desktop's per-model panel permanently empty. No
estimation, no extrapolation: the numbers answer "what did the shown
percentage cost, via this relay". Traffic from outside the relay is
invisible here by design, and the UI says so.

State survives restarts through a small JSON file in the data dir, written
atomically like the other state files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from airelay.usage_windows import window_label

# Still 1 across the longest-window change: the schema gained only the
# optional `window_seconds` field, and entries the old code anchored to a
# short (5h) window self-correct on the first probe — their stored anchor no
# longer matches the longest window's, which clears them — while entries
# already anchored to a weekly window survive correctly scoped. A version
# bump would discard that valid data for no correctness gain.
STATE_VERSION = 1
# Persist at most once per this many recorded requests (probes and shutdown
# also save), so a crash loses at most a sliver of tooltip data without
# turning every request into disk IO.
SAVE_EVERY_RECORDS = 50


class WindowTokenTally:
    """Tokens served per account, per model, within the account's current
    longest-horizon usage window. Keys are upstream account ids (stable
    across re-logins); the window identity is that window's upstream
    `reset_at` anchor, with `window_seconds` kept as display metadata."""

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        # account_id -> {"reset_at": int | None, "window_seconds": int | None,
        #                "models": {model: {"input", "output", "cached"}}}
        self._accounts: dict[str, dict[str, Any]] = {}
        self._unsaved_records = 0
        self._load()

    # ----- recording -----

    def record(
        self,
        account_id: str | None,
        model: str | None,
        usage: Any,
    ) -> None:
        """Adds one request's usage. Missing fields count as zero; nothing
        is ever estimated. Unknown accounts/models are keyed verbatim."""
        if not account_id or not isinstance(usage, dict):
            return
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) and not isinstance(output_tokens, int):
            return
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        entry = self._accounts.setdefault(
            account_id, {"reset_at": None, "window_seconds": None, "models": {}}
        )
        bucket = entry["models"].setdefault(
            str(model or "unknown"), {"input": 0, "output": 0, "cached": 0}
        )
        bucket["input"] += input_tokens if isinstance(input_tokens, int) else 0
        bucket["output"] += output_tokens if isinstance(output_tokens, int) else 0
        bucket["cached"] += cached if isinstance(cached, int) else 0
        self._unsaved_records += 1
        if self._unsaved_records >= SAVE_EVERY_RECORDS:
            self.save()

    def set_window(
        self, account_id: str | None, reset_at: Any, window_seconds: Any = None
    ) -> None:
        """Aligns the tally with the account's current (longest-horizon)
        window. A changed `reset_at` anchor means that window rolled over —
        even when it drifted while the relay sat idle — so the old breakdown
        no longer describes the shown percentage and is cleared.
        `window_seconds` is display metadata (it labels the breakdown); the
        anchor alone is the window's identity. Callers that observe no
        windows simply never call this, which keeps the current tally."""
        if not account_id or not isinstance(reset_at, (int, float)):
            return
        seconds = (
            int(window_seconds)
            if isinstance(window_seconds, (int, float))
            and not isinstance(window_seconds, bool)
            and window_seconds > 0
            else None
        )
        entry = self._accounts.setdefault(
            account_id, {"reset_at": None, "window_seconds": None, "models": {}}
        )
        known = entry.get("reset_at")
        if known is None:
            # Tokens recorded before the first probe stay attributed to the
            # window that probe then identifies.
            entry["reset_at"] = int(reset_at)
            entry["window_seconds"] = seconds
        elif int(reset_at) != known:
            self._accounts[account_id] = {
                "reset_at": int(reset_at),
                "window_seconds": seconds,
                "models": {},
            }
            self.save()
        elif seconds is not None and entry.get("window_seconds") != seconds:
            # Same window, newly learned duration (e.g. state written before
            # the duration was persisted): metadata only, tokens are kept.
            entry["window_seconds"] = seconds

    # ----- reading -----

    def snapshot(self, account_id: str | None) -> dict[str, Any] | None:
        """The current window's breakdown for one account, or None when
        nothing was observed yet. Models are sorted by total descending."""
        if not account_id:
            return None
        entry = self._accounts.get(account_id)
        if not entry or not entry.get("models"):
            return None
        models = []
        total_input = total_output = total_cached = 0
        for name, bucket in entry["models"].items():
            models.append(
                {
                    "model": name,
                    "input_tokens": bucket.get("input", 0),
                    "output_tokens": bucket.get("output", 0),
                    "cached_input_tokens": bucket.get("cached", 0),
                }
            )
            total_input += bucket.get("input", 0)
            total_output += bucket.get("output", 0)
            total_cached += bucket.get("cached", 0)
        models.sort(key=lambda m: m["input_tokens"] + m["output_tokens"], reverse=True)
        seconds = entry.get("window_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            seconds = None  # tolerate hand-edited/older state files
        return {
            "object": "relay_window_tokens",
            # Horizon-neutral: WHICH window the numbers cover is data
            # (window_label/window_seconds below), not a baked-in 5h claim —
            # plans differ in which windows they even have.
            "scope": "current_usage_window_via_this_relay",
            "window_reset_at": entry.get("reset_at"),
            "window_seconds": seconds,
            "window_label": window_label(seconds),
            "models": models,
            "totals": {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cached_input_tokens": total_cached,
            },
        }

    # ----- persistence -----

    def _load(self) -> None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return
        accounts = state.get("accounts")
        if isinstance(accounts, dict):
            self._accounts = {
                key: value
                for key, value in accounts.items()
                if isinstance(value, dict) and isinstance(value.get("models"), dict)
            }

    def save(self) -> None:
        self._unsaved_records = 0
        state = {"version": STATE_VERSION, "accounts": self._accounts}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            pass  # best-effort: the tally is display data, never authority
