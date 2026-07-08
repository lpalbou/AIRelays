from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from airelay.auth import AuthManager
from airelay.config import Settings
from airelay.traffic import TrafficLogger


class ProviderError(RuntimeError):
    def __init__(self, status_code: int, detail: str, *, code: str = "provider_error") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    provider: str
    public_id: str
    upstream_id: str


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    provider: str
    owned_by: str
    upstream_id: str
    experimental: bool
    routes: dict[str, bool]
    stateful_conversations: bool

    def as_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": self.owned_by,
            "airelays": {
                "provider": self.provider,
                "upstream_model": self.upstream_id,
                "experimental": self.experimental,
                "capabilities": {
                    "routes": self.routes,
                    "stateful_conversations": self.stateful_conversations,
                },
            },
        }


def _openai_model_record(model_id: str) -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider="openai",
        owned_by="airelays-openai-subscription",
        upstream_id=model_id,
        experimental=False,
        routes={
            "responses": True,
            "chat_completions": True,
            "completions": True,
            "files": True,
            "conversations": True,
            "subscription_status": True,
        },
        stateful_conversations=True,
    )


class ProviderRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        openai_auth: AuthManager,
        openai_backend: Any = None,
        traffic: TrafficLogger | None = None,
        account_pool: Any = None,
    ) -> None:
        self._settings = settings
        self._openai_auth = openai_auth
        self._openai_backend = openai_backend
        self._traffic = traffic
        # Optional multi-account pool; when present, provider status lists
        # every enrolled account.
        self._account_pool = account_pool
        self._openai_models_cache_payload: dict[str, Any] | None = None
        self._openai_models_cache_fetched_at: float | None = None
        self._openai_models_cache_key: tuple[str | None, ...] | None = None
        self._openai_models_cache_lock = asyncio.Lock()

    def resolve_model(self, model_id: str) -> ResolvedModel:
        if self._settings.enable_openai_provider:
            return ResolvedModel(provider="openai", public_id=model_id, upstream_id=model_id)
        raise ProviderError(
            422,
            f"Unknown model `{model_id}` and the OpenAI runtime is disabled.",
            code="unsupported_for_provider",
        )

    async def list_models(self, request_id: str) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        openai_error: Exception | None = None
        if self._settings.enable_openai_provider and self._openai_backend is not None:
            try:
                payload = await self._openai_models_payload(request_id)
                models = payload.get("models")
                if isinstance(models, list):
                    for item in models:
                        if not isinstance(item, dict):
                            continue
                        slug = item.get("slug")
                        if isinstance(slug, str) and slug:
                            data.append(_openai_model_record(slug).as_wire())
            except Exception as exc:  # noqa: BLE001
                openai_error = exc
        if data:
            return {"object": "list", "data": data}
        if openai_error is not None:
            raise openai_error
        return {"object": "list", "data": []}

    def _models_cache_ttl_seconds(self) -> float:
        return max(0.0, float(self._settings.models_cache_ttl_seconds))

    def _current_openai_models_cache_key(self) -> tuple[str | None, ...] | None:
        record = self._openai_auth.load()
        if record is None or not record.authenticated or not record.account_matches_binding():
            return None
        return (
            self._settings.upstream_base_url,
            self._settings.client_version,
            record.account_id,
            record.bound_account_id,
        )

    def _clear_openai_models_cache(self) -> None:
        self._openai_models_cache_payload = None
        self._openai_models_cache_fetched_at = None
        self._openai_models_cache_key = None

    def _cached_openai_models_payload(
        self, now: float, cache_key: tuple[str | None, ...] | None
    ) -> dict[str, Any] | None:
        ttl = self._models_cache_ttl_seconds()
        if ttl <= 0:
            return None
        if cache_key is None:
            self._clear_openai_models_cache()
            return None
        if self._openai_models_cache_payload is None or self._openai_models_cache_fetched_at is None:
            return None
        if self._openai_models_cache_key != cache_key:
            self._clear_openai_models_cache()
            return None
        if now - self._openai_models_cache_fetched_at >= ttl:
            return None
        return self._openai_models_cache_payload

    async def _openai_models_payload(self, request_id: str) -> dict[str, Any]:
        if self._openai_backend is None:
            return {"models": []}
        now = time.monotonic()
        cache_key = self._current_openai_models_cache_key()
        cached = self._cached_openai_models_payload(now, cache_key)
        if cached is not None:
            self._log_openai_models_cache(request_id, "hit", now)
            return cached

        ttl = self._models_cache_ttl_seconds()
        if ttl <= 0:
            self._log_openai_models_cache(request_id, "disabled", now)
            return await self._openai_backend.list_models(request_id)

        self._log_openai_models_cache(request_id, "miss", now)
        async with self._openai_models_cache_lock:
            now = time.monotonic()
            cache_key = self._current_openai_models_cache_key()
            cached = self._cached_openai_models_payload(now, cache_key)
            if cached is not None:
                self._log_openai_models_cache(request_id, "hit", now)
                return cached
            payload = await self._openai_backend.list_models(request_id)
            models = payload.get("models")
            cache_key = self._current_openai_models_cache_key()
            if isinstance(models, list) and cache_key is not None:
                self._openai_models_cache_payload = payload
                self._openai_models_cache_fetched_at = time.monotonic()
                self._openai_models_cache_key = cache_key
                self._log_openai_models_cache(
                    request_id, "refresh", self._openai_models_cache_fetched_at
                )
            return payload

    def openai_models_cache_status(self) -> dict[str, Any]:
        ttl = self._models_cache_ttl_seconds()
        configured = ttl > 0
        enabled = self._settings.enable_openai_provider and configured
        status: dict[str, Any] = {
            "configured": configured,
            "enabled": enabled,
            "ttl_seconds": ttl,
        }
        if not self._settings.enable_openai_provider:
            status.update(
                {
                    "state": "provider_disabled",
                    "age_seconds": None,
                    "expires_in_seconds": None,
                    "cached_model_count": 0,
                }
            )
            return status
        payload = self._openai_models_cache_payload
        fetched_at = self._openai_models_cache_fetched_at
        if payload is None or fetched_at is None:
            status.update(
                {
                    "state": "disabled" if not configured else "empty",
                    "age_seconds": None,
                    "expires_in_seconds": None,
                    "cached_model_count": 0,
                }
            )
            return status

        age = max(0.0, time.monotonic() - fetched_at)
        expires_in = max(0.0, ttl - age) if enabled else 0.0
        models = payload.get("models")
        status.update(
            {
                "state": "fresh" if enabled and age < ttl else "expired",
                "age_seconds": round(age, 3),
                "expires_in_seconds": round(expires_in, 3) if enabled else 0.0,
                "cached_model_count": len(models) if isinstance(models, list) else 0,
            }
        )
        return status

    def _log_openai_models_cache(self, request_id: str, cache_state: str, now: float) -> None:
        if self._traffic is None:
            return
        status = self.openai_models_cache_status()
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_models_cache",
                "provider": "openai",
                "cache": cache_state,
                "ttl_seconds": status["ttl_seconds"],
                "age_seconds": status["age_seconds"],
                "expires_in_seconds": status["expires_in_seconds"],
                "cached_model_count": status["cached_model_count"],
                "monotonic_time": round(now, 3),
            }
        )

    def provider_statuses(self) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        openai_status = {
            "enabled": self._settings.enable_openai_provider,
            "experimental": False,
            "models_cache": self.openai_models_cache_status(),
            **self._openai_auth.status(),
        }
        if not self._settings.enable_openai_provider:
            openai_status["ready_for_requests"] = False
        if self._account_pool is not None:
            self._account_pool.refresh_if_changed()
        if self._account_pool is not None and self._account_pool.size > 1:
            openai_status["accounts"] = self._account_pool.account_statuses()
            openai_status["balance"] = self._settings.openai_balance
            openai_status["ready_for_requests"] = openai_status["ready_for_requests"] or any(
                account.get("ready_for_requests") for account in openai_status["accounts"]
            )
        providers["openai"] = openai_status
        return providers
