"""Window parsing and ranking for upstream usage payloads.

The upstream reports one or two rate-limit windows whose slot position is
plan-dependent; only the duration identifies a horizon. These examples
mirror live payload shapes (2026-07), but the parser must hold for any
window mix, including degraded payloads with missing fields.
"""

from __future__ import annotations

from airelay.usage_windows import (
    UsageWindow,
    longest_window,
    parse_rate_limit_windows,
    window_label,
)


def test_parse_collects_every_present_window_with_its_fields() -> None:
    windows = parse_rate_limit_windows(
        {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 0,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 4200,
                "reset_at": 1785214800,
            },
            "secondary_window": {
                "used_percent": 4,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 518000,
                "reset_at": 1785732000,
            },
        }
    )
    assert [w.slot for w in windows] == ["primary_window", "secondary_window"]
    assert windows[0].window_seconds == 18000
    assert windows[0].used_percent == 0.0
    assert windows[1].window_seconds == 604800
    assert windows[1].reset_at == 1785732000
    assert windows[1].reset_after_seconds == 518000


def test_parse_skips_absent_slots_and_non_dict_payloads() -> None:
    only_primary = parse_rate_limit_windows(
        {"primary_window": {"used_percent": 91, "limit_window_seconds": 604800}}
    )
    assert len(only_primary) == 1 and only_primary[0].slot == "primary_window"
    assert parse_rate_limit_windows({"primary_window": "broken"}) == []
    assert parse_rate_limit_windows(None) == []
    assert parse_rate_limit_windows("rate_limit") == []


def test_parse_degrades_malformed_fields_without_dropping_the_window() -> None:
    # A window with a readable used_percent must survive broken metadata:
    # benching relies on the percentage even when reset fields are junk.
    windows = parse_rate_limit_windows(
        {
            "primary_window": {
                "used_percent": 100,
                "limit_window_seconds": "soon",
                "reset_after_seconds": -5,
                "reset_at": None,
            }
        }
    )
    assert len(windows) == 1
    assert windows[0].used_percent == 100.0
    assert windows[0].window_seconds is None
    assert windows[0].reset_after_seconds is None
    assert windows[0].exhausted


def test_parse_accepts_resets_in_seconds_fallback_spelling() -> None:
    windows = parse_rate_limit_windows(
        {"primary_window": {"used_percent": 100, "resets_in_seconds": 120}}
    )
    assert windows[0].reset_after_seconds == 120


def test_parse_rejects_boolean_impostors() -> None:
    # bool is an int subclass in Python; True must not read as 1% or 1s.
    windows = parse_rate_limit_windows(
        {"primary_window": {"used_percent": True, "limit_window_seconds": True}}
    )
    assert windows[0].used_percent is None
    assert windows[0].window_seconds is None


def test_longest_window_ranks_by_duration_not_slot() -> None:
    five_hour = UsageWindow("primary_window", 18000, 12.0, 1, 100)
    weekly = UsageWindow("secondary_window", 604800, 4.0, 2, 200)
    assert longest_window([five_hour, weekly]) is weekly
    # Slot order must not matter: the weekly window wins from either slot.
    assert longest_window([weekly, five_hour]) is weekly


def test_longest_window_handles_unknown_durations_and_empty_lists() -> None:
    assert longest_window([]) is None
    lone = UsageWindow("primary_window", None, 10.0, None, None)
    assert longest_window([lone]) is lone
    # A window whose duration is known outranks one that cannot be ranked.
    known = UsageWindow("primary_window", 18000, 50.0, None, None)
    unknown = UsageWindow("secondary_window", None, 1.0, None, None)
    assert longest_window([known, unknown]) is known
    assert longest_window([unknown, known]) is known


def test_window_label_covers_common_horizons() -> None:
    assert window_label(604800) == "weekly"
    assert window_label(172800) == "2d"
    assert window_label(18000) == "5h"
    assert window_label(1800) == "30m"
    assert window_label(90) == "90s"
    assert window_label(None) is None
    assert window_label(0) is None
