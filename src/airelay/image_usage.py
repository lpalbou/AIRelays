"""Image-generation quota tracking for the ChatGPT subscription runtime.

The subscription backend meters image generation in its own bucket,
separate from the text windows the usage endpoint reports: observed live
on 2026-10-18 as `imagegen_premium`, a 24-hour window. The usage endpoint
never mentions it (`additional_rate_limits` stays null); the only source
is the `x-codex-*` response headers of the image routes, which arrive on
every answer including the 400 a body without a prompt earns. This module
parses those headers into a window, keeps the latest observation per
account (persisted, so a restart does not blank the meter), and tallies
the images this relay itself produced in the window.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from airelay.usage_windows import window_label

STATE_VERSION = 1
# The limit id observed on the image routes; kept as a default label only,
# the header's own value always wins.
DEFAULT_IMAGE_LIMIT_ID = "imagegen_premium"
DEFAULT_IMAGE_WINDOW_SECONDS = 86400
# Rolling-window events are pruned by age; this caps pathological growth
# when the window length is unknown.
_MAX_EVENTS = 5000


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _positive_int(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number)


def image_window_label(seconds: int | None) -> str | None:
    """"24h" for the image bucket's day-long window (the shared label
    would say "1d"), the shared vocabulary for anything else."""
    if seconds == DEFAULT_IMAGE_WINDOW_SECONDS:
        return "24h"
    return window_label(seconds)


@dataclass(frozen=True, slots=True)
class ImageUsageWindow:
    """One observation of the image bucket, from response headers."""

    limit_id: str | None
    used_percent: float | None
    window_seconds: int | None
    reset_at: int | None
    reset_after_seconds: int | None
    plan_type: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.used_percent is not None and self.used_percent >= 100


def parse_image_usage_headers(headers: Mapping[str, str] | None) -> ImageUsageWindow | None:
    """The image bucket as the `x-codex-*` headers describe it, or None when
    the response carries no usage headers (a transport error page, an
    upstream without the feature)."""
    if not headers:
        return None
    lowered: dict[str, str] = {}
    for key, value in headers.items():
        if isinstance(key, str) and isinstance(value, str):
            lowered[key.lower()] = value
    used = _number(lowered.get("x-codex-primary-used-percent"))
    limit_id = lowered.get("x-codex-active-limit") or None
    if used is None and limit_id is None:
        return None
    minutes = _positive_int(lowered.get("x-codex-primary-window-minutes"))
    return ImageUsageWindow(
        limit_id=limit_id,
        used_percent=used,
        window_seconds=minutes * 60 if minutes else None,
        reset_at=_positive_int(lowered.get("x-codex-primary-reset-at")),
        reset_after_seconds=_positive_int(lowered.get("x-codex-primary-reset-after-seconds")),
        plan_type=lowered.get("x-codex-plan-type") or None,
    )


class ImageUsageTracker:
    """Per-account image quota: the latest upstream observation plus what
    this relay generated inside the window. Display data, never authority:
    persistence is best effort and a lost file only blanks the meter until
    the next image request or probe."""

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._accounts: dict[str, dict[str, Any]] = {}
        self._load()

    # ----- recording -----

    def observe(self, account_id: str | None, window: ImageUsageWindow | None, *, source: str) -> None:
        """Stores a fresh observation of the bucket (from an image response
        or a probe). `source` says which, so consumers can tell a live
        probe from a reading that is as old as the last image."""
        if not account_id or window is None:
            return
        entry = self._entry(account_id)
        entry["window"] = {
            "limit_id": window.limit_id,
            "used_percent": window.used_percent,
            "window_seconds": window.window_seconds,
            "reset_at": window.reset_at,
            "reset_after_seconds": window.reset_after_seconds,
            "plan_type": window.plan_type,
        }
        entry["observed_at"] = time.time()
        entry["source"] = source
        self.save()

    def record(
        self,
        account_id: str | None,
        window: ImageUsageWindow | None,
        *,
        images: int,
        usage: Any,
    ) -> None:
        """Counts one successful image response: its images and image
        tokens join the rolling tally, and its headers refresh the window."""
        if not account_id:
            return
        entry = self._entry(account_id)
        tokens = 0
        if isinstance(usage, dict):
            details = usage.get("output_tokens_details")
            image_tokens = details.get("image_tokens") if isinstance(details, dict) else None
            if image_tokens is None:
                image_tokens = usage.get("output_tokens")
            if isinstance(image_tokens, (int, float)) and not isinstance(image_tokens, bool):
                tokens = int(image_tokens)
        entry["events"].append([int(time.time()), max(0, int(images)), max(0, tokens)])
        if len(entry["events"]) > _MAX_EVENTS:
            del entry["events"][: len(entry["events"]) - _MAX_EVENTS]
        if window is not None:
            self.observe(account_id, window, source="image_response")
        else:
            self.save()

    # ----- reading -----

    def used_percent(self, account_id: str | None) -> float | None:
        entry = self._accounts.get(account_id or "")
        window = entry.get("window") if entry else None
        value = window.get("used_percent") if isinstance(window, dict) else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def snapshot(self, account_id: str | None) -> dict[str, Any] | None:
        """The account's image quota as last observed, plus the relay-side
        tally for the window, or None when nothing was ever observed."""
        if not account_id:
            return None
        entry = self._accounts.get(account_id)
        if not entry:
            return None
        window = entry.get("window") if isinstance(entry.get("window"), dict) else None
        seconds = (window or {}).get("window_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            seconds = DEFAULT_IMAGE_WINDOW_SECONDS
        self._prune(entry, seconds)
        events = entry.get("events") or []
        if window is None and not events:
            return None
        observed_at = entry.get("observed_at")
        observed_iso = None
        if isinstance(observed_at, (int, float)) and not isinstance(observed_at, bool):
            observed_iso = datetime.fromtimestamp(observed_at, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {
            "object": "relay_image_usage",
            "limit_id": (window or {}).get("limit_id") or DEFAULT_IMAGE_LIMIT_ID,
            "used_percent": (window or {}).get("used_percent"),
            "window_seconds": seconds,
            "window_label": image_window_label(seconds),
            "reset_at": (window or {}).get("reset_at"),
            "reset_after_seconds": (window or {}).get("reset_after_seconds"),
            "plan_type": (window or {}).get("plan_type"),
            "observed_at": observed_iso,
            "source": entry.get("source") if window is not None else None,
            "relay": {
                "scope": "last_window_via_this_relay",
                "window_seconds": seconds,
                "requests": len(events),
                "images": sum(event[1] for event in events),
                "image_tokens": sum(event[2] for event in events),
            },
        }

    # ----- internals -----

    def _entry(self, account_id: str) -> dict[str, Any]:
        entry = self._accounts.setdefault(account_id, {})
        entry.setdefault("events", [])
        return entry

    @staticmethod
    def _prune(entry: dict[str, Any], window_seconds: int) -> None:
        cutoff = int(time.time()) - window_seconds
        events = entry.get("events")
        if not isinstance(events, list):
            entry["events"] = []
            return
        entry["events"] = [
            event for event in events
            if isinstance(event, list) and len(event) == 3
            and all(isinstance(part, int) and not isinstance(part, bool) for part in event)
            and event[0] >= cutoff
        ]

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
                key: value for key, value in accounts.items() if isinstance(value, dict)
            }
            for entry in self._accounts.values():
                entry.setdefault("events", [])

    def save(self) -> None:
        state = {"version": STATE_VERSION, "accounts": self._accounts}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            pass  # best effort: display data, never authority
