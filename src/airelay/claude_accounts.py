"""Selection and failover across the Claude accounts AIRelays owns (ADR 0007)."""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from airelay.claude_auth import account_identity, discover_profiles, suspend_profile
from airelay.claude_launch import sweep_stale_workdirs
from airelay.config import Settings
from airelay.providers import CLAUDE_TOKEN_USAGE_UNSUPPORTED, ClaudeCliRuntime, ProviderError, ResolvedModel
from airelay.traffic import TrafficLogger


NO_ACCOUNT = (
    "No Claude account is connected to AIRelays. Run `airelays claude login` "
    "or `airelays claude set-token`."
)


@dataclass
class _Account:
    runtime: ClaudeCliRuntime
    last_selected: int = 0
    in_flight: int = 0
    blocked_until: float = 0.0  # wall clock: sleep must count towards recovery
    blocked_reason: str | None = None
    blocked_at: float = 0.0
    # Models this account could not serve although its sign-in works
    # (model -> paused until); other models are unaffected.
    model_blocked: dict[str, float] = field(default_factory=dict)
    sign_in_check: asyncio.Task | None = None
    usage: dict[str, Any] | None = None
    usage_at: float = 0.0


def _windows(limit: Any) -> list[dict[str, Any]]:
    if not isinstance(limit, dict):
        return []
    return [limit[key] for key in ("primary_window", "secondary_window") if isinstance(limit.get(key), dict)]


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _scope_matches(extra: dict[str, Any], model: str) -> bool:
    """Only apply model-scoped caps when the upstream actually names a model.

    Do not infer that an opaque feature/speed scope limits all model traffic.
    Legacy payloads name model families in limit_name.
    """
    if extra.get("is_active") is False:
        return False
    scope = extra.get("scope")
    if isinstance(scope, dict) and scope:
        value = scope.get("model") or scope.get("model_family")
        if isinstance(value, dict):
            value = value.get("id") or value.get("display_name")
        # A cap scoped to additional dimensions does not necessarily apply
        # to this request (e.g. fast-mode-only allowances).
        if set(scope) - {"model", "model_family"}:
            return False
    else:
        value = extra.get("limit_name")
    if not isinstance(value, str):
        return False
    value = value.lower().removeprefix("claude:").removeprefix("claude-")
    model = model.lower().removeprefix("claude:").removeprefix("claude-")
    return model == value or model.startswith(value + "-") or model.startswith(value + "[")


def _label(account: _Account) -> str:
    """How messages name an account: its sign-in email, else its id."""
    return (account.runtime._last_probe or {}).get("email") or account.runtime.profile.slug


