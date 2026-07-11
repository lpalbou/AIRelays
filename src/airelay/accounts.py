"""Multiple OpenAI accounts for one user: discovery, storage slots, and the
request pool that balances traffic across them.

Design (from the adversarial investigation):

- An account is a storage root. The legacy ``data_dir/auth.json`` (or its
  keyring entry) is the implicit first account; extra accounts live under
  ``data_dir/accounts/<slug>/``. Keyring entries key on the storage-root
  path, so per-directory slots isolate credentials with no auth changes.
- Accounts are discovered from storage, never declared in config.toml
  (desktop apps rewrite that file). Only the balancing strategy and the
  failover cooldown are config knobs.
- Requests pin one account for their whole lifetime (including the 401
  refresh retry inside the backend). Switching accounts is an explicit pool
  decision, logged as an ``account_failover`` traffic phase, and only
  happens before the first streamed byte reaches the client.
- Conversations stick to the account that served their first turn (the
  local conversation id is sent upstream as ``session_id``; alternating
  accounts under one session id would defeat prompt caching and look
  anomalous). Failover re-pins at turn boundaries.

Vocabulary note: this exists so one person can use their own multiple
subscriptions from one relay — it is not an account-sharing mechanism.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from airelay.auth import AuthenticationError, AuthManager, AuthRecord, AuthStorage
from airelay.backend import BackendError, ChatGptCodexBackend, SSEEvent
from airelay.config import Settings
from airelay.traffic import TrafficLogger
from airelay.usage_tally import WindowTokenTally

ACCOUNTS_DIRNAME = "accounts"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_SLUG = "default"
# Conservative failover triggers: quota/availability problems, never client
# errors (a 400/404 on the next account would just mask the real issue).
RETRIABLE_STATUS_MIN = 500
RETRIABLE_STATUS_EXACT = 429
USAGE_LIMIT_MARKERS = ("usage_limit_reached", "rate_limit_reached")
# Usage probes hit a personal upstream endpoint: cache briefly, coalesce
# concurrent callers, refresh in the background, and let routing trust the
# signal for a bounded window before falling back to plain rotation.
USAGE_CACHE_SECONDS = 60.0
USAGE_REFRESH_INTERVAL_SECONDS = 300.0
USAGE_ROUTING_MAX_AGE_SECONDS = 900.0


def _accounts_dir(data_dir: Path) -> Path:
    return data_dir / ACCOUNTS_DIRNAME


def _manifest_path(data_dir: Path) -> Path:
    return _accounts_dir(data_dir) / MANIFEST_FILENAME


def slug_for_account(account_id: str | None, email: str | None) -> str:
    """Stable, path-safe directory name for an account."""
    basis = account_id or email or "account"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^a-z0-9]+", "-", (email or basis).split("@")[0].lower()).strip("-")
    return f"{stem or 'account'}-{digest}"


def load_manifest(data_dir: Path) -> dict[str, Any]:
    path = _manifest_path(data_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_manifest(data_dir: Path, manifest: dict[str, Any]) -> None:
    path = _manifest_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


@dataclass
class AccountSlot:
    """One account's storage location plus lazily-loaded identity."""

    slug: str
    storage_root: Path
    email: str | None = None
    plan_type: str | None = None
    account_id: str | None = None
    authenticated: bool = False

    @property
    def label(self) -> str:
        return self.email or self.slug


