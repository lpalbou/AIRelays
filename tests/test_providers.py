from __future__ import annotations

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
    def status() -> dict[str, object]:
        return {
            "ready_for_requests": True,
            "authenticated": True,
            "account_bound": True,
            "email": "user@example.com",
        }


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
