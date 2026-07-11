# Deprecated (rejected): Absolute account-capacity estimation from usage history

## Metadata

- Created: 2026-07-11
- Status: Deprecated (rejected after adversarial investigation; not built)

## Question investigated

Can an OpenAI account's absolute 5h-window capacity (input/output tokens) be estimated from the relay's own usage history (per-request token accounting correlated with used_percent movement), and should the relay ship such an estimate?

## What the investigation established

- Window semantics (from live log evidence): the 5h window is a fixed bucket anchored at first use — `reset_at` freezes at `first_use + 18000s`, `used_percent` grows cumulatively, then hard-resets. Pairing snapshots with identical `reset_at` and differencing percent is therefore well-posed.
- Empirical tokens-per-percent (Jul 11 logs, pairs with identical `reset_at`, Δpercent ≥ 2, censored at 100%):
  - plus account: median ≈ 22k tokens/percent → ≈ 2.2M tokens per 5h window, but cross-pair spread 316–651%
  - enterprise account: median ≈ 0.58–0.89M tokens/percent → ≈ 58–89M tokens per 5h window, spread 150–234%
  - a cached-discounted basis (cached input at 0.1 weight) consistently reduced spread, consistent with credit-weighted upstream metering
- Why a shipped estimate was rejected:
  1. The unit is wrong: the upstream quota is credit-denominated with per-model, per-token-class weights spanning ~400x (published Codex rate card), so "capacity in tokens" depends entirely on traffic mix — the observed spreads are structural, not sampling noise.
  2. Integer percent quantization makes large plans nearly unobservable (~1 point per ~1M tokens → ±25–100% error for a long time).
  3. Usage outside the relay (same subscription in first-party apps) biases estimates downward by an unknowable amount; window roll effects bias upward.
  4. ADR 0001 forbids invented token budgets; a point estimate is exactly that, and even an honest range ("30–300M depending on mix") is practically useless.
  5. Routing must not consume it: balanced routing's percent equalization is already the correct fixed point (it splits traffic proportionally to capacity without knowing it, and all accounts exhaust simultaneously); absolute-remaining routing would front-load the large account and behave worse under estimate error.
- The wham/usage payload exposes no absolute quota anywhere (full field inventory checked), and public plan documentation covers neither the unit nor enterprise org policies.

## Sanctioned fallback (not committed)

If capacity visibility is ever wanted in UI, the honest reduced form is the observed exchange rate composed purely of ground truth — e.g. "this 5h window: 1.9M tokens via relay ≈ 47 points" — with no extrapolation, no persistence, no routing coupling. If the upstream ever exposes credit-denominated usage (the payload's `credits` block shows the shape exists), read it instead of estimating.

## Related

- completed/accounts/0016_capacity_aware_balanced_routing.md (percent equalization; why absolute capacity is unnecessary for routing)
- completed/accounts/0013_openai_usage_probe_caching_and_single_flight.md (probe cadence the estimator would have used)
- docs/adr/0001-no-silent-degradation-truncation-or-budget-invention.md
