from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from airelay import __version__
from airelay.app import create_app
from airelay.auth import AuthenticationError
from airelay.backend import BackendError, ChatGptCodexBackend, SSEEvent
from airelay.config import Settings
from airelay.traffic import TrafficLogger, snapshot_body


class FakeBackend(ChatGptCodexBackend):
    async def stream_response_events(self, payload, request_id, session_id):  # type: ignore[override]
        del payload, request_id, session_id
        yield SSEEvent(
            event="response.created",
            data=json.dumps(
                {
                    "response": {
                        "id": "resp_123",
                        "object": "response",
                        "created_at": 1,
                        "model": "gpt-5.4-mini",
                        "output": [],
                    }
                }
            ),
        )
        yield SSEEvent(
            event="response.output_item.done",
            data=json.dumps(
                {
                    "output_index": 0,
                    "item": {
                        "id": "msg_123",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                }
            ),
        )
        yield SSEEvent(
            event="response.completed",
            data=json.dumps(
                {
                    "response": {
                        "id": "resp_123",
                        "object": "response",
                        "created_at": 1,
                        "model": "gpt-5.4-mini",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }
                }
            ),
        )


class FakeAuthManager:
    async def ensure_fresh_tokens(self):  # type: ignore[override]
        return SimpleNamespace(access_token="chatgpt-token", account_id="account-123")

    async def refresh_tokens(self):  # type: ignore[override]
        return await self.ensure_fresh_tokens()


def make_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        require_bearer_auth=False,
        enable_claude=False,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def write_openai_auth(settings: Settings, account_id: str = "acct_123") -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": f"access-{account_id}",
                    "refresh_token": f"refresh-{account_id}",
                    "account_id": account_id,
                },
                "bound_account_id": account_id,
                "last_refresh": "2099-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_collect_response_rebuilds_output_from_stream(tmp_path) -> None:
    backend = FakeBackend(
        settings=make_settings(tmp_path),
        auth_manager=None,  # type: ignore[arg-type]
        traffic=TrafficLogger(tmp_path / "logs"),
        client=httpx.AsyncClient(),
    )
    try:
        response = await backend.collect_response({}, "req_123", None)
    finally:
        await backend.close()

    assert response["id"] == "resp_123"
    assert response["usage"]["total_tokens"] == 2
    assert response["output"] == [
        {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "ok"}],
        }
    ]


