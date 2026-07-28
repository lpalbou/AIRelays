"""Typed view of the rate-limit windows inside an upstream usage payload.

The upstream usage endpoint reports one or two windows under ``rate_limit``
(``primary_window``/``secondary_window``), but which horizon occupies which
slot is plan-dependent: observed live (2026-07), a Plus account reports its
weekly window alone in the primary slot (the plan has no 5h window anymore),
while an Enterprise account keeps the classic 5h primary plus a weekly
secondary. Slot position therefore no longer identifies a window — only its
duration (``limit_window_seconds``) does. This module is the single place
that parses windows out of a payload and ranks them by horizon, so routing
and the token tally never reason about slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Slot names the upstream uses today, kept in payload order. Order matters
# only as a deterministic tie-break; a window's identity is its duration.
_WINDOW_SLOTS = ("primary_window", "secondary_window")


def _number(value: Any) -> float | None:
    """A float for real numbers only. bool is an int subclass in Python and
    must never pass as a percentage, duration, or timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _positive_int(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number)


@dataclass(frozen=True)
class UsageWindow:
    """One rate-limit window. ``slot`` records where it came from for
    debugging; consumers identify the window by ``window_seconds``."""

    slot: str
    window_seconds: int | None
    used_percent: float | None
    reset_at: int | None
    reset_after_seconds: int | None

    @property
    def exhausted(self) -> bool:
        return self.used_percent is not None and self.used_percent >= 100


def parse_rate_limit_windows(rate_limit: Any) -> list[UsageWindow]:
    """Every window present in a ``rate_limit`` dict, in slot order. Absent
    or non-dict slots are skipped; malformed fields inside a present window
    degrade to None instead of dropping the window (a window with a readable
    used_percent must still bench even if its reset fields are broken)."""
    if not isinstance(rate_limit, dict):
        return []
    windows: list[UsageWindow] = []
    for slot in _WINDOW_SLOTS:
        raw = rate_limit.get(slot)
        if not isinstance(raw, dict):
            continue
        # Structured 429 error bodies spell the same field
        # `resets_in_seconds`; accept it as the fallback spelling so both
        # payload families feed the same cooldown math.
        reset_after = _positive_int(raw.get("reset_after_seconds"))
        if reset_after is None:
            reset_after = _positive_int(raw.get("resets_in_seconds"))
        windows.append(
            UsageWindow(
                slot=slot,
                window_seconds=_positive_int(raw.get("limit_window_seconds")),
                used_percent=_number(raw.get("used_percent")),
                reset_at=_positive_int(raw.get("reset_at")),
                reset_after_seconds=reset_after,
            )
        )
    return windows


def longest_window(windows: list[UsageWindow]) -> UsageWindow | None:
    """The longest-horizon window — the scarce, slowest-recovering budget
    (today: the weekly window, present on every current plan shape).

    A window without a reported duration cannot be ranked and loses to any
    window whose duration is known; when nothing disambiguates (equal or
    all-unknown durations) the earliest slot wins, and a lone window is
    trivially the longest — which is what keeps single-window payloads,
    whatever their shape, behaving like today. Returns None when the payload
    carries no windows at all.
    """
    if not windows:
        return None
    # max() keeps the first maximal element, so slot order is the tie-break.
    return max(
        windows,
        key=lambda window: (window.window_seconds is not None, window.window_seconds or 0),
    )


def window_label(seconds: int | None) -> str | None:
    """Human label for a window duration ("weekly", "2d", "5h", "30m").
    Shared by the normalized status payload and the token tally so the
    desktop renders one vocabulary everywhere."""
    if seconds is None or seconds <= 0:
        return None
    if seconds == 604800:
        return "weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
