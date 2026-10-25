"""Reference list prices for the models the relay exposes.

These are the providers' published pay-as-you-go API list prices (USD per
1M tokens, standard tier). AIRelays serves requests from subscription quota
(ChatGPT/Codex and Claude plans), which is not billed per token, so nothing
here is what a relay request costs. The table is only a relative cost
indicator: a model with a higher list price usually consumes subscription
limits faster too.

Only prices read on the providers' pricing pages are recorded, each with its
source and the date it was checked. A model without a published price (a
codename-only Codex catalog entry, an upstream-hidden model) has no entry
and reports `pricing: null`; no price is inferred from a sibling model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PRICES_AS_OF = "2026-10-25"
OPENAI_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
CLAUDE_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

_OPENAI_SHORT_CONTEXT = "Standard tier, short context (up to 272K input tokens); longer prompts are priced higher."
_CLAUDE_STANDARD = "Standard API price; cached input is the cache-hit price (cache writes cost more)."


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """One model family's list price, in USD per 1M tokens."""

    family: str
    input_per_million: float
    cached_input_per_million: float | None
    output_per_million: float
    source: str
    basis: str
    as_of: str = PRICES_AS_OF
    currency: str = "USD"
    unit: str = "per_million_tokens"

    @property
    def cost_tier(self) -> int:
        """1 to 5 by output price, for a compact "$$$" display."""
        output = self.output_per_million
        if output <= 1:
            return 1
        if output <= 5:
            return 2
        if output <= 15:
            return 3
        if output <= 30:
            return 4
        return 5

    def as_wire(self) -> dict[str, Any]:
        return {
            "input_per_million": self.input_per_million,
            "cached_input_per_million": self.cached_input_per_million,
            "output_per_million": self.output_per_million,
            "currency": self.currency,
            "unit": self.unit,
            "as_of": self.as_of,
            "source": self.source,
            "matched_family": self.family,
            "basis": self.basis,
            "cost_tier": self.cost_tier,
        }


def _openai(family: str, input_: float, cached: float | None, output: float,
            basis: str = _OPENAI_SHORT_CONTEXT) -> ModelPrice:
    return ModelPrice(family, input_, cached, output, OPENAI_PRICING_SOURCE, basis)


def _claude(family: str, input_: float, cached: float | None, output: float,
            basis: str = _CLAUDE_STANDARD) -> ModelPrice:
    return ModelPrice(family, input_, cached, output, CLAUDE_PRICING_SOURCE, basis)


# Checked on the pages above on PRICES_AS_OF. Keys are lowercase model ids.
PRICES: tuple[ModelPrice, ...] = (
    _openai("gpt-6-astra", 10.00, 1.00, 50.00),
    _openai("gpt-6.1-sol", 2.00, 0.10, 10.00),
    _openai("gpt-6-sol", 2.00, 0.20, 10.00),
    _openai("gpt-6-luna", 0.10, 0.01, 0.50),
    _openai("gpt-5.6-sol", 4.00, 0.40, 20.00),
    _openai("gpt-5.6-terra", 2.00, 0.20, 12.00),
    _openai("gpt-5.6-luna", 0.20, 0.02, 1.20),
    _openai("gpt-5.5", 5.00, 0.50, 30.00),
    _openai("gpt-5.4", 2.50, 0.25, 15.00),
    _openai("gpt-5.4-mini", 0.75, 0.075, 4.50, basis="Standard tier."),
    _openai(
        "gpt-image-2", 5.00, 1.25, 30.00,
        basis="Input and cached input are text tokens; output is image tokens "
              "(image input tokens: $8.00, cached $2.00).",
    ),
    _claude("claude-fable-5-1", 10.00, 0.25, 50.00),
    _claude("claude-fable-5", 10.00, 1.00, 50.00),
    _claude("claude-opus-5-5", 4.00, 0.20, 20.00),
    _claude("claude-opus-5", 5.00, 0.50, 25.00),
    _claude("claude-opus-4-8", 5.00, 0.50, 25.00),
    _claude("claude-opus-4-7", 5.00, 0.50, 25.00),
    _claude("claude-opus-4-6", 5.00, 0.50, 25.00),
    _claude("claude-opus-4-5", 5.00, 0.50, 25.00),
    _claude("claude-sonnet-5-5", 2.00, 0.10, 10.00),
    _claude("claude-sonnet-5", 2.00, 0.20, 10.00),
    _claude("claude-sonnet-4-6", 3.00, 0.30, 15.00),
    _claude("claude-sonnet-4-5", 3.00, 0.30, 15.00),
    _claude(
        "claude-haiku-5-5", 0.10, 0.01, 0.50,
        basis="Prompts up to 100K tokens; longer prompts cost $0.50 input / "
              "$2.50 output. Cached input is the cache-hit price.",
    ),
    _claude("claude-haiku-4-5", 1.00, 0.10, 5.00),
)

_BY_FAMILY = {price.family: price for price in PRICES}
# A family key also covers its dated snapshots (claude-haiku-4-5-20251001,
# gpt-5.4-2026-03-05), and nothing else: gpt-5.6-sol-pro is not gpt-5.6-sol.
_SNAPSHOT = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def match_price(*model_ids: str | None) -> ModelPrice | None:
    """The list price for the first id that names a priced family.

    Pass the most concrete id first: for `claude:sonnet` that is its
    `resolved_model` (aliases have no price of their own and follow what
    the CLI resolves them to).
    """
    for model_id in model_ids:
        if not isinstance(model_id, str) or not model_id:
            continue
        normalized = model_id.strip().lower()
        price = _BY_FAMILY.get(normalized) or _BY_FAMILY.get(_SNAPSHOT.sub("", normalized))
        if price is not None:
            return price
    return None


def pricing_payload(*model_ids: str | None) -> dict[str, Any] | None:
    """`airelays.pricing` for a `/v1/models` entry, or None when unpublished."""
    price = match_price(*model_ids)
    return price.as_wire() if price is not None else None
