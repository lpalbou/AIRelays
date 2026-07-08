from __future__ import annotations

import asyncio

import pytest

from airelay.providers import ProviderRegistry
from airelay.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class _FakeAuthManager:
    @staticmethod
    def load():
        return _FakeAuthRecord()

    @staticmethod
    def status() -> dict[str, object]:
        return {
            "ready_for_requests": True,
            "authenticated": True,
            "account_bound": True,
            "email": "user@example.com",
        }


class _FakeAuthRecord:
    authenticated = True
    account_id = "acct_123"
    bound_account_id = "acct_123"

    @staticmethod
    def account_matches_binding() -> bool:
        return True


class _FakeOpenAIBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self, request_id: str) -> dict[str, object]:
        del request_id
        self.calls += 1
        await asyncio.sleep(0.01)
        return {"models": [{"slug": "gpt-concurrent-cache"}]}


def test_provider_registry_marks_disabled_openai_runtime_not_ready(tmp_path) -> None:
    settings = make_settings(tmp_path, enable_openai_provider=False)
    registry = ProviderRegistry(settings, openai_auth=_FakeAuthManager())

    statuses = registry.provider_statuses()

    assert statuses["openai"]["enabled"] is False
    assert statuses["openai"]["ready_for_requests"] is False


@pytest.mark.asyncio
async def test_provider_registry_collapses_concurrent_openai_model_cache_misses(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        models_cache_ttl_seconds=300.0,
    )
    backend = _FakeOpenAIBackend()
    registry = ProviderRegistry(
        settings,
        openai_auth=_FakeAuthManager(),  # type: ignore[arg-type]
        openai_backend=backend,  # type: ignore[arg-type]
    )

    responses = await asyncio.gather(
        *(registry.list_models(f"req_{index}") for index in range(10))
    )

    assert backend.calls == 1
    assert {response["data"][0]["id"] for response in responses} == {"gpt-concurrent-cache"}
