# Completed: Models table and API list-price reference

## Metadata

- Created: 2026-10-25
- Status: Completed
- Completed: 2026-10-25
- Release: unreleased

## Context

The desktop Models page rendered each model as a wrapping row of free-text
chips ("2/2 accounts", "reasoning: low · medium · …", "structured: …",
"resolves to: …"). With a dozen OpenAI and Claude models the page read as
noise: no column headers, account coverage as prose, and no sense of how
expensive one model is next to another.

The relay runs on subscription quota, which is not billed per token, so there
is no real per-request price to show. The providers' published pay-as-you-go
API list prices are still a useful relative cost indicator: a model with a
higher list price usually consumes subscription limits faster too.

## Evidence (pricing pages, checked 2026-10-25)

- OpenAI, https://developers.openai.com/api/docs/pricing (Standard tier,
  short context up to 272K input tokens), USD per 1M tokens, input / cached
  input / output: `gpt-6-astra` 10 / 1.00 / 50, `gpt-5.6-sol` 4 / 0.40 / 20,
  `gpt-5.6-terra` 2 / 0.20 / 12, `gpt-5.6-luna` 0.20 / 0.02 / 1.20, `gpt-5.5`
  5 / 0.50 / 30, `gpt-5.4` 2.50 / 0.25 / 15, `gpt-5.4-mini` 0.75 / 0.075 /
  4.50; `gpt-image-2` text input 5 / cached 1.25, image output 30 (image
  input 8, cached 2). No price is published for `gpt-reserve` or
  `codex-auto-review`.
- Anthropic, https://platform.claude.com/docs/en/about-claude/pricing (base
  input / cache hits / output): Fable 5.1 10 / 0.25 / 50, Fable 5 10 / 1 / 50,
  Opus 5.5 4 / 0.20 / 20, Opus 5, 4.8, 4.7, 4.6 and 4.5 5 / 0.50 / 25,
  Sonnet 5.5 2 / 0.10 / 10, Sonnet 5 2 / 0.20 / 10, Sonnet 4.6 and 4.5
  3 / 0.30 / 15, Haiku 5.5 0.10 / 0.01 / 0.50 (prompts up to 100K tokens;
  0.50 / 2.50 beyond), Haiku 4.5 1 / 0.10 / 5.

## Plan

1. A static table in `airelay.model_pricing`: only prices read on those
   pages, each with source, date, and a basis note (tier, context threshold,
   modality). Exact ids and dated snapshots match; aliases are priced through
   their resolved model; unknown variants get no price rather than a
   sibling's.
2. `airelays.pricing` on every `/v1/models` entry (or `null`), with a
   `cost_tier` from 1 to 5 by output price. `airelays models` prints the list
   price beside each id.
3. The desktop Models view as one table per provider card: Model (with
   copy), Accounts (`supported / total` with a coverage bar, muted "all" when
   every account serves it), Reasoning (default mode highlighted), Structured
   output, Cost / 1M (input / output, tier, tooltip with the cached price,
   date and caveat; "—" with "no published price"), Notes (upstream-hidden,
   resolves to, configured).
4. Fit the 760 px minimum window: Notes hide below 1100 px (the badges move
   under the model id), Structured output and the tier below 860 px; the
   table scrolls inside its card, never the page.

## Validation

- Unit tests for the matcher (families, snapshots, unpublished ids, alias
  resolution), the table's consistency, and the payload shape.
- Browser test against the mock backend: headers, "2 / 2" and "1 / 2"
  coverage, a priced and an unpriced cost cell, sorting, filtering, keyboard
  copy, and no horizontal overflow at 1280, 1000 and 760 px.

## Outcome

- `src/airelay/model_pricing.py`; `airelays.pricing` in `ProviderModel.as_wire`;
  list prices in `airelays models`.
- `desktop/ui/js/views/models.js` renders the tables; rows are focusable
  (Enter copies the id, arrow keys move between rows). Rows are sorted listed
  before upstream-hidden, Claude aliases first, then by id. The view's
  lifecycle and reload logic are unchanged.
- The mock backend's `get_models` carries realistic metadata, including a
  partially covered, an upstream-hidden and an unpriced model.
- Exposed ids matched today: `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`,
  `gpt-5.6-luna`, `gpt-image-2`, every concrete `claude-*` id and the
  `claude:*` aliases through their resolution. Unpriced: `gpt-reserve`,
  `codex-auto-review`.
- Follow-up: prices change; the table is checked by hand at release time
  (`PRICES_AS_OF`). It is not fetched at runtime.

## Final validation

- `pytest -q`: 731 passed.
- `pytest -q desktop/tests/test_models_table_browser.py`: 6 passed.
