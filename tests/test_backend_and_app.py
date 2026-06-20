from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from airelay import __version__
from airelay.app import create_app
from airelay.backend import BackendError, ChatGptCodexBackend, SSEEvent
from airelay.config import Settings
from airelay.traffic import TrafficLogger


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
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


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
