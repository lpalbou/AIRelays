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

ACCOUNTS_DIRNAME = "accounts"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_SLUG = "default"
# Conservative failover triggers: quota/availability problems, never client
# errors (a 400/404 on the next account would just mask the real issue).
RETRIABLE_STATUS_MIN = 500
RETRIABLE_STATUS_EXACT = 429
USAGE_LIMIT_MARKERS = ("usage_limit_reached", "rate_limit_reached")


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
    last_error: str | None = None
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
        self._rr_next = 0
        self._sticky: dict[str, str] = {}
        self._sticky_cap = 5000
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
        for slot in slots:
            current = existing.get(slot.slug)
            if current is not None and current.slot.account_id == slot.account_id:
                current.slot = slot  # refresh email/plan/auth metadata
                reconciled.append(current)
            else:
                reconciled.append(self._make_pooled(slot))
        # Retired accounts simply drop out; their httpx clients are closed on
        # app shutdown via close(), and abandoning them here avoids racing an
        # in-flight request that still holds a reference.
        self._accounts = reconciled
        self._last_signature = signature
        self._traffic.write(
            {
                "phase": "account_pool_reloaded",
                "accounts": [slot for slot in signature],
            }
        )
        return True

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

    def clear_cooldowns(self) -> None:
        """Forgets every bench so all accounts are immediately eligible
        again. Used by the manual hard-refresh; genuinely-limited accounts
        are re-benched by the following usage re-probe."""
        for account in self._accounts:
            account.limited_until = 0.0
            account.last_error = None

    async def hard_refresh(self, request_id: str) -> list[dict[str, Any]]:
        """Manual override: reload accounts, clear all benches, then re-probe
        usage so only genuinely-exhausted accounts stay benched. Returns the
        resulting per-account status."""
        self.refresh_if_changed()
        self.clear_cooldowns()
        # Re-probe usage; _bench_from_usage re-benches the truly-limited and
        # leaves the rest available. A probe failure leaves the account
        # available, which is the intended "just in case" bias.
        try:
            await self.subscription_statuses(request_id)
        except Exception:  # noqa: BLE001
            pass
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
                    return account
        healthy = self._healthy(now)
        # Prefer accounts that support the requested model.
        model_healthy = [a for a in healthy if self._model_supported(a, model)]
        pick_from = model_healthy or healthy
        if not pick_from:
            # Every account is cooling down; least-recently-limited first.
            return min(self._accounts, key=lambda account: account.limited_until)
        if self._settings.openai_balance == "round_robin":
            chosen = pick_from[self._rr_next % len(pick_from)]
            self._rr_next += 1
        else:  # ordered spillover (default)
            chosen = pick_from[0]
        if session_id:
            if len(self._sticky) >= self._sticky_cap:
                self._sticky.clear()
            self._sticky[session_id] = chosen.slot.slug
        return chosen

    # ----- failure classification -----

    def _cooldown_seconds(self, error: BackendError) -> float | None:
        """Returns the cooldown when the error should trigger failover."""
        detail = error.detail or ""
        limit_hit = any(marker in detail for marker in USAGE_LIMIT_MARKERS)
        if not limit_hit and error.status_code != RETRIABLE_STATUS_EXACT and error.status_code < RETRIABLE_STATUS_MIN:
            return None
        if limit_hit or error.status_code == RETRIABLE_STATUS_EXACT:
            try:
                payload = json.loads(detail)
                resets = payload.get("error", {}).get("resets_in_seconds")
                if isinstance(resets, (int, float)) and resets > 0:
                    return float(resets)
            except (json.JSONDecodeError, AttributeError):
                pass
            return float(self._settings.openai_account_cooldown_seconds)
        # Transient 5xx: short cooldown so one bad gateway response does not
        # bench an account for minutes.
        return min(30.0, float(self._settings.openai_account_cooldown_seconds))

    def _mark_limited(self, account: _PooledAccount, seconds: float, reason: str) -> None:
        account.limited_until = time.monotonic() + seconds
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
        rest = [account for account in self._accounts if account is not first]
        ordered = [first, *rest]
        # Prefer accounts that support the model; keep the rest as last-ditch
        # fallbacks so a request is never dropped purely on a stale cache.
        supported = [a for a in ordered if self._model_supported(a, model)]
        unsupported = [a for a in ordered if not self._model_supported(a, model)]
        return supported + unsupported if supported else ordered

    def _log_selection(self, request_id: str, account: _PooledAccount) -> None:
        if len(self._accounts) > 1:
            self._traffic.write(
                {
                    "request_id": request_id,
                    "phase": "account_selected",
                    "account_id": account.slot.account_id,
                    "account": account.slot.slug,
                }
            )

    def _repin(self, session_id: str | None, account: _PooledAccount) -> None:
        if session_id:
            self._sticky[session_id] = account.slot.slug

    # ----- backend surface consumed by app.py -----

    async def collect_response(
        self, payload: dict[str, Any], request_id: str, session_id: str | None
    ) -> dict[str, Any]:
        self.refresh_if_changed()
        model = payload.get("model") if isinstance(payload, dict) else None
        attempts = self._attempt_order(session_id, model)
        last_error: Exception | None = None
        for index, account in enumerate(attempts):
            self._log_selection(request_id, account)
            try:
                result = await account.backend.collect_response(payload, request_id, session_id)
                self._repin(session_id, account)
                return result
            except BackendError as error:
                cooldown = self._cooldown_seconds(error)
                if cooldown is None or index == len(attempts) - 1:
                    raise self._final_error(error) from error
                self._mark_limited(account, cooldown, error.detail[:200])
                self._log_failover(
                    request_id,
                    _FailoverDecision(
                        from_account=account.slot.slug,
                        to_account=attempts[index + 1].slot.slug,
                        reason=f"status {error.status_code}",
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
            self._log_selection(request_id, account)
            stream = account.backend.stream_response_events(payload, request_id, session_id)
            started = False
            try:
                async for event in stream:
                    if not started:
                        started = True
                        self._repin(session_id, account)
                    yield event
                return
            except BackendError as error:
                # Failover is only honest before the first byte reached the
                # client; afterwards the stream must die visibly.
                cooldown = self._cooldown_seconds(error)
                if started or cooldown is None or index == len(attempts) - 1:
                    raise self._final_error(error) from error
                self._mark_limited(account, cooldown, error.detail[:200])
                self._log_failover(
                    request_id,
                    _FailoverDecision(
                        from_account=account.slot.slug,
                        to_account=attempts[index + 1].slot.slug,
                        reason=f"status {error.status_code}",
                    ),
                    cooldown,
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
        if slug is not None:
            for account in self._accounts:
                if account.slot.slug == slug:
                    return await account.backend.get_subscription_status(request_id)
            raise BackendError(404, f"Unknown account `{slug}`.")
        return await self.primary().backend.get_subscription_status(request_id)

    async def subscription_statuses(self, request_id: str) -> list[dict[str, Any]]:
        """Per-account usage; errors are reported per account, not fatal.

        Doubles as a proactive limit check: an account whose usage already
        reports a reached limit is benched here, so it is skipped without
        first wasting a request that would just 429."""
        self.refresh_if_changed()
        results: list[dict[str, Any]] = []
        for account in self._accounts:
            entry: dict[str, Any] = {
                "slug": account.slot.slug,
                "email": account.slot.email,
            }
            try:
                usage = await account.backend.get_subscription_status(request_id)
                entry["payload"] = usage
                self._bench_from_usage(account, usage)
            except (BackendError, AuthenticationError) as error:
                entry["error"] = str(error)
            results.append(entry)
        return results

    def _bench_from_usage(self, account: _PooledAccount, usage: dict[str, Any]) -> None:
        """Proactively cools down an account whose upstream usage already
        reports a reached limit, using the soonest window reset as the
        cooldown so the account returns to rotation exactly when it recovers."""
        if not isinstance(usage, dict):
            return
        reached = bool(usage.get("rate_limit_reached_type"))
        resets: list[int] = []
        rate = usage.get("rate_limit")
        windows = []
        if isinstance(rate, dict):
            windows = [rate.get("primary_window"), rate.get("secondary_window")]
        for window in windows:
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            if isinstance(used, (int, float)) and used >= 100:
                reached = True
                secs = window.get("reset_after_seconds") or window.get("resets_in_seconds")
                if isinstance(secs, (int, float)) and secs > 0:
                    resets.append(int(secs))
        now = time.monotonic()
        if not reached:
            # Authoritative recovery signal: usage says there is capacity, so
            # release any bench immediately rather than waiting out an
            # over-long estimate. This is what returns an account to rotation
            # the moment its budget is back.
            if account.limited_until > now:
                account.limited_until = 0.0
                account.last_error = None
            return
        cooldown = float(min(resets)) if resets else float(self._settings.openai_account_cooldown_seconds)
        # Only extend, never shorten, an existing bench.
        if account.limited_until < now + cooldown:
            account.limited_until = now + cooldown
            account.last_error = "usage limit reached (from usage report)"

    async def close(self) -> None:
        for account in self._accounts:
            await account.backend.close()

    def _final_error(self, error: Exception) -> Exception:
        if (
            isinstance(error, BackendError)
            and len(self._accounts) > 1
            and self._cooldown_seconds(error) is not None
        ):
            now = time.monotonic()
            waits = [max(0.0, account.limited_until - now) for account in self._accounts]
            earliest = min(waits) if waits else 0.0
            names = len(self._accounts)
            return BackendError(
                error.status_code,
                f"All {names} OpenAI accounts are unavailable "
                f"(earliest retry in {int(earliest) or '?'}s). Last error: {error.detail[:300]}",
            )
        return error


def build_pool(settings: Settings, traffic: TrafficLogger) -> OpenAiAccountPool:
    return OpenAiAccountPool(settings, traffic, slots=discover_slots(settings))