def discover_slots(settings: Settings) -> list[AccountSlot]:
    """Finds every account slot: the legacy root first, then accounts/*,
    ordered by the manifest when present. Slots without credentials are
    skipped; identity comes from the stored record."""
    slots: list[AccountSlot] = []
    candidates: list[tuple[str, Path]] = [(DEFAULT_SLUG, settings.data_dir)]
    accounts_dir = _accounts_dir(settings.data_dir)
    if accounts_dir.is_dir():
        for child in sorted(accounts_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                candidates.append((child.name, child))

    seen_account_ids: set[str] = set()
    for slug, root in candidates:
        storage = AuthStorage(root, settings.auth_storage_mode)
        try:
            payload = storage.load()
        except RuntimeError:
            payload = None
        if not payload:
            continue
        # Email and plan live inside the signed id_token, not as top-level
        # keys; AuthRecord is the single source of truth for deriving them.
        record = AuthRecord(payload)
        if not (record.access_token or record.refresh_token):
            continue
        account_id = record.account_id
        # The same subscription enrolled twice would double-weight it and
        # "fail over" onto an already-limited account.
        if isinstance(account_id, str) and account_id:
            if account_id in seen_account_ids:
                continue
            seen_account_ids.add(account_id)
        slots.append(
            AccountSlot(
                slug=slug,
                storage_root=root,
                email=record.email,
                plan_type=record.plan_type,
                account_id=account_id if isinstance(account_id, str) else None,
                authenticated=record.authenticated,
            )
        )

    order: list[str] = []
    manifest_order = load_manifest(settings.data_dir).get("order")
    if isinstance(manifest_order, list):
        order = [entry for entry in manifest_order if isinstance(entry, str)]
    if order:
        rank = {slug: index for index, slug in enumerate(order)}
        slots.sort(key=lambda slot: rank.get(slot.slug, len(rank)))
    return slots


def find_slot(slots: list[AccountSlot], needle: str) -> AccountSlot | None:
    """Resolves an account by email, unambiguous email prefix, or slug."""
    slot, _ = resolve_slot(slots, needle)
    return slot


def resolve_slot(
    slots: list[AccountSlot], needle: str
) -> tuple[AccountSlot | None, str | None]:
    """Resolves an account, distinguishing 'unknown' from 'ambiguous' so the
    caller can give an actionable message rather than a misleading one."""
    lowered = needle.lower()
    exact = [s for s in slots if (s.email or "").lower() == lowered or s.slug == needle]
    if len(exact) == 1:
        return exact[0], None
    prefixed = [s for s in slots if (s.email or "").lower().startswith(lowered)]
    if len(prefixed) == 1:
        return prefixed[0], None
    if len(prefixed) > 1:
        matches = ", ".join(s.email or s.slug for s in prefixed)
        return None, f"`{needle}` matches {len(prefixed)} accounts: {matches}. Type more of the email."
    known = ", ".join(s.email or s.slug for s in slots) or "none"
    return None, f"Unknown account `{needle}`. Known accounts: {known}."


@dataclass
class _PooledAccount:
    slot: AccountSlot
    manager: AuthManager
    backend: ChatGptCodexBackend
    limited_until: float = 0.0
    # When the bench evidence was observed (monotonic). Usage-driven bench
    # release compares against the probe's start time so a stale snapshot
    # can never erase a bench placed after the snapshot was taken.
    limited_since: float = 0.0
    last_error: str | None = None
    # Rotation state: least-recently-selected wins. A plain round-robin
    # counter indexes a list whose membership changes between calls (health
    # and model filters), which starves accounts; a per-account timestamp is
    # fair under any churn.
    last_selected_at: float = 0.0
    # Capacity signal for the balanced strategy: the short-window (5h)
    # used_percent from the account's last usage probe, and when it was
    # observed. Plans differ wildly in absolute quota, so equalizing this
    # percentage is what actually balances charge across accounts.
    used_percent: float | None = None
    usage_observed_at: float = 0.0
    # Cached usage payload (for status consumers) with fetch time; protects
    # the aggressively personal upstream endpoint from polling storms.
    usage_payload: dict[str, Any] | None = None
    usage_fetched_at: float = 0.0
    # Cached lowercase model slugs this account exposes, with fetch time.
    models: frozenset[str] = field(default_factory=frozenset)
    models_fetched_at: float = 0.0

    def is_limited(self, now: float) -> bool:
        return self.limited_until > now


@dataclass
class _FailoverDecision:
    from_account: str
    to_account: str
    reason: str


class OpenAiAccountPool:
    """Duck-type compatible with ChatGptCodexBackend for the routes app.py
    serves, plus account-aware extensions. With one account every code path
    degenerates to today's single-account behavior."""

    def __init__(
        self,
        settings: Settings,
        traffic: TrafficLogger,
        slots: list[AccountSlot] | None = None,
        accounts: list[tuple[AuthManager, ChatGptCodexBackend]] | None = None,
    ) -> None:
        self._settings = settings
        self._traffic = traffic
        self._accounts: list[_PooledAccount] = []
        self._sticky: dict[str, str] = {}
        self._sticky_cap = 5000
        # Bench state of accounts that briefly left the pool (transient
        # keyring/storage read failures make a slot vanish and reappear);
        # without this, every flap would launder an active bench.
        self._bench_memory: dict[str, tuple[str | None, float, float, str | None]] = {}
        # Backends whose account was dropped by reconciliation; closed on
        # shutdown (or immediately when an event loop is available) instead
        # of leaking their connection pools.
        self._retired_backends: list[ChatGptCodexBackend] = []
        # Single-flight for usage probing: concurrent status consumers must
        # not multiply upstream hits on the rate-limited usage endpoint.
        self._usage_probe_lock = asyncio.Lock()
        # Ground-truth token breakdown behind the usage bars: what the relay
        # itself served per account/model in the current window.
        self._tally = WindowTokenTally(settings.data_dir / "openai-window-tokens.json")
        # Set when the pool owns discovery (production); None when the caller
        # supplied fixed accounts (tests), which disables live reload.
        self._discoverable = accounts is None
        self._last_signature: tuple[str, ...] | None = None
        self._last_reload_check = 0.0

        if accounts is not None:
            for manager, backend in accounts:
                slot = AccountSlot(slug=DEFAULT_SLUG, storage_root=manager.storage_root)
                record = manager.load()
                if record is not None:
                    slot.email = record.email
                    slot.plan_type = record.plan_type
                    slot.account_id = record.account_id
                    slot.authenticated = record.authenticated
                self._accounts.append(_PooledAccount(slot=slot, manager=manager, backend=backend))
            return

        self._install_slots(slots or [])
        self._last_signature = self._slot_signature(slots or [])

    def _make_pooled(self, slot: AccountSlot) -> _PooledAccount:
        manager = AuthManager(
            slot.storage_root,
            self._settings.auth_storage_mode,
            self._settings.issuer_base_url,
            client_id=self._settings.client_id,
        )
        backend = ChatGptCodexBackend(self._settings, manager, self._traffic)
        return _PooledAccount(slot=slot, manager=manager, backend=backend)

    def _install_slots(self, slots: list[AccountSlot]) -> None:
        self._accounts = [self._make_pooled(slot) for slot in slots]

    @staticmethod
    def _slot_signature(slots: list[AccountSlot]) -> tuple[str, ...]:
        # Order-sensitive: reordering accounts changes routing priority.
        return tuple(f"{slot.slug}:{slot.account_id or ''}" for slot in slots)

    def refresh_if_changed(self) -> bool:
        """Reconciles the pool with on-disk accounts so a newly added or
        removed account takes effect without a relay restart. Throttled to
        avoid re-scanning storage on every request; reconciles in place so
        existing accounts keep their cooldown state and in-flight requests
        are never interrupted. Returns True when the set changed."""
        if not self._discoverable:
            return False
        now = time.monotonic()
        # 10s: discovery does per-slot credential loads (keyring IPC on
        # macOS) on the event loop. At the old 2s throttle the desktop's
        # 1.5s status poll re-triggered it on every other poll; account
        # additions are rare and sign-in flows call hard_refresh anyway.
        if now - self._last_reload_check < 10.0:
            return False
        self._last_reload_check = now

        slots = discover_slots(self._settings)
        signature = self._slot_signature(slots)
        if signature == self._last_signature:
            return False

        existing = {account.slot.slug: account for account in self._accounts}
        reconciled: list[_PooledAccount] = []
        kept: set[str] = set()
        for slot in slots:
            current = existing.get(slot.slug)
            if current is not None and current.slot.account_id == slot.account_id:
                current.slot = slot  # refresh email/plan/auth metadata
                reconciled.append(current)
                kept.add(slot.slug)
            else:
                fresh = self._make_pooled(slot)
                # A slot that flapped out and back (transient storage read
                # failure) must not return with its bench laundered.
                remembered = self._bench_memory.pop(slot.slug, None)
                if remembered is not None:
                    account_id, limited_until, limited_since, last_error = remembered
                    if account_id == slot.account_id and limited_until > time.monotonic():
                        fresh.limited_until = limited_until
                        fresh.limited_since = limited_since
                        fresh.last_error = last_error
                reconciled.append(fresh)
        for slug, account in existing.items():
            if slug in kept:
                continue
            if account.limited_until > time.monotonic():
                self._bench_memory[slug] = (
                    account.slot.account_id,
                    account.limited_until,
                    account.limited_since,
                    account.last_error,
                )
                # Bounded: drop the oldest memory once past a sane cap.
                while len(self._bench_memory) > 64:
                    self._bench_memory.pop(next(iter(self._bench_memory)))
            self._retire_backend(account.backend)
        self._accounts = reconciled
        self._last_signature = signature
        self._traffic.write(
            {
                "phase": "account_pool_reloaded",
                "accounts": [slot for slot in signature],
            }
        )
        return True

    def _retire_backend(self, backend: ChatGptCodexBackend) -> None:
        """Closes a dropped account's HTTP client. Immediate when an event
        loop is running; otherwise deferred to close() so the connections
        are never simply abandoned."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._retired_backends.append(backend)
            return
        loop.create_task(backend.close())

    # ----- introspection -----

    @property
    def size(self) -> int:
        return len(self._accounts)

    def slots(self) -> list[AccountSlot]:
        return [account.slot for account in self._accounts]

    def account_statuses(self) -> list[dict[str, Any]]:
        self.refresh_if_changed()
        now = time.monotonic()
        statuses = []
        for account in self._accounts:
            status = account.manager.status()
            status["slug"] = account.slot.slug
            status["limited"] = account.is_limited(now)
            if account.is_limited(now):
                status["limited_for_seconds"] = int(account.limited_until - now)
            if account.last_error:
                status["last_error"] = account.last_error
            # Ground truth behind the usage bars: what this relay served on
            # this account in the current window, per model.
            window_tokens = self._tally.snapshot(account.slot.account_id)
            if window_tokens is not None:
                status["window_tokens"] = window_tokens
            statuses.append(status)
        return statuses

    def primary(self) -> _PooledAccount:
        if not self._accounts:
            raise AuthenticationError(
                "No ChatGPT login found. Run `airelays login` first.",
                code="upstream_auth_missing",
            )
        now = time.monotonic()
        for account in self._accounts:
            if not account.is_limited(now):
                return account
        return self._accounts[0]

    def manager_for_primary(self) -> AuthManager:
        return self.primary().manager

    async def warm_start(self, request_id: str = "startup") -> None:
        """Launch-time capacity and capability probe, so balancing is correct
        from the very first request instead of being relearned by failure.

        A fresh process knows nothing: an account exhausted before the
        restart would receive live traffic and waste a guaranteed 429, and
        the per-account model catalogs are empty so model-aware routing has
        nothing to route on. One usage probe per account benches the
        genuinely-limited ones; one models fetch per account fills the
        catalogs. Best effort: a probe failure leaves the account available
        (the reactive 429 path still protects it), and nothing here blocks
        serving — callers run it as a background task."""
        if len(self._accounts) < 2:
            return
        try:
            await self.subscription_statuses(request_id, force=True)
        except Exception:  # noqa: BLE001 - startup probing must never crash the relay
            pass
        for account in self._accounts:
            try:
                await self._account_models(account, request_id)
            except Exception:  # noqa: BLE001
                continue
        now = time.monotonic()
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "account_pool_warmed",
                "accounts": [
                    {
                        "account": account.slot.slug,
                        "limited": account.is_limited(now),
                        "models_known": len(account.models),
                    }
                    for account in self._accounts
                ],
            }
        )

    async def hard_refresh(self, request_id: str) -> list[dict[str, Any]]:
        """Manual capacity re-check. Every bench release is evidence-gated:
        an account leaves its bench only when a fresh, successful usage probe
        shows capacity (_bench_from_usage handles both directions). Benches
        are never cleared up front — the previous clear-first design opened a
        window in which live traffic hit a known-exhausted account and earned
        a fresh 429 before the re-probe landed."""
        self.refresh_if_changed()
        before = {
            account.slot.slug: account.is_limited(time.monotonic())
            for account in self._accounts
        }
        try:
            # Manual refresh wants fresh evidence, not the TTL cache.
            await self.subscription_statuses(request_id, force=True)
        except Exception:  # noqa: BLE001
            pass
        now = time.monotonic()
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "accounts_refresh",
                "accounts": [
                    {
                        "account": account.slot.slug,
                        "was_limited": before.get(account.slot.slug, False),
                        "limited": account.is_limited(now),
                    }
                    for account in self._accounts
                ],
            }
        )
        return self.account_statuses()

    # ----- selection -----

    def _healthy(self, now: float) -> list[_PooledAccount]:
        return [account for account in self._accounts if not account.is_limited(now)]

    def _select(self, session_id: str | None, model: str | None = None) -> _PooledAccount:
        if not self._accounts:
            raise AuthenticationError(
                "No ChatGPT login found. Run `airelays login` first.",
                code="upstream_auth_missing",
            )
        now = time.monotonic()
        # Conversation affinity: keep one session on one account (only if it
        # can still serve this model).
        if session_id and session_id in self._sticky:
            slug = self._sticky[session_id]
            for account in self._accounts:
                if (
                    account.slot.slug == slug
                    and not account.is_limited(now)
                    and self._model_supported(account, model)
                ):
                    # Sticky traffic counts as selection, or the balanced
                    # picker would see this account as idle and pile on.
                    account.last_selected_at = now
                    return account
        healthy = self._healthy(now)
        # Prefer accounts that support the requested model.
        model_healthy = [a for a in healthy if self._model_supported(a, model)]
        pick_from = model_healthy or healthy
        if not pick_from:
            # Every account is cooling down; least-recently-limited first.
            return min(self._accounts, key=lambda account: account.limited_until)
        strategy = self._settings.openai_balance
        if strategy == "ordered":
            chosen = pick_from[0]  # opt-in spillover: first healthy account
        elif strategy == "round_robin":
            # Strict equal request counts: least-recently-selected is fair
            # under membership churn, unlike a shared modulo counter over a
            # list whose contents change between calls.
            chosen = min(pick_from, key=lambda account: account.last_selected_at)
        else:  # balanced (default): equalize quota consumption
            chosen = self._pick_balanced(pick_from, now)
        chosen.last_selected_at = now
        if session_id:
            self._pin(session_id, chosen.slot.slug)
        return chosen

    def _pick_balanced(self, pick_from: list[_PooledAccount], now: float) -> _PooledAccount:
        """Routes to the account with the most remaining short-window quota
        (lowest used_percent), so consumption equalizes as a percentage of
        each plan's own capacity — equal request counts would drain a small
        plan many times faster than a large one. Accounts whose usage signal
        is missing or stale fall back to least-recently-selected, and win
        over usage-known accounts only when everything is stale."""
        fresh = [
            account
            for account in pick_from
            if account.used_percent is not None
            and now - account.usage_observed_at < USAGE_ROUTING_MAX_AGE_SECONDS
        ]
        if fresh:
            # Integer bucketing avoids ping-pong on sub-percent noise; ties
            # rotate via least-recently-selected.
            return min(
                fresh,
                key=lambda account: (int(account.used_percent or 0), account.last_selected_at),
            )
        return min(pick_from, key=lambda account: account.last_selected_at)

    def _pin(self, session_id: str, slug: str) -> None:
        # Evict oldest affinities instead of wiping the whole map: a full
        # clear would flip every active conversation to a different account
        # at once, defeating upstream prompt caching en masse.
        self._sticky.pop(session_id, None)
        while len(self._sticky) >= self._sticky_cap:
            self._sticky.pop(next(iter(self._sticky)))
        self._sticky[session_id] = slug

    # ----- failure classification -----

    @staticmethod
    def _structured_error_type(detail: str) -> str | None:
        """The upstream error `type` when the body is the structured JSON
        error shape; None otherwise. Substring scanning of arbitrary bodies
        is not safe here — a 400 whose body merely echoes request content
        containing a marker string must never bench the pool."""
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("type"), str):
            return error["type"]
        return None

    def _cooldown_seconds(self, error: BackendError) -> float | None:
        """Returns the cooldown when the error should trigger failover."""
        detail = error.detail or ""
        error_type = self._structured_error_type(detail)
        limit_hit = error_type in USAGE_LIMIT_MARKERS or (
            error.status_code == RETRIABLE_STATUS_EXACT
            and any(marker in detail for marker in USAGE_LIMIT_MARKERS)
        )
        if limit_hit or error.status_code == RETRIABLE_STATUS_EXACT:
            try:
                payload = json.loads(detail)
                resets = payload.get("error", {}).get("resets_in_seconds")
                if isinstance(resets, (int, float)) and resets > 0:
                    return float(resets)
            except (json.JSONDecodeError, AttributeError):
                pass
            return float(self._settings.openai_account_cooldown_seconds)
        if error.status_code == 401:
            # One account's credentials are dead (the backend already spent
            # its refresh retry). Other accounts can still serve; recovery
            # needs user action, so bench for the full cooldown.
            return float(self._settings.openai_account_cooldown_seconds)
        if error.status_code >= RETRIABLE_STATUS_MIN:
            # Transient 5xx: short cooldown so one bad gateway response does
            # not bench an account for minutes.
            return min(30.0, float(self._settings.openai_account_cooldown_seconds))
        # Client errors (400/403/404/422...) would fail identically on the
        # next account; failing over would just mask the real problem.
        return None

    def _failover_cooldown(self, error: Exception) -> float | None:
        """Cooldown for any exception the attempt loop may see. Transport
        and auth failures are account-scoped problems: the next account gets
        its chance instead of the client eating the error."""
        if isinstance(error, BackendError):
            return self._cooldown_seconds(error)
        if isinstance(error, AuthenticationError):
            return float(self._settings.openai_account_cooldown_seconds)
        return None

    def _mark_limited(self, account: _PooledAccount, seconds: float, reason: str) -> None:
        now = time.monotonic()
        # Extend-only: a short transient-error cooldown must never truncate
        # an authoritative multi-hour usage bench.
        account.limited_until = max(account.limited_until, now + seconds)
        account.limited_since = now
        account.last_error = reason

    def _log_failover(
        self, request_id: str, decision: _FailoverDecision, cooldown: float
    ) -> None:
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "account_failover",
                "from_account": decision.from_account,
                "to_account": decision.to_account,
                "reason": decision.reason[:500],
                "cooldown_seconds": round(cooldown, 1),
            }
        )

    def _model_supported(self, account: _PooledAccount, model: str | None) -> bool:
        """A request may only route to an account that exposes the model.
        Unknown models (empty cache, or a slug never listed) are allowed
        through — clients legitimately send unlisted ids, and the upstream
        is the final authority."""
        if not model or not account.models:
            return True
        return model.lower() in account.models

    def _attempt_order(
        self, session_id: str | None, model: str | None = None
    ) -> list[_PooledAccount]:
        first = self._select(session_id, model)
        now = time.monotonic()
        # Fallbacks in health order: healthy accounts before benched ones
        # (a known-benched account would waste a guaranteed-429 round trip),
        # benched ones by earliest recovery.
        rest = sorted(
            (account for account in self._accounts if account is not first),
            key=lambda account: (account.is_limited(now), account.limited_until),
        )
        ordered = [first, *rest]
        # Prefer accounts that support the model; keep the rest as last-ditch
        # fallbacks so a request is never dropped purely on a stale cache.
        supported = [a for a in ordered if self._model_supported(a, model)]
        unsupported = [a for a in ordered if not self._model_supported(a, model)]
        return supported + unsupported if supported else ordered

    def _log_selection(self, request_id: str, account: _PooledAccount, attempt: int) -> None:
        # `attempt` disambiguates served traffic from failover retries: a
        # failed-over request writes one record per attempt, so counting
        # attempt-0 records is what measures the selection distribution.
        if len(self._accounts) > 1:
            self._traffic.write(
                {
                    "request_id": request_id,
                    "phase": "account_selected",
                    "account_id": account.slot.account_id,
                    "account": account.slot.slug,
                    "attempt": attempt,
                }
            )

    def _repin(self, session_id: str | None, account: _PooledAccount) -> None:
        if session_id:
            self._pin(session_id, account.slot.slug)

    # ----- backend surface consumed by app.py -----

    async def collect_response(
        self, payload: dict[str, Any], request_id: str, session_id: str | None
    ) -> dict[str, Any]:
        self.refresh_if_changed()
        model = payload.get("model") if isinstance(payload, dict) else None
        attempts = self._attempt_order(session_id, model)
        last_error: Exception | None = None
        for index, account in enumerate(attempts):
            self._log_selection(request_id, account, index)
            try:
                result = await account.backend.collect_response(payload, request_id, session_id)
                self._repin(session_id, account)
                if isinstance(result, dict):
                    self._tally.record(
                        account.slot.account_id, result.get("model"), result.get("usage")
                    )
                return result
            except (BackendError, AuthenticationError) as error:
                cooldown = self._failover_cooldown(error)
                if cooldown is None:
                    raise self._final_error(error) from error
                # Bench on every failing attempt, including the last one:
                # otherwise an exhausted final account is re-selected as
                # "healthy" by every subsequent request and hammered with
                # guaranteed 429s.
                self._mark_limited(account, cooldown, self._error_reason(error))
                if index == len(attempts) - 1:
                    raise self._final_error(error) from error
                self._log_failover(
                    request_id,
                    _FailoverDecision(
                        from_account=account.slot.slug,
                        to_account=attempts[index + 1].slot.slug,
                        reason=self._error_reason(error),
                    ),
                    cooldown,
                )
                last_error = error
        raise self._final_error(last_error or BackendError(502, "No account available."))

    async def stream_response_events(
        self, payload: dict[str, Any], request_id: str, session_id: str | None
    ) -> AsyncIterator[SSEEvent]:
        self.refresh_if_changed()
        model = payload.get("model") if isinstance(payload, dict) else None
        attempts = self._attempt_order(session_id, model)
        for index, account in enumerate(attempts):
            self._log_selection(request_id, account, index)
            stream = account.backend.stream_response_events(payload, request_id, session_id)
            started = False
            try:
                async for event in stream:
                    if not started:
                        started = True
                        self._repin(session_id, account)
                    if event.event == "response.completed":
                        self._record_stream_usage(account, event)
                    yield event
                return
            except (BackendError, AuthenticationError) as error:
                # Failover is only honest before the first byte reached the
                # client; afterwards the stream must die visibly.
                cooldown = self._failover_cooldown(error)
                if cooldown is None or started:
                    raise self._final_error(error) from error
                self._mark_limited(account, cooldown, self._error_reason(error))
                if index == len(attempts) - 1:
                    raise self._final_error(error) from error
                self._log_failover(
                    request_id,
                    _FailoverDecision(
                        from_account=account.slot.slug,
                        to_account=attempts[index + 1].slot.slug,
                        reason=self._error_reason(error),
                    ),
                    cooldown,
                )

    @staticmethod
    def _error_reason(error: Exception) -> str:
        if isinstance(error, BackendError):
            return f"status {error.status_code}: {error.detail[:160]}"
        return f"auth: {str(error)[:160]}"

    def _record_stream_usage(self, account: _PooledAccount, event: SSEEvent) -> None:
        """Feeds the token tally from a completed streamed response — the
        one event per request that carries the final usage object."""
        try:
            parsed = json.loads(event.data)
        except json.JSONDecodeError:
            return
        response = parsed.get("response") if isinstance(parsed, dict) else None
        if isinstance(response, dict):
            self._tally.record(
                account.slot.account_id, response.get("model"), response.get("usage")
            )

    async def _account_models(self, account: _PooledAccount, request_id: str) -> frozenset[str]:
        """Cached per-account model slugs, refreshed on the same TTL as the
        registry's models cache."""
        ttl = max(30.0, float(self._settings.models_cache_ttl_seconds))
        now = time.monotonic()
        if account.models and now - account.models_fetched_at < ttl:
            return account.models
        payload = await account.backend.list_models(request_id)
        slugs: set[str] = set()
        models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(models, list):
            for item in models:
                if isinstance(item, dict) and isinstance(item.get("slug"), str):
                    slugs.add(item["slug"].lower())
        account.models = frozenset(slugs)
        account.models_fetched_at = now
        return account.models

    async def list_models(self, request_id: str) -> dict[str, Any]:
        # Advertise only models every authenticated account can serve, so a
        # balanced or failed-over request never lands on an account missing
        # the requested model. Falls back to the primary account's list when
        # only one account is enrolled.
        authed = [a for a in self._accounts if a.slot.authenticated]
        if len(authed) <= 1:
            return await self.primary().backend.list_models(request_id)
        raw = await self.primary().backend.list_models(request_id)
        common: set[str] | None = None
        for account in authed:
            try:
                slugs = await self._account_models(account, request_id)
            except (BackendError, AuthenticationError):
                continue
            common = slugs if common is None else (common & slugs)
        if not common:
            return raw
        models = raw.get("models") if isinstance(raw, dict) else None
        if isinstance(models, list):
            raw = dict(raw)
            raw["models"] = [
                item
                for item in models
                if isinstance(item, dict)
                and isinstance(item.get("slug"), str)
                and item["slug"].lower() in common
            ]
        return raw

    async def get_subscription_status(
        self, request_id: str, slug: str | None = None
    ) -> dict[str, Any]:
        account = None
        if slug is not None:
            for candidate in self._accounts:
                if candidate.slot.slug == slug:
                    account = candidate
                    break
            if account is None:
                raise BackendError(404, f"Unknown account `{slug}`.")
        else:
            account = self.primary()
        return await self._probe_usage(account, request_id)

    async def _probe_usage(
        self, account: _PooledAccount, request_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        """One account's usage with a short TTL cache. Every successful probe
        feeds the bench lifecycle and the balanced strategy's capacity
        signal, so any status consumer keeps routing honest for free."""
        now = time.monotonic()
        if (
            not force
            and account.usage_payload is not None
            and now - account.usage_fetched_at < USAGE_CACHE_SECONDS
        ):
            return json.loads(json.dumps(account.usage_payload))
        probe_started = time.monotonic()
        usage = await account.backend.get_subscription_status(request_id)
        account.usage_payload = usage
        account.usage_fetched_at = time.monotonic()
        self._bench_from_usage(account, usage, probe_started)
        return json.loads(json.dumps(usage))

    async def subscription_statuses(
        self, request_id: str, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Per-account usage; errors are reported per account, not fatal.

        Doubles as a proactive limit check: an account whose usage already
        reports a reached limit is benched here, so it is skipped without
        first wasting a request that would just 429. Single-flighted and
        TTL-cached: concurrent status consumers must not multiply hits on
        the personal upstream usage endpoint."""
        self.refresh_if_changed()
        async with self._usage_probe_lock:
            results: list[dict[str, Any]] = []
            for account in self._accounts:
                entry: dict[str, Any] = {
                    "slug": account.slot.slug,
                    "email": account.slot.email,
                }
                try:
                    entry["payload"] = await self._probe_usage(
                        account, request_id, force=force
                    )
                except (BackendError, AuthenticationError) as error:
                    entry["error"] = str(error)
                results.append(entry)
            return results

    async def usage_refresh_loop(self) -> None:
        """Background capacity refresher for the balanced strategy: without
        it the routing signal would age out between manual status loads and
        proactive benching would only happen reactively. Runs forever; the
        app cancels the task on shutdown. ~12 probes/hour/account."""
        while True:
            await asyncio.sleep(USAGE_REFRESH_INTERVAL_SECONDS)
            if len(self._accounts) < 2:
                continue
            try:
                await self.subscription_statuses("usage-refresh")
            except Exception:  # noqa: BLE001 - the refresher must never die
                continue

    def _bench_from_usage(
        self, account: _PooledAccount, usage: dict[str, Any], probe_started: float
    ) -> None:
        """Proactively cools down an account whose upstream usage already
        reports a reached limit, and releases a benched account when a fresh
        probe shows capacity. ``probe_started`` gates the release: a snapshot
        fetched before the bench was placed carries no evidence about it (the
        429 may have happened after the snapshot), so it must not erase it."""
        if not isinstance(usage, dict):
            return
        # Every reached-limit signal the payload carries: the nullable
        # reached-type object, the explicit booleans, and the window math.
        # Relying on a single undocumented field is one upstream tweak away
        # from releasing an exhausted account back into rotation.
        reached = bool(usage.get("rate_limit_reached_type"))
        rate = usage.get("rate_limit")
        windows = []
        if isinstance(rate, dict):
            if rate.get("limit_reached") is True or rate.get("allowed") is False:
                reached = True
            windows = [rate.get("primary_window"), rate.get("secondary_window")]
            # Capacity signal for balanced routing: the short-window
            # percentage, recorded on every probe (limited or not).
            primary = rate.get("primary_window")
            if isinstance(primary, dict) and isinstance(
                primary.get("used_percent"), (int, float)
            ):
                account.used_percent = float(primary["used_percent"])
                account.usage_observed_at = time.monotonic()
            # Window identity for the token tally: a changed reset anchor
            # means the 5h bucket rolled and the breakdown starts fresh.
            if isinstance(primary, dict):
                self._tally.set_window(account.slot.account_id, primary.get("reset_at"))
        resets: list[int] = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            if isinstance(used, (int, float)) and used >= 100:
                reached = True
                secs = window.get("reset_after_seconds")
                if not isinstance(secs, (int, float)) or secs <= 0:
                    secs = window.get("resets_in_seconds")
                if isinstance(secs, (int, float)) and secs > 0:
                    resets.append(int(secs))
        now = time.monotonic()
        if not reached:
            # Authoritative recovery signal: usage says there is capacity, so
            # release any bench rather than waiting out an over-long
            # estimate — but only when the snapshot is fresher than the
            # bench evidence.
            if account.limited_until > now and probe_started > account.limited_since:
                account.limited_until = 0.0
                account.last_error = None
            return
        # The account cannot serve until every exhausted window has reset:
        # with a maxed 5h window AND a maxed weekly window, using the shorter
        # reset would re-bench-flap every five hours for days.
        cooldown = float(max(resets)) if resets else float(self._settings.openai_account_cooldown_seconds)
        # Only extend, never shorten, an existing bench.
        if account.limited_until < now + cooldown:
            account.limited_until = now + cooldown
            account.limited_since = now
            account.last_error = "usage limit reached (from usage report)"

    async def close(self) -> None:
        self._tally.save()
        for account in self._accounts:
            await account.backend.close()
        retired, self._retired_backends = self._retired_backends, []
        for backend in retired:
            await backend.close()

    def _final_error(self, error: Exception) -> Exception:
        if (
            isinstance(error, BackendError)
            and len(self._accounts) > 1
            and self._cooldown_seconds(error) is not None
        ):
            now = time.monotonic()
            # Only claim "all unavailable" when it is true.
            if all(account.is_limited(now) for account in self._accounts):
                earliest = min(account.limited_until - now for account in self._accounts)
                names = len(self._accounts)
                return BackendError(
                    error.status_code,
                    f"All {names} OpenAI accounts are at their limits "
                    f"(earliest retry in {max(1, int(earliest))}s). "
                    f"Last error: {error.detail[:300]}",
                )
        return error


def build_pool(settings: Settings, traffic: TrafficLogger) -> OpenAiAccountPool:
    return OpenAiAccountPool(settings, traffic, slots=discover_slots(settings))
