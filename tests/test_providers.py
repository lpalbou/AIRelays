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


def test_subprocess_env_injects_stored_claude_token(tmp_path, monkeypatch) -> None:
    """A token stored via `airelays claude set-token` must reach every
    spawned claude child, and must beat any ambient environment value —
    explicit configuration over invisible shell state."""
    settings = make_settings(tmp_path)
    settings.claude_oauth_token_file = tmp_path / "data" / "claude-token"
    settings.write_claude_oauth_token("file-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")

    runtime = ClaudeCliRuntime(settings)
    env = runtime._subprocess_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "file-token"

    token_mode = settings.claude_oauth_token_file.stat().st_mode & 0o777
    assert token_mode == 0o600
    assert settings.claude_oauth_token_source() == "file"


def test_subprocess_env_falls_back_to_ambient_token(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings.claude_oauth_token_file = tmp_path / "data" / "claude-token"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")

    runtime = ClaudeCliRuntime(settings)
    env = runtime._subprocess_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient-token"
    assert settings.claude_oauth_token_source() == "env"


def test_claude_usage_normalizes_to_openai_shape(tmp_path) -> None:
    """The Claude usage payload must produce the exact window shape the
    OpenAI runtime produces, so one renderer covers both providers."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    runtime._last_probe = {"email": "me@example.com", "subscription_type": "pro", "version": "2.1.94"}

    normalized = runtime._normalize_usage(
        {
            "five_hour": {"utilization": 35.0, "resets_at": "2099-01-01T05:00:00+00:00"},
            "seven_day": {"utilization": 100.0, "resets_at": "2099-01-03T00:00:00+00:00"},
            "seven_day_sonnet": {"utilization": 12.0, "resets_at": "2099-01-03T00:00:00+00:00"},
            "seven_day_opus": None,
        }
    )

    assert normalized["account"] == {"email": "me@example.com", "plan_type": "pro"}
    primary = normalized["rate_limits"]["default"]["primary_window"]
    secondary = normalized["rate_limits"]["default"]["secondary_window"]
    assert primary["used_percent"] == 35.0
    assert primary["window_label"] == "5h"
    assert primary["reset_after_seconds"] > 0
    assert secondary["window_label"] == "weekly"
    # A 100% window marks the account as at its limit, like OpenAI.
    assert normalized["rate_limit_reached_type"] == "seven_day"
    additional = normalized["rate_limits"]["additional"]
    assert len(additional) == 1 and additional[0]["limit_name"] == "Sonnet"


def test_claude_usage_tolerates_missing_buckets(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    normalized = runtime._normalize_usage({})
    assert normalized["rate_limits"]["default"]["primary_window"] is None
    assert normalized["rate_limit_reached_type"] is None


def test_claude_usage_serves_stale_snapshot_during_rate_limit(tmp_path) -> None:
    """A 429 from the undocumented usage endpoint must not blank the UI:
    the last good snapshot is served, annotated as stale with the retry
    horizon, and no further upstream request is made inside the window."""
    import time as _time

    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    runtime._usage_last_good = {"account": {"email": "a@b.c"}, "rate_limits": {"default": None, "additional": []}}
    runtime._usage_last_good_epoch = _time.time() - 120
    runtime._usage_blocked_until = _time.monotonic() + 1800

    stale = runtime._stale_or_rate_limit_error(_time.monotonic())

    assert stale["stale"] is True
    assert 0 < stale["retry_after_seconds"] <= 1800
    assert stale["account"]["email"] == "a@b.c"
    # The stored snapshot itself must stay unannotated (deep-copied).
    assert "stale" not in runtime._usage_last_good


def test_claude_usage_raises_actionable_error_without_snapshot(tmp_path) -> None:
    import time as _time

    import pytest as _pytest

    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    runtime._usage_blocked_until = _time.monotonic() + 3600

    with _pytest.raises(ProviderError) as excinfo:
        runtime._stale_or_rate_limit_error(_time.monotonic())

    assert excinfo.value.status_code == 503
    assert "rate-limit" in str(excinfo.value).lower()
    assert excinfo.value.code == "provider_rate_limited"


def test_retry_after_seconds_clamps_and_defaults() -> None:
    from airelay.providers import _retry_after_seconds

    assert _retry_after_seconds(None) == 3600
    assert _retry_after_seconds("garbage") == 3600
    assert _retry_after_seconds("120") == 120
    assert _retry_after_seconds("5") == 60          # floor: no hammering
    assert _retry_after_seconds("999999") == 7200   # ceiling: no multi-day wedge


@pytest.mark.asyncio
async def test_claude_usage_429_blocks_upstream_until_window_passes(tmp_path, monkeypatch) -> None:
    """End-to-end orchestration: a 429 sets the block from retry-after, no
    request is sent inside the window, and the first call after the window
    succeeds and clears the block."""
    import time as _time

    import httpx as _httpx

    calls = {"count": 0}
    responses = [
        _httpx.Response(429, headers={"retry-after": "120"}, json={"error": "rate_limit"}),
        _httpx.Response(200, json={"five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00+00:00"}}),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            response = responses[min(calls["count"], len(responses) - 1)]
            calls["count"] += 1
            return response

    monkeypatch.setattr("airelay.providers.httpx.AsyncClient", FakeClient)
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    monkeypatch.setattr(runtime, "_resolve_usage_token", lambda: "tok")

    # 1) First call hits upstream, gets 429, no snapshot yet → clear error.
    with pytest.raises(ProviderError) as excinfo:
        await runtime.get_subscription_status("req-1")
    assert excinfo.value.code == "provider_rate_limited"
    assert calls["count"] == 1
    assert runtime._usage_blocked_until > _time.monotonic()

    # 2) Inside the window: no upstream request at all.
    with pytest.raises(ProviderError):
        await runtime.get_subscription_status("req-2")
    assert calls["count"] == 1

    # 3) Window passed: fetch succeeds, block clears, payload is fresh.
    runtime._usage_blocked_until = _time.monotonic() - 1
    payload = await runtime.get_subscription_status("req-3")
    assert calls["count"] == 2
    assert "stale" not in payload
    assert payload["rate_limits"]["default"]["primary_window"]["used_percent"] == 10.0
    assert runtime._usage_blocked_until == 0.0

    # 4) Fresh cache: still no extra upstream call.
    await runtime.get_subscription_status("req-4")
    assert calls["count"] == 2
