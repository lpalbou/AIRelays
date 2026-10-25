from __future__ import annotations

from dataclasses import replace

import pytest

from airelay.model_pricing import PRICES, match_price, pricing_payload
from airelay.providers import (
    ClaudeCliRuntime,
    _openai_image_model_record,
    _openai_model_record,
)
from airelay.config import Settings


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("gpt-6-astra", "gpt-6-astra"),
        ("GPT-5.6-Sol", "gpt-5.6-sol"),
        ("gpt-5.6-terra", "gpt-5.6-terra"),
        ("gpt-5.6-luna", "gpt-5.6-luna"),
        ("gpt-5.4-2026-03-05", "gpt-5.4"),
        ("gpt-image-2", "gpt-image-2"),
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ],
)
def test_priced_ids_match_their_family_and_dated_snapshots(model_id, family) -> None:
    assert match_price(model_id).family == family


@pytest.mark.parametrize(
    "model_id",
    # Unpublished or codename-only catalog entries, and variants of priced
    # families that the table does not list: never given a sibling's price.
    ["gpt-reserve", "codex-auto-review", "gpt-5.6-sol-pro", "gpt-5.4-nano",
     "claude:sonnet", "claude-opus-5-5-fast", "", None],
)
def test_unpublished_ids_have_no_price(model_id) -> None:
    assert match_price(model_id) is None
    assert pricing_payload(model_id) is None


def test_first_priced_candidate_wins_so_aliases_follow_their_resolution() -> None:
    assert match_price(None, "sonnet", "claude:sonnet") is None
    assert match_price("claude-sonnet-5-5", "sonnet", "claude:sonnet").family == "claude-sonnet-5-5"


def test_price_table_is_sourced_dated_and_consistent() -> None:
    assert len({price.family for price in PRICES}) == len(PRICES)
    for price in PRICES:
        assert price.source.startswith("https://")
        assert price.as_of == "2026-10-25"
        assert price.currency == "USD"
        assert price.input_per_million > 0 and price.output_per_million > 0
        if price.cached_input_per_million is not None:
            assert price.cached_input_per_million < price.input_per_million
        assert 1 <= price.cost_tier <= 5


def test_cost_tier_orders_models_by_output_price() -> None:
    tiers = {family: match_price(family).cost_tier for family in (
        "claude-haiku-5-5", "gpt-5.6-luna", "claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra",
    )}
    assert tiers == {
        "claude-haiku-5-5": 1, "gpt-5.6-luna": 2, "claude-sonnet-5-5": 3,
        "claude-opus-5-5": 4, "gpt-6-astra": 5,
    }


def test_models_payload_carries_pricing_or_null() -> None:
    priced = _openai_model_record("gpt-5.6-sol", {"slug": "gpt-5.6-sol"}).as_wire()["airelays"]["pricing"]
    assert priced == {
        "input_per_million": 4.0,
        "cached_input_per_million": 0.4,
        "output_per_million": 20.0,
        "currency": "USD",
        "unit": "per_million_tokens",
        "as_of": "2026-10-25",
        "source": "https://developers.openai.com/api/docs/pricing",
        "matched_family": "gpt-5.6-sol",
        "basis": priced["basis"],
        "cost_tier": 4,
    }
    assert "272K" in priced["basis"]
    hidden = _openai_model_record("gpt-reserve", {"slug": "gpt-reserve", "visibility": "hide"}).as_wire()
    assert hidden["airelays"]["pricing"] is None
    image = _openai_image_model_record("gpt-image-2").as_wire()["airelays"]["pricing"]
    assert image["matched_family"] == "gpt-image-2" and "image tokens" in image["basis"]


def test_claude_alias_is_priced_through_its_resolved_model(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    runtime = ClaudeCliRuntime(settings)
    unresolved = runtime.list_models()[0]
    assert unresolved["id"] == "claude:sonnet"
    assert unresolved["airelays"]["pricing"] is None
    record = runtime._models["claude:sonnet"]
    resolved = replace(record, resolved_model="claude-sonnet-5-5").as_wire()
    assert resolved["airelays"]["pricing"]["matched_family"] == "claude-sonnet-5-5"
    assert resolved["airelays"]["pricing"]["output_per_million"] == 10.0


def test_cli_formats_list_prices_like_the_dashboard() -> None:
    from airelay.cli import _usd

    assert [_usd(value) for value in (4.0, 0.2, 0.075, 1.25, 50, None)] == [
        "$4", "$0.20", "$0.075", "$1.25", "$50", "?",
    ]
