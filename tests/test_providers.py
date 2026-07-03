from __future__ import annotations

import asyncio

import pytest

from airelay.providers import ClaudeCliRuntime, ProviderError, ProviderRegistry
from airelay.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        enable_claude_experimental=True,
        claude_models=("claude:sonnet",),
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


@pytest.mark.asyncio
async def test_claude_runtime_creates_chat_completion_from_text_messages(tmp_path, monkeypatch) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        captured["request_id"] = request_id
        return {
            "result": "Claude says hi",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 4},
        }

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)

    payload = await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Say hi"},
            ],
        },
        "req_123",
    )

    request = captured["request"]
    assert request.public_model == "claude:sonnet"
    assert request.upstream_model == "sonnet"
    assert request.system_prompt == "Be terse."
    assert request.prompt == "Say hi"
    assert payload["choices"][0]["message"]["content"] == "Claude says hi"
    assert payload["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_claude_runtime_rejects_tools_on_chat_route(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    with pytest.raises(ProviderError, match="does not support `tools`"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "name": "lookup"}],
            },
            "req_123",
        )


@pytest.mark.asyncio
async def test_claude_runtime_rejects_temperature_on_completions_route(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    with pytest.raises(ProviderError, match="does not support `temperature`"):
        await runtime.create_completion(
            {
                "model": "claude:sonnet",
                "prompt": "hello",
                "temperature": 0.2,
            },
            "req_123",
        )


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
        enable_claude_experimental=False,
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