def _not_ready_message(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return NO_ACCOUNT
    renew = [row.get("email") or row["slug"] for row in rows if row.get("reauth_required")]
    if renew:
        return "No Claude account is ready. " + " ".join(
            f"{name} must sign in again: run `airelays claude login --replace {name}`." for name in renew)
    return "No Claude account is ready. Run `airelays claude login`."


class ClaudeAccountPool:
    def __init__(self, settings: Settings, traffic: TrafficLogger | None = None) -> None:
        self._settings = settings
        self._traffic = traffic
        self._accounts: dict[str, _Account] = {}
        self._sequence = 0
        sweep_stale_workdirs()
        self.refresh_if_changed()

    def refresh_if_changed(self) -> None:
        accounts: dict[str, _Account] = {}
        for profile in discover_profiles(self._settings):
            current = self._accounts.get(profile.slug)
            if current is None or current.runtime.profile != profile:
                current = _Account(ClaudeCliRuntime(self._settings, self._traffic, profile=profile))
                current.usage = current.runtime._usage_last_good
                current.usage_at = current.runtime._usage_last_good_epoch
            accounts[profile.slug] = current
        self._accounts = accounts

    def _statuses(self) -> list[dict[str, Any]]:
        self.refresh_if_changed()
        rows = []
        seen: dict[str, str] = {}
        snapshots = [(slug, account, account.runtime.status()) for slug, account in list(self._accounts.items())]
        # Ready rows first, browser profiles before the setup token: one
        # subscription never gets two shares of traffic, and a signed-out
        # profile is a profile to renew, not a duplicate.
        ordered = sorted(snapshots, key=lambda item: (not item[2].get("ready_for_requests"),
                                                      not item[1].runtime.profile.managed))
        for slug, account, status in ordered:
            status["slug"] = slug
            status["managed_profile"] = account.runtime.profile.managed
            identity = account_identity(status)
            if identity and status.get("ready_for_requests"):
                if identity in seen:
                    status["ready_for_requests"] = False
                    status["duplicate_of"] = seen[identity]
                else:
                    seen[identity] = slug
            remaining = max(0, math.ceil(account.blocked_until - time.time()))
            status["cooldown_seconds"] = remaining
            status["cooldown_reason"] = account.blocked_reason if remaining else None
            # Models this account cannot serve right now (its sign-in works).
            status["unavailable_models"] = {
                model: math.ceil(until - time.time())
                for model, until in account.model_blocked.items() if until > time.time()
            }
            status["in_flight"] = account.in_flight
            rows.append(status)
        return rows

    async def warm_status_probes(self) -> None:
        """A runtime's first status probe spawns the CLI and waits for it;
        run those off the event loop so status routes never block."""
        self.refresh_if_changed()
        if any(account.runtime.needs_first_probe for account in self._accounts.values()):
            await asyncio.to_thread(self._statuses)

    def status(self) -> dict[str, Any]:
        rows = self._statuses()
        primary = next((row for row in rows if row.get("ready_for_requests")), rows[0] if rows else None)
        if primary is None:
            primary = {
                "enabled": True, "local_only": True, "stateless_only": True,
                "requires_relay_bearer_auth": self._settings.require_bearer_auth,
                "logged_in": False, "notes": [NO_ACCOUNT],
            }
        return {
            **primary,
            "accounts": rows,
            "balance": self._settings.claude_balance,
            "ready_for_requests": any(row.get("ready_for_requests") for row in rows),
            "models": [item["id"] for item in self.list_models()],
        }

    def list_models(self) -> list[dict[str, Any]]:
        self.refresh_if_changed()
        records: dict[str, dict[str, Any]] = {}
        for account in self._accounts.values():
            for model in account.runtime.list_models():
                records.setdefault(model["id"], model)
        return list(records.values())

    def resolve_model(self, model_id: str) -> ResolvedModel | None:
        self.refresh_if_changed()
        for account in self._accounts.values():
            model = account.runtime.resolve_model(model_id)
            if model is not None:
                return model
        if not self._accounts and model_id.startswith(("claude:", "claude-")):
            raise ProviderError(503, NO_ACCOUNT, code="provider_auth_error")
        return None

    async def refresh_models(self, *, force: bool = False) -> None:
        self.refresh_if_changed()
        await asyncio.gather(*(account.runtime.refresh_models(force=force) for account in self._accounts.values()))

    def _validate(self, body: dict[str, Any], chat: bool) -> None:
        self.refresh_if_changed()
        for account in self._accounts.values():
            if account.runtime.resolve_model(body.get("model", "")):
                if chat:
                    account.runtime._prepare_chat_request(body)
                else:
                    account.runtime._prepare_completion_request(body)
                return
        if not self._accounts:
            raise ProviderError(503, NO_ACCOUNT, code="provider_auth_error")
        raise ProviderError(404, "Unknown Claude model; refresh `/v1/models`.", code="model_not_found")

    def _quota(self, account: _Account, model: str) -> tuple[float, float | None]:
        """Return quota-block deadline and remaining-budget ranking signal.

        Expired/stale meters never invent available capacity. The runtime
        independently preserves usage-endpoint backoff across restarts.
        """
        usage = account.usage
        now = time.time()
        if not usage:
            return 0, None
        stale = usage.get("stale") or now - account.usage_at > 900
        limits = usage.get("rate_limits") or {}
        windows = _windows(limits.get("default"))
        resolved = account.runtime._models.get(model)
        model_id = (resolved.resolved_model or resolved.upstream_id) if resolved else model
        for extra in limits.get("additional") or []:
            if _scope_matches(extra, model_id):
                windows.extend(_windows(extra.get("rate_limit")))
        blocked = 0.0
        fresh = []
        for window in windows:
            reset = _number(window.get("reset_at"))
            used = _number(window.get("used_percent"))
            if reset is not None and reset <= now:
                continue
            if used is not None:
                fresh.append(window)
                if used >= 100:
                    # If upstream omitted reset time, re-evaluate after a
                    # bounded cooldown from the observation, not from now.
                    blocked = max(blocked, reset or account.usage_at + self._settings.claude_account_cooldown_seconds)
        # Positive capacity ages out, but a known exhausted window remains
        # exhausted until its reset (or a newer successful observation).
        if stale or not fresh:
            return blocked, None
        longest = max((_number(w.get("window_seconds")) or 0) for w in fresh)
        used = max(float(w["used_percent"]) for w in fresh if (_number(w.get("window_seconds")) or 0) == longest)
        return blocked, used

    async def _eligible(self, model: str) -> list[_Account]:
        rows = await asyncio.to_thread(self._statuses)
        accounts = [account for row in rows if row.get("ready_for_requests")
                    if (account := self._accounts.get(row["slug"])) is not None]
        if not accounts:
            raise ProviderError(503, _not_ready_message(rows), code="provider_auth_error")
        eligible = [a for a in accounts if a.runtime.resolve_model(model)]
        if not eligible:
            raise ProviderError(422, f"No signed-in Claude account advertises `{model}`.", code="unsupported_for_provider")
        now = time.time()
        healthy = [a for a in eligible if self._paused_until(a, model) <= now]
        if not healthy:
            deadline = min(self._paused_until(a, model) for a in eligible)
            wait = max(1, math.ceil(deadline - now))
            quota_only = all(self._quota(a, model)[0] > now or a.blocked_reason == "provider_quota_exhausted"
                             for a in eligible)
            reasons = "; ".join(self._pause_reason(a, model, now) for a in eligible)
            if all(a.model_blocked.get(model, 0.0) > now for a in eligible):
                raise ProviderError(404, f"`{model}` is not available on any connected Claude account ({reasons}). "
                                    "Use another model from `/v1/models`.", code="model_unavailable")
            raise ProviderError(
                429 if quota_only else 503,
                f"No Claude account can serve `{model}` right now ({reasons}). Retry in {wait}s.",
                code="provider_quota_exhausted" if quota_only else "provider_unavailable",
                retry_after_seconds=wait,
            )
        return healthy

    def _paused_until(self, account: _Account, model: str) -> float:
        return max(account.blocked_until, self._quota(account, model)[0], account.model_blocked.get(model, 0.0))

    def _pause_reason(self, account: _Account, model: str, now: float) -> str:
        label = _label(account)
        if self._quota(account, model)[0] > now or account.blocked_reason == "provider_quota_exhausted":
            return f"{label}: usage limit reached"
        if account.model_blocked.get(model, 0.0) > now:
            return f"{label}: `{model}` is not available on this account"
        return f"{label}: paused after a temporary failure"

    def _pick(self, accounts: list[_Account], model: str) -> _Account:
        strategy = self._settings.claude_balance
        if strategy == "ordered":
            return accounts[0]
        if strategy == "round_robin":
            return min(accounts, key=lambda a: a.last_selected)
        # Prefer an available execution slot before quota ranking. Otherwise
        # a cached low percentage can queue every concurrent request on A.
        def rank(account: _Account) -> tuple:
            used = self._quota(account, model)[1]
            return (
                account.in_flight >= self._settings.claude_max_concurrent_requests,
                used is None, int(used or 0), account.in_flight, account.last_selected,
            )
        return min(accounts, key=rank)

    def _log(self, phase: str, account: _Account, request_id: str, **fields: Any) -> None:
        if self._traffic is not None:
            self._traffic.write({
                "phase": phase, "provider": "claude", "request_id": request_id,
                "account_slug": account.runtime.profile.slug,
                "account_email": (account.runtime._last_probe or {}).get("email"), **fields,
            })

    async def _settle(self, account: _Account, model: str, error: ProviderError,
                      request_id: str) -> tuple[bool, ProviderError]:
        """What a failed attempt means, and whether another account may try.

        The request's own fault (4xx) never pauses an account. A login-like
        failure is checked against Anthropic before blaming the account (see
        `_verify_sign_in`). A model the API does not serve to this account
        pauses only that model there. Quota waits for its reset; temporary
        failures pause briefly."""
        status, label = error.status_code, _label(account)
        if status in (401, 403):
            verdict = await self._check_sign_in(account, request_id)
            if verdict is False:
                return True, ProviderError(
                    503, f"Claude account {label} must sign in again (the Claude CLI answered: {error.detail}). "
                    f"Run `airelays claude login --replace {label}`.", code="provider_auth_error",
                )
            if verdict is True and status == 403:
                self._block_model(account, model, request_id)
                return True, ProviderError(
                    404, f"`{model}` is not available on Claude account {label}, whose sign-in works "
                    f"(the Claude CLI answered: {error.detail}). Use another model from `/v1/models`.",
                    code="model_unavailable",
                )
            if verdict is True:
                return True, ProviderError(
                    503, f"Claude account {label} refused the request with a sign-in error although its sign-in "
                    f"works (the Claude CLI answered: {error.detail}). Retrying usually works; if `{model}` keeps "
                    "failing, use another model from `/v1/models`.", code="provider_unavailable",
                )
            return True, error  # the check could not tell; `_verify_sign_in` paused the account
        if error.code == "invalid_json_output":
            return False, error  # the model's answer, not the account
        if error.code == "model_unavailable":
            self._block_model(account, model, request_id)
            return True, error
        if status == 429 or status >= 500:
            delay = error.retry_after_seconds or (self._settings.claude_account_cooldown_seconds if status == 429 else 30)
            self._pause(account, delay, error.code, request_id)
            return True, error
        return False, error

    async def _check_sign_in(self, account: _Account, request_id: str) -> bool | None:
        if account.sign_in_check is None or account.sign_in_check.done():
            account.sign_in_check = asyncio.create_task(self._verify_sign_in(account, request_id))
        return await asyncio.shield(account.sign_in_check)

    async def _verify_sign_in(self, account: _Account, request_id: str) -> bool | None:
        """Ask Anthropic whether the account's sign-in works, and apply the
        answer here, so it holds even if the request that asked is
        cancelled: rejected -> needs a new sign-in (no timer); unknown ->
        pause; works -> nothing to do."""
        profile = account.runtime.profile
        verdict = await account.runtime.check_sign_in()
        if verdict is False:
            current = next((p for p in discover_profiles(self._settings) if p.slug == profile.slug), None)
            # A sign-in completed meanwhile (new revision) must not be undone.
            if current is None or current.identity.get("revision") != profile.identity.get("revision"):
                return True  # the fresh sign-in is the one requests will use
            suspend_profile(current)
            self._log("account_needs_sign_in", account, request_id)
        elif verdict is None:
            self._pause(account, self._settings.claude_account_cooldown_seconds, "provider_auth_error", request_id)
        return verdict

    def _pause(self, account: _Account, delay: float, reason: str | None, request_id: str) -> None:
        account.blocked_until = max(account.blocked_until, time.time() + delay)
        account.blocked_at = time.time()
        account.blocked_reason = reason
        self._log("account_failover", account, request_id, reason=reason, cooldown_seconds=delay)

    def _block_model(self, account: _Account, model: str, request_id: str) -> None:
        delay = self._settings.claude_account_cooldown_seconds
        account.model_blocked[model] = time.time() + delay
        self._log("model_unavailable", account, request_id, model=model, cooldown_seconds=delay)

    async def _remaining(self, model: str, attempted: set[str]) -> list[_Account]:
        """Accounts still worth trying; the failure just seen explains
        itself better than "nothing is left", so this never raises."""
        try:
            return [a for a in await self._eligible(model) if a.runtime.profile.slug not in attempted]
        except ProviderError:
            return []

    async def _collect(self, body: dict[str, Any], request_id: str, *, chat: bool) -> dict[str, Any]:
        self._validate(body, chat)
        available = await self._eligible(body["model"])
        attempted: set[str] = set()
        while available:
            account = self._pick(available, body["model"])
            available.remove(account)
            attempted.add(account.runtime.profile.slug)
            self._sequence += 1
            account.last_selected = self._sequence
            account.in_flight += 1
            self._log("account_selected", account, request_id)
            try:
                method = account.runtime.create_chat_completion if chat else account.runtime.create_completion
                return await method(body, request_id)
            except ProviderError as error:
                retry, error = await self._settle(account, body["model"], error, request_id)
                if not retry:
                    raise error
                # Other requests or a sign-out can change the remaining
                # pool while this subprocess runs. Never reuse that snapshot.
                available = await self._remaining(body["model"], attempted)
                if not available:
                    raise error
            finally:
                account.in_flight -= 1
        raise AssertionError("No account selected")

    async def create_chat_completion(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        return await self._collect(body, request_id, chat=True)

    async def create_completion(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        return await self._collect(body, request_id, chat=False)

    def stream_chat_completion(self, body: dict[str, Any], request_id: str) -> AsyncIterator[bytes]:
        self._validate(body, True)
        return self._stream(body, request_id, chat=True)

    def stream_completion(self, body: dict[str, Any], request_id: str) -> AsyncIterator[bytes]:
        self._validate(body, False)
        return self._stream(body, request_id, chat=False)

    async def _stream(self, body: dict[str, Any], request_id: str, *, chat: bool) -> AsyncIterator[bytes]:
        available = await self._eligible(body["model"])
        attempted: set[str] = set()
        while available:
            account = self._pick(available, body["model"])
            available.remove(account)
            attempted.add(account.runtime.profile.slug)
            self._sequence += 1
            account.last_selected = self._sequence
            account.in_flight += 1
            self._log("account_selected", account, request_id)
            sent = False
            try:
                method = account.runtime.stream_chat_completion if chat else account.runtime.stream_completion
                async with aclosing(method(body, request_id)) as stream:
                    async for chunk in stream:
                        sent = True
                        yield chunk
                return
            except ProviderError as error:
                retry, error = await self._settle(account, body["model"], error, request_id)
                if sent or not retry:
                    raise error
                available = await self._remaining(body["model"], attempted)
                if not available:
                    raise error
            finally:
                account.in_flight -= 1

    async def _usage(self, account: _Account, request_id: str) -> dict[str, Any]:
        payload = await account.runtime.get_subscription_status(request_id)
        if not payload.get("stale"):
            account.usage = payload
            # Cached reads must not make an old observation look fresh.
            account.usage_at = account.runtime._usage_last_good_epoch or time.time()
            if account.blocked_reason == "provider_quota_exhausted" and account.usage_at > account.blocked_at:
                windows = _windows((payload.get("rate_limits") or {}).get("default"))
                if windows and all(_number(w.get("used_percent")) is not None and w["used_percent"] < 100 for w in windows):
                    account.blocked_until = 0.0
                    account.blocked_reason = None
        return payload

    async def subscription_statuses(self, request_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._statuses)
        async def fetch(row: dict[str, Any]) -> dict[str, Any]:
            result = {"slug": row["slug"], "email": row.get("email")}
            if not row.get("managed_profile"):
                return {**result, "usage_unsupported": CLAUDE_TOKEN_USAGE_UNSUPPORTED}
            if not row.get("ready_for_requests"):
                return {**result, "error": "Account is not ready; sign in again with `airelays claude login --replace ACCOUNT`."}
            try:
                account = self._accounts.get(row["slug"])
                if account is None:
                    return {**result, "error": "Account was removed."}
                return {**result, "status": await self._usage(account, request_id)}
            except (ProviderError, OSError) as error:
                return {**result, "error": str(error)}
        return list(await asyncio.gather(*(fetch(row) for row in rows)))

    async def get_subscription_status(self, request_id: str, *, slug: str | None = None) -> dict[str, Any]:
        rows = await asyncio.to_thread(self._statuses)
        if slug is not None:
            matches = [row for row in rows if slug in (row["slug"], row.get("email"))]
            if len(matches) != 1:
                raise ProviderError(404, "Claude account is unknown or ambiguous; use an account id.")
            row = matches[0]
        else:
            ready = [row for row in rows if row.get("ready_for_requests")]
            row = next((row for row in ready if row.get("managed_profile")), ready[0] if ready else None)
        if row is None or not row.get("ready_for_requests"):
            raise ProviderError(503, _not_ready_message(rows), code="provider_unavailable")
        account = self._accounts.get(row["slug"])
        if account is None:
            raise ProviderError(404, "Claude account was removed.")
        return await self._usage(account, request_id)

    async def hard_refresh(self, request_id: str) -> list[dict[str, Any]]:
        # Respect each runtime's usage-endpoint cache/backoff. A button press
        # must not undo an upstream Retry-After or a generation cooldown.
        return await self.subscription_statuses(request_id)

    async def warm_start(self) -> None:
        # Every account, every start: reading usage is also what keeps an
        # idle profile's OAuth token rotated.
        await self.subscription_statuses("startup")

    async def usage_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await self.subscription_statuses("claude-usage-refresh")
            except (OSError, ProviderError):
                pass