@pytest.mark.asyncio
async def test_get_subscription_status_uses_wham_usage_path(tmp_path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["account_id"] = request.headers.get("chatgpt-account-id")
        return httpx.Response(
            200,
            json={
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {
                        "used_percent": 14,
                        "limit_window_seconds": 18000,
                        "reset_after_seconds": 8557,
                        "reset_at": 1781321703,
                    },
                    "secondary_window": {
                        "used_percent": 39,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 442067,
                        "reset_at": 1781755213,
                    },
                },
            },
        )

    backend = ChatGptCodexBackend(
        settings=make_settings(
            tmp_path,
            upstream_base_url="https://chatgpt.com/backend-api/codex",
        ),
        auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
        traffic=TrafficLogger(tmp_path / "logs"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        response = await backend.get_subscription_status("req_123")
    finally:
        await backend.close()

    assert response["plan_type"] == "pro"
    assert captured["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert captured["authorization"] == "Bearer chatgpt-token"
    assert captured["account_id"] == "account-123"


def test_responses_route_returns_400_for_invalid_json(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            content="{not-json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Request body must be valid JSON."


def test_responses_route_reports_unknown_local_file_as_422(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "file_id": "file_missing"}],
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert "Unknown local file id `file_missing`" in response.json()["detail"]


def test_responses_route_rejects_max_output_tokens_locally(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "input": "hello",
                "stream": False,
                "max_output_tokens": 20,
            },
        )

    assert response.status_code == 422
    assert "max_output_tokens" in response.json()["detail"]


def test_chat_completions_route_rejects_max_completion_tokens_locally(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "max_completion_tokens": 20,
            },
        )

    assert response.status_code == 422
    assert "max_completion_tokens" in response.json()["detail"]


def test_completions_route_rejects_max_tokens_locally(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/completions",
            json={
                "model": "gpt-5.4",
                "prompt": "hello",
                "stream": False,
                "max_tokens": 20,
            },
        )

    assert response.status_code == 422
    assert "max_tokens" in response.json()["detail"]


def test_responses_route_rewrites_local_pdf_file_id_as_input_file(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_collect_response(payload, request_id, session_id):
        del request_id, session_id
        captured["payload"] = payload
        return {
            "id": "resp_123",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    with TestClient(app) as client:
        record = client.app.state.store.create_file(
            filename="sample.pdf",
            purpose="user_data",
            content_type="application/pdf",
            data=b"%PDF-1.4\nsample\n",
            sha256="abc123",
        )
        client.app.state.backend.collect_response = fake_collect_response
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "stream": False,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": record["id"]}],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert captured["payload"]["input"][0]["content"] == [  # type: ignore[index]
        {
            "type": "input_file",
            "filename": "sample.pdf",
            "file_data": "data:application/pdf;base64,JVBERi0xLjQKc2FtcGxlCg==",
        }
    ]


def test_snapshot_body_redacts_inline_file_data() -> None:
    snapshot = snapshot_body(
        "application/json",
        b'{"input":[{"type":"input_file","file_data":"data:application/pdf;base64,JVBERi0xLjQKc2FtcGxlCg=="}]}',
    )

    assert snapshot["kind"] == "json"
    assert snapshot["json"]["input"][0]["file_data"] == "[REDACTED]"


def test_responses_route_ignores_unsupported_sampling_parameters_and_sets_header(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_collect_response(payload, request_id, session_id):
        del request_id, session_id
        captured["payload"] = payload
        return {
            "id": "resp_123",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    with TestClient(app) as client:
        client.app.state.backend.collect_response = fake_collect_response
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4",
                "input": "hello",
                "stream": False,
                "temperature": 0.7,
                "top_p": 0.9,
                "presence_penalty": 0.1,
                "frequency_penalty": 0.2,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == (
        "temperature,top_p,presence_penalty,frequency_penalty"
    )
    assert "temperature" not in captured["payload"]
    assert "top_p" not in captured["payload"]
    assert "presence_penalty" not in captured["payload"]
    assert "frequency_penalty" not in captured["payload"]


def test_no_tools_responses_route_rejects_tool_requests(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/no-tools/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": "hello",
                "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
            },
        )

    assert response.status_code == 422
    assert "disables tools" in response.json()["detail"]


def test_subscription_status_route_returns_normalized_windows_and_raw_alias(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_get_subscription_status(request_id):
        del request_id
        return {
            "user_id": "user_123",
            "account_id": "acct_123",
            "email": "user@example.com",
            "plan_type": "pro",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 14,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 8557,
                    "reset_at": 1781321703,
                },
                "secondary_window": {
                    "used_percent": 39,
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 442067,
                    "reset_at": 1781755213,
                },
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": {
                            "used_percent": 0,
                            "limit_window_seconds": 18000,
                            "reset_after_seconds": 18000,
                            "reset_at": 1781331147,
                        },
                        "secondary_window": {
                            "used_percent": 0,
                            "limit_window_seconds": 604800,
                            "reset_after_seconds": 604800,
                            "reset_at": 1781917947,
                        },
                    },
                }
            ],
            "credits": {
                "has_credits": False,
                "unlimited": False,
                "overage_limit_reached": False,
                "balance": "0",
                "approx_local_messages": [0, 0],
                "approx_cloud_messages": [0, 0],
            },
            "spend_control": {"reached": False, "individual_limit": None},
            "rate_limit_reset_credits": {"available_count": 1},
        }

    with TestClient(app) as client:
        client.app.state.backend.get_subscription_status = fake_get_subscription_status
        response = client.get("/v1/account/rate_limits?raw=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "subscription_status"
    assert payload["account"]["plan_type"] == "pro"
    assert payload["rate_limits"]["default"]["primary_window"]["window_label"] == "5h"
    assert payload["rate_limits"]["default"]["secondary_window"]["window_label"] == "weekly"
    assert payload["rate_limits"]["additional"][0]["rate_limit"]["primary_window"]["window_label"] == "5h"
    assert payload["raw"]["plan_type"] == "pro"


def test_chat_stream_ignores_non_json_events(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield SSEEvent(
            "response.created",
            json.dumps({"response": {"id": "resp_1", "created_at": 1, "model": "gpt-5.4-mini"}}),
        )
        yield SSEEvent("message", "[DONE]")
        yield SSEEvent(
            "response.completed",
            json.dumps(
                {
                    "response": {
                        "id": "resp_1",
                        "created_at": 1,
                        "model": "gpt-5.4-mini",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }
                }
            ),
        )

    with TestClient(app) as client:
        client.app.state.backend.stream_response_events = fake_stream_response_events
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert "data: [DONE]" in response.text


def test_chat_route_ignores_unsupported_sampling_parameters_and_sets_header(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_collect_response(payload, request_id, session_id):
        del request_id, session_id
        captured["payload"] = payload
        return {
            "id": "resp_123",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    with TestClient(app) as client:
        client.app.state.backend.collect_response = fake_collect_response
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "stream": False,
                "temperature": 0.7,
                "top_p": 0.9,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "temperature,top_p"
    assert "temperature" not in captured["payload"]
    assert "top_p" not in captured["payload"]


def test_chat_route_flattens_tools_before_upstream_request(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_collect_response(payload, request_id, session_id):
        del request_id, session_id
        captured["payload"] = payload
        return {
            "id": "resp_123",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    with TestClient(app) as client:
        client.app.state.backend.collect_response = fake_collect_response
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "description": "Search the web.",
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "web_search"}},
            },
        )

    assert response.status_code == 200
    assert captured["payload"]["tools"] == [  # type: ignore[index]
        {
            "type": "function",
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }
    ]
    assert captured["payload"]["tool_choice"] == {  # type: ignore[index]
        "type": "function",
        "name": "web_search",
    }


def test_startup_generates_bearer_token_when_explicitly_enabled(tmp_path) -> None:
    token_file = tmp_path / "data" / "relay-token"
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=True,
        bearer_token_file=token_file,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert token_file.exists()


def test_healthz_is_minimal_and_public(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=False,
        bearer_token="secret-token",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "app_name": "AIRelays", "version": __version__}


def test_relay_status_returns_protected_diagnostics(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=False,
        bearer_token="secret-token",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/v1/relay/status",
            headers={"authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "relay.status"
    assert payload["ready"]["relay_token"] is True
    assert payload["security"]["client"]["ip"] == "testclient"
    assert payload["storage"]["file_count"] == 0


def test_relay_status_is_open_when_bearer_auth_is_disabled(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/v1/relay/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["security"]["require_bearer_auth"] is False
    assert payload["ready"]["relay_token"] is True


def test_relay_status_reports_any_provider_ready_when_openai_is_ready(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        bearer_token="secret-token",
    )
    app = create_app(settings)

    def fake_provider_statuses() -> dict[str, object]:
        return {
            "openai": {
                "enabled": True,
                "ready_for_requests": True,
            },
        }

    with TestClient(app) as client:
        client.app.state.providers.provider_statuses = fake_provider_statuses
        response = client.get(
            "/v1/relay/status",
            headers={"authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"]["upstream_auth"] is True
    assert payload["ready"]["any_provider"] is True
    assert payload["ready"]["openai_upstream_auth"] is True
    assert payload["ready"]["providers"]["openai"] is True


def test_relay_status_reports_any_provider_ready_when_claude_is_ready(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        bearer_token="secret-token",
        enable_openai_provider=False,
        enable_claude=True,
    )
    app = create_app(settings)

    def fake_provider_statuses() -> dict[str, object]:
        return {
            "openai": {
                "enabled": False,
                "ready_for_requests": False,
            },
            "claude": {
                "enabled": True,
                "ready_for_requests": True,
            },
        }

    with TestClient(app) as client:
        client.app.state.providers.provider_statuses = fake_provider_statuses
        response = client.get(
            "/v1/relay/status",
            headers={"authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"]["upstream_auth"] is True
    assert payload["ready"]["any_provider"] is True
    assert payload["ready"]["openai_upstream_auth"] is False
    assert payload["ready"]["providers"]["claude"] is True


def test_models_route_without_upstream_login_returns_upstream_auth_error_not_local_auth(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/v1/models")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["type"] == "authentication_error"
    assert payload["error"]["code"] == "upstream_auth_missing"
    assert response.headers["x-airelays-upstream-auth"] == "missing"


def test_models_route_maps_upstream_401_to_upstream_auth_error(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
    )
    app = create_app(settings)

    async def fake_list_models(request_id: str):
        del request_id
        raise BackendError(401, '{"detail":"unauthorized"}')

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        response = client.get("/v1/models")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "upstream_auth_rejected"
    assert response.headers["x-airelays-upstream-auth"] == "rejected"


def test_models_route_caches_openai_models_within_ttl(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        auth_storage_mode="file",
        models_cache_ttl_seconds=300.0,
    )
    write_openai_auth(settings)
    app = create_app(settings)
    calls = 0

    async def fake_list_models(request_id: str):
        nonlocal calls
        del request_id
        calls += 1
        return {"models": [{"slug": f"gpt-cache-{calls}"}]}

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        first = client.get("/v1/models")
        second = client.get("/v1/models")
        status = client.get("/v1/relay/status")

    assert calls == 1
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"][0]["id"] == "gpt-cache-1"
    assert second.json()["data"][0]["id"] == "gpt-cache-1"
    cache = status.json()["providers"]["openai"]["models_cache"]
    assert cache["enabled"] is True
    assert cache["state"] == "fresh"
    assert cache["ttl_seconds"] == 300.0
    assert cache["cached_model_count"] == 1
    assert "models_cache" not in status.json()["auth"]


def test_models_route_refreshes_openai_models_after_ttl(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        auth_storage_mode="file",
        models_cache_ttl_seconds=300.0,
    )
    write_openai_auth(settings)
    app = create_app(settings)
    calls = 0

    async def fake_list_models(request_id: str):
        nonlocal calls
        del request_id
        calls += 1
        return {"models": [{"slug": f"gpt-cache-{calls}"}]}

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        first = client.get("/v1/models")
        client.app.state.providers._openai_models_cache_fetched_at = time.monotonic() - 301.0
        second = client.get("/v1/models")

    assert calls == 2
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"][0]["id"] == "gpt-cache-1"
    assert second.json()["data"][0]["id"] == "gpt-cache-2"


def test_models_route_does_not_cache_openai_model_errors(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        auth_storage_mode="file",
        models_cache_ttl_seconds=300.0,
    )
    write_openai_auth(settings)
    app = create_app(settings)
    calls = 0

    async def fake_list_models(request_id: str):
        nonlocal calls
        del request_id
        calls += 1
        if calls == 1:
            raise BackendError(502, "temporary upstream failure")
        return {"models": [{"slug": "gpt-cache-ok"}]}

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        first = client.get("/v1/models")
        second = client.get("/v1/models")

    assert calls == 2
    assert first.status_code == 502
    assert second.status_code == 200
    assert second.json()["data"][0]["id"] == "gpt-cache-ok"


def test_models_route_ignores_warm_cache_when_openai_auth_is_removed(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        auth_storage_mode="file",
        models_cache_ttl_seconds=300.0,
    )
    write_openai_auth(settings)
    app = create_app(settings)
    calls = 0

    async def fake_list_models(request_id: str):
        nonlocal calls
        del request_id
        calls += 1
        if calls == 1:
            return {"models": [{"slug": "gpt-cache-warm"}]}
        raise AuthenticationError(
            "No ChatGPT login found. Run `airelays login` first.",
            code="upstream_auth_missing",
        )

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        first = client.get("/v1/models")
        (settings.data_dir / "auth.json").unlink()
        second = client.get("/v1/models")
        status = client.get("/v1/relay/status")

    assert calls == 2
    assert first.status_code == 200
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "upstream_auth_missing"
    cache = status.json()["providers"]["openai"]["models_cache"]
    assert cache["state"] == "empty"
    assert cache["cached_model_count"] == 0


def test_models_route_ignores_warm_cache_when_openai_account_changes(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        auth_storage_mode="file",
        models_cache_ttl_seconds=300.0,
    )
    write_openai_auth(settings, account_id="acct_1")
    app = create_app(settings)
    calls = 0

    async def fake_list_models(request_id: str):
        nonlocal calls
        del request_id
        calls += 1
        return {"models": [{"slug": f"gpt-cache-account-{calls}"}]}

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        first = client.get("/v1/models")
        write_openai_auth(settings, account_id="acct_2")
        second = client.get("/v1/models")

    assert calls == 2
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"][0]["id"] == "gpt-cache-account-1"
    assert second.json()["data"][0]["id"] == "gpt-cache-account-2"


def test_models_route_ttl_zero_disables_openai_models_cache(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        auth_storage_mode="file",
        models_cache_ttl_seconds=0.0,
    )
    write_openai_auth(settings)
    app = create_app(settings)
    calls = 0

    async def fake_list_models(request_id: str):
        nonlocal calls
        del request_id
        calls += 1
        return {"models": [{"slug": f"gpt-cache-disabled-{calls}"}]}

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        first = client.get("/v1/models")
        second = client.get("/v1/models")
        status = client.get("/v1/relay/status")

    assert calls == 2
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"][0]["id"] == "gpt-cache-disabled-1"
    assert second.json()["data"][0]["id"] == "gpt-cache-disabled-2"
    cache = status.json()["providers"]["openai"]["models_cache"]
    assert cache["configured"] is False
    assert cache["enabled"] is False
    assert cache["state"] == "disabled"


def test_relay_status_reports_models_cache_disabled_with_openai_provider_disabled(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        enable_openai_provider=False,
        models_cache_ttl_seconds=300.0,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/v1/relay/status")

    assert response.status_code == 200
    cache = response.json()["providers"]["openai"]["models_cache"]
    assert cache["configured"] is True
    assert cache["enabled"] is False
    assert cache["state"] == "provider_disabled"


def test_models_route_returns_claude_models_when_openai_auth_is_missing(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/v1/models",
            headers={"authorization": "Bearer relay-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert any(item["id"] == "claude:sonnet" for item in payload["data"])
    claude_model = next(item for item in payload["data"] if item["id"] == "claude:sonnet")
    assert claude_model["airelays"]["provider"] == "claude"
    assert "experimental" not in claude_model["airelays"]


def test_responses_route_rejects_claude_models_locally(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "input": "hello",
                "stream": False,
            },
        )

    assert response.status_code == 422
    assert "/v1/chat/completions" in response.json()["detail"]


def test_chat_completions_route_dispatches_claude_model(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)

    async def fake_create_chat_completion(body, request_id):
        assert body["model"] == "claude:sonnet"
        assert request_id.startswith("req_")
        return {
            "id": "chatcmpl_claude",
            "object": "chat.completion",
            "created": 1,
            "model": "claude:sonnet",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Claude OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    with TestClient(app) as client:
        client.app.state.providers.claude.create_chat_completion = fake_create_chat_completion
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "claude:sonnet"
    assert payload["choices"][0]["message"]["content"] == "Claude OK"


def test_chat_completions_route_streams_claude_model(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)

    async def fake_stream_chat_completion(body, request_id):
        del body, request_id
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"

    with TestClient(app) as client:
        client.app.state.providers.claude.stream_chat_completion = fake_stream_chat_completion
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.text == "data: first\n\ndata: [DONE]\n\n"


def test_completions_route_dispatches_claude_model(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)

    async def fake_create_completion(body, request_id):
        assert body["model"] == "claude:sonnet"
        assert request_id.startswith("req_")
        return {
            "id": "cmpl_claude",
            "object": "text_completion",
            "created": 1,
            "model": "claude:sonnet",
            "choices": [
                {
                    "text": "Claude OK",
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    with TestClient(app) as client:
        client.app.state.providers.claude.create_completion = fake_create_completion
        response = client.post(
            "/v1/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "prompt": "hello",
                "stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "claude:sonnet"
    assert payload["choices"][0]["text"] == "Claude OK"


def test_chat_completions_route_strips_sampling_parameters_for_claude(tmp_path) -> None:
    """Regression: standard OpenAI SDK clients send sampling parameters by
    default; the Claude runtime used to 422 on them. They must now get the
    same strip-and-disclose treatment as on the OpenAI runtime."""
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_create_chat_completion(body, request_id):
        del request_id
        captured["body"] = body
        return {
            "id": "chatcmpl_claude",
            "object": "chat.completion",
            "created": 1,
            "model": "claude:sonnet",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Claude OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    with TestClient(app) as client:
        client.app.state.providers.claude.create_chat_completion = fake_create_chat_completion
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "temperature": 0.5,
                "top_p": 0.9,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "temperature,top_p"
    assert "temperature" not in captured["body"]
    assert "top_p" not in captured["body"]
    assert response.json()["choices"][0]["message"]["content"] == "Claude OK"


def test_chat_completions_route_strips_sampling_parameters_for_claude_stream(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_stream_chat_completion(body, request_id):
        del request_id
        captured["body"] = body
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"

    with TestClient(app) as client:
        client.app.state.providers.claude.stream_chat_completion = fake_stream_chat_completion
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "temperature": 0.5,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "temperature"
    assert "temperature" not in captured["body"]


def test_completions_route_strips_sampling_parameters_for_claude(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)
    captured: dict[str, object] = {}

    async def fake_create_completion(body, request_id):
        del request_id
        captured["body"] = body
        return {
            "id": "cmpl_claude",
            "object": "text_completion",
            "created": 1,
            "model": "claude:sonnet",
            "choices": [
                {
                    "text": "Claude OK",
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

    with TestClient(app) as client:
        client.app.state.providers.claude.create_completion = fake_create_completion
        response = client.post(
            "/v1/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "prompt": "hello",
                "stream": False,
                "temperature": 0.2,
                "frequency_penalty": 0.1,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "temperature,frequency_penalty"
    assert "temperature" not in captured["body"]
    assert "frequency_penalty" not in captured["body"]
    assert response.json()["choices"][0]["text"] == "Claude OK"


def test_protected_route_rejects_missing_bearer_token(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=False,
        bearer_token="secret-token",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/v1/models")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_protected_route_accepts_valid_bearer_token_for_local_route(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=False,
        bearer_token="secret-token",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/conversations",
            json={"metadata": {"name": "demo"}},
            headers={"authorization": "Bearer secret-token"},
        )

    assert response.status_code == 200
    assert response.json()["object"] == "conversation"


def test_openai_only_local_routes_reject_when_openai_runtime_is_disabled(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=False,
        enable_openai_provider=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        files_response = client.get("/v1/files")
        conversations_response = client.post("/v1/conversations", json={"metadata": {"name": "demo"}})

    assert files_response.status_code == 501
    assert "OpenAI runtime is enabled" in files_response.json()["detail"]
    assert conversations_response.status_code == 501
    assert "OpenAI runtime is enabled" in conversations_response.json()["detail"]


def test_wrong_token_attempts_trigger_temporary_ip_block(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=False,
        bearer_token="secret-token",
        failed_auth_max_attempts=2,
        failed_auth_block_seconds=60,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        first = client.get("/v1/models", headers={"authorization": "Bearer wrong"})
        second = client.get("/v1/models", headers={"authorization": "Bearer wrong"})
        third = client.get("/v1/models", headers={"authorization": "Bearer wrong"})

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    assert third.headers["retry-after"] == "60"


def test_auth_failures_are_logged_with_redacted_authorization(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        auto_generate_bearer_token=False,
        bearer_token="secret-token",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        client.get("/v1/models", headers={"authorization": "Bearer wrong-token"})

    log_files = sorted((tmp_path / "logs").rglob("*.log"))
    assert log_files
    content = log_files[-1].read_text(encoding="utf-8")
    assert '"phase":"endpoint_auth_failed"' in content
    assert "[REDACTED]" in content


def test_upload_rejects_file_larger_than_configured_limit(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        max_upload_bytes=4,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/v1/files",
            files={"file": ("oversized.txt", b"12345", "text/plain")},
        )

    assert response.status_code == 413
    assert "upload limit" in response.json()["detail"]


def test_upload_quota_rejects_when_total_storage_limit_would_be_exceeded(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        max_upload_bytes=10,
        max_total_upload_bytes=6,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        first = client.post(
            "/v1/files",
            files={"file": ("first.txt", b"1234", "text/plain")},
        )
        second = client.post(
            "/v1/files",
            files={"file": ("second.txt", b"123", "text/plain")},
        )

    assert first.status_code == 200
    assert second.status_code == 413
    assert "upload quota" in second.json()["detail"]
