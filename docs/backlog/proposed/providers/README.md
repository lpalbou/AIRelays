# Provider runtimes: proposed follow-ups

## Status

Proposed (not committed)

## Purpose

Follow-up ideas for the provider runtimes, triaged from completed work.
Promote to `planned/` only when evidence shows urgency.

## Items

- [0015_claude_token_shape_validation_on_set_token.md](0015_claude_token_shape_validation_on_set_token.md)
- [0018_metered_embeddings_capability_slice.md](0018_metered_embeddings_capability_slice.md):
  single opt-in API key serving `/v1/embeddings` only (capability the
  subscriptions physically cannot provide). Deliberately uncertain — the owner's
  own framing is "may be useless, except to simplify key management, which may or
  may not be a good thing"; requires a new ADR amending ADR 0002 before any
  implementation.
- [0028_disclose_dropped_content_part_fields.md](0028_disclose_dropped_content_part_fields.md):
  content-part fields the relay drops (e.g. Anthropic `cache_control`) are listed in
  `x-airelays-ignored-parameters` like top-level ones; per-runtime prompt-caching docs.
