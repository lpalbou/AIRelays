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
        # Failure-path tests assert the terminal outcome; automatic retry
        # (with real backoff sleeps) is opted into by the retry tests.
        openai_retry_attempts=0,
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


def _created_event(response_id: str = "resp_fail") -> SSEEvent:
    return SSEEvent(
        "response.created",
        json.dumps(
            {
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": 1,
                    "model": "gpt-5.4-mini",
                    "status": "in_progress",
                    "output": [],
                }
            }
        ),
    )


def _failure_events(code: str = "server_is_overloaded") -> list[SSEEvent]:
    """The upstream failure grammar captured live on 2026-08-01: an `error`
    event with the object nested under "error", then `response.failed` with
    response.error carrying code/message only."""
    return [
        SSEEvent(
            "error",
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "service_unavailable_error",
                        "code": code,
                        "message": "Our servers are currently overloaded.",
                        "param": None,
                    },
                    "sequence_number": 2,
                }
            ),
        ),
        SSEEvent(
            "response.failed",
            json.dumps(
                {
                    "response": {
                        "id": "resp_fail",
                        "status": "failed",
                        "error": {
                            "code": code,
                            "message": "Our servers are currently overloaded.",
                        },
                    }
                }
            ),
        ),
    ]


def make_event_backend(tmp_path, events: list[SSEEvent]) -> ChatGptCodexBackend:
    class _EventBackend(ChatGptCodexBackend):
        async def stream_response_events(self, payload, request_id, session_id):  # type: ignore[override]
            del payload, request_id, session_id
            for event in events:
                yield event

    return _EventBackend(
        settings=make_settings(tmp_path),
        auth_manager=None,  # type: ignore[arg-type]
        traffic=TrafficLogger(tmp_path / "logs"),
        client=httpx.AsyncClient(),
    )


def collect_via_events(tmp_path, events: list[SSEEvent]):
    """A drop-in for app.state.backend.collect_response that runs the REAL
    collect_response logic over a canned upstream stream."""

    async def _collect(payload, request_id, session_id):
        backend = make_event_backend(tmp_path, events)
        try:
            return await backend.collect_response(payload, request_id, session_id)
        finally:
            await backend.close()

    return _collect


@pytest.mark.asyncio
async def test_collect_response_raises_on_upstream_failure_events(tmp_path) -> None:
    backend = make_event_backend(tmp_path, [_created_event(), *_failure_events()])
    try:
        with pytest.raises(BackendError) as excinfo:
            await backend.collect_response({}, "req_123", None)
    finally:
        await backend.close()

    assert excinfo.value.status_code == 502
    error = json.loads(excinfo.value.detail)["error"]
    assert error["code"] == "server_is_overloaded"
    assert "overloaded" in error["message"]


@pytest.mark.asyncio
async def test_collect_response_raises_when_stream_dies_before_completion(tmp_path) -> None:
    backend = make_event_backend(tmp_path, [_created_event()])
    try:
        with pytest.raises(BackendError) as excinfo:
            await backend.collect_response({}, "req_123", None)
    finally:
        await backend.close()

    assert excinfo.value.status_code == 502
    assert json.loads(excinfo.value.detail)["error"]["code"] == "incomplete_stream"


@pytest.mark.asyncio
async def test_collect_response_maps_usage_limit_failure_to_429(tmp_path) -> None:
    backend = make_event_backend(
        tmp_path, [_created_event(), *_failure_events(code="usage_limit_reached")]
    )
    try:
        with pytest.raises(BackendError) as excinfo:
            await backend.collect_response({}, "req_123", None)
    finally:
        await backend.close()

    assert excinfo.value.status_code == 429
    assert json.loads(excinfo.value.detail)["error"]["code"] == "usage_limit_reached"


def _oversized_input_failure_events() -> list[SSEEvent]:
    """The 2026-08-01 15:26Z incident grammar, verbatim shape
    (req_7d3b0b16f7ee43a5a6569c38b6d46133): a deterministic
    invalid_request_error `error` event, then `response.failed`. The
    incident's real code was scrubbed from the traffic log by the blanket
    "code" redaction (fixed alongside this test); the code here is a
    representative stand-in — classification keys on the type and param
    just as much, so the exact code string is not load-bearing."""
    message = (
        "Your input exceeds the context window of this model. "
        "Please adjust your input and try again."
    )
    return [
        SSEEvent(
            "error",
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                        "message": message,
                        "param": "input",
                    },
                    "sequence_number": 2,
                }
            ),
        ),
        SSEEvent(
            "response.failed",
            json.dumps(
                {
                    "response": {
                        "id": "resp_fail",
                        "status": "failed",
                        "error": {"code": "context_length_exceeded", "message": message},
                    }
                }
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_collect_response_maps_invalid_request_failure_to_400_passthrough(tmp_path) -> None:
    """Regression for the 2026-08-01 incident: a deterministic client
    rejection in the stream must surface as a 400 carrying the upstream
    error verbatim (type/code/message/param), never as a retriable 502 —
    the 502 class is what bought 8 paid upstream calls and a fabricated
    "all accounts are at their limits" answer."""
    backend = make_event_backend(
        tmp_path, [_created_event(), *_oversized_input_failure_events()]
    )
    try:
        with pytest.raises(BackendError) as excinfo:
            await backend.collect_response({}, "req_123", None)
    finally:
        await backend.close()

    assert excinfo.value.status_code == 400
    error = json.loads(excinfo.value.detail)["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"
    assert error["param"] == "input"
    assert "context window" in error["message"]


@pytest.mark.asyncio
async def test_collect_response_returns_incomplete_response_as_result(tmp_path) -> None:
    events = [
        _created_event("resp_partial"),
        SSEEvent(
            "response.output_item.done",
            json.dumps(
                {
                    "output_index": 0,
                    "item": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "incomplete",
                        "content": [{"type": "output_text", "text": "partial"}],
                    },
                }
            ),
        ),
        SSEEvent(
            "response.incomplete",
            json.dumps(
                {
                    "response": {
                        "id": "resp_partial",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {"input_tokens": 1, "output_tokens": 5, "total_tokens": 6},
                    }
                }
            ),
        ),
    ]
    backend = make_event_backend(tmp_path, events)
    try:
        response = await backend.collect_response({}, "req_123", None)
    finally:
        await backend.close()

    assert response["status"] == "incomplete"
    assert response["usage"]["total_tokens"] == 6
    assert response["output"][0]["content"][0]["text"] == "partial"


class RecordingTraffic:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_log_stream_summary_records_failures_and_incomplete_usage(tmp_path) -> None:
    traffic = RecordingTraffic()
    backend = ChatGptCodexBackend(
        settings=make_settings(tmp_path),
        auth_manager=None,  # type: ignore[arg-type]
        traffic=traffic,  # type: ignore[arg-type]
        client=httpx.AsyncClient(),
    )
    try:
        for event in _failure_events():
            backend._log_stream_summary("req_123", event)
        backend._log_stream_summary(
            "req_123",
            SSEEvent(
                "response.incomplete",
                json.dumps(
                    {
                        "response": {
                            "id": "resp_partial",
                            "model": "gpt-5.4-mini",
                            "status": "incomplete",
                            "usage": {"total_tokens": 6},
                        }
                    }
                ),
            ),
        )
    finally:
        await backend.close()

    failures = [r for r in traffic.records if r["phase"] == "upstream_stream_error"]
    assert len(failures) == 2
    assert failures[0]["event"] == "error"
    assert failures[0]["error"]["code"] == "server_is_overloaded"
    assert failures[1]["event"] == "response.failed"
    assert failures[1]["status"] == "failed"
    usage = [r for r in traffic.records if r["phase"] == "upstream_usage"]
    assert len(usage) == 1
    assert usage[0]["status"] == "incomplete"
    assert usage[0]["usage"] == {"total_tokens": 6}


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


def test_responses_route_strips_max_output_tokens_locally(tmp_path) -> None:
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
                "max_output_tokens": 20,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "max_output_tokens"
    assert "max_output_tokens" not in captured["payload"]


def test_chat_completions_route_strips_max_completion_tokens_locally(tmp_path) -> None:
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
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "max_completion_tokens": 20,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "max_completion_tokens"
    assert "max_completion_tokens" not in captured["payload"]


def test_completions_route_strips_max_tokens_locally(tmp_path) -> None:
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
            "/v1/completions",
            json={
                "model": "gpt-5.4",
                "prompt": "hello",
                "stream": False,
                "max_tokens": 20,
            },
        )

    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "max_tokens"
    assert "max_tokens" not in captured["payload"]


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


def test_redact_keeps_error_codes_readable_but_scrubs_auth_codes() -> None:
    """operator 2026-08-01: every upstream error code in the incident's
    traffic log read "[REDACTED]" — the blanket "code" redaction (meant for
    OAuth authorization codes) scrubbed the diagnostic vocabulary needed to
    classify the failure. Error objects keep their code; credential-shaped
    "code" keys stay scrubbed. The client-visible response body was never
    affected either way: redaction runs at log-serialization time only."""
    from airelay.traffic import redact_value

    # The upstream_stream_error record shape (error object under "error").
    record = redact_value(
        {
            "phase": "upstream_stream_error",
            "error": {
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
                "message": "Your input exceeds the context window of this model.",
                "param": "input",
            },
        }
    )
    assert record["error"]["code"] == "context_length_exceeded"

    # response.failed nests the error inside the response object.
    failed = redact_value(
        {"response": {"error": {"code": "server_is_overloaded", "message": "overloaded"}}}
    )
    assert failed["response"]["error"]["code"] == "server_is_overloaded"

    # The inline top-level variant carries code+message with no wrapper.
    inline = redact_value({"code": "invalid_prompt", "message": "rejected"})
    assert inline["code"] == "invalid_prompt"

    # Snapshot of an outbound error body: the logged JSON keeps the code.
    snapshot = snapshot_body(
        "application/json",
        b'{"error":{"type":"invalid_request_error","code":"context_length_exceeded","message":"too big","param":"input"}}',
    )
    assert snapshot["json"]["error"]["code"] == "context_length_exceeded"

    # OAuth shapes stay scrubbed: callback query params and token-exchange
    # bodies have no "message" sibling and no "error" parent.
    callback = redact_value({"query": {"code": "authz-one-time-secret", "state": "xyz"}})
    assert callback["query"]["code"] == "[REDACTED]"
    exchange = redact_value({"grant_type": "authorization_code", "code": "authz-one-time-secret"})
    assert exchange["code"] == "[REDACTED]"

    # The scoping frees only "code": other secrets inside an error object
    # would still be scrubbed.
    mixed = redact_value({"error": {"code": "x", "message": "m", "access_token": "tok"}})
    assert mixed["error"]["access_token"] == "[REDACTED]"
    assert mixed["error"]["code"] == "x"


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


def test_chat_route_shapes_upstream_failure_as_openai_error(tmp_path) -> None:
    """Regression: a response.failed stream used to come back as HTTP 200
    with {"content": null, "finish_reason": "stop", "usage": null}."""
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        client.app.state.backend.collect_response = collect_via_events(
            tmp_path, [_created_event(), *_failure_events()]
        )
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 502
    body = response.json()
    assert "choices" not in body
    assert body["error"]["code"] == "server_is_overloaded"
    assert "overloaded" in body["error"]["message"]
    # The desktop usage view reads FastAPI's default envelope; keep a
    # human-readable string mirror alongside the OpenAI error object.
    assert isinstance(body["detail"], str)


def test_responses_route_non_streaming_failure_returns_error(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        client.app.state.backend.collect_response = collect_via_events(
            tmp_path, [_created_event(), *_failure_events()]
        )
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "stream": False,
            },
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "server_is_overloaded"


def test_chat_stream_surfaces_mid_stream_failure_in_band(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        for event in _failure_events():
            yield event

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
    assert '"content": "Hel"' in response.text
    assert '"code": "server_is_overloaded"' in response.text
    assert "data: [DONE]" not in response.text


def test_chat_stream_first_event_failure_returns_http_error(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        for event in _failure_events():
            yield event

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

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "server_is_overloaded"


def test_chat_stream_invalid_request_returns_400_with_real_code(tmp_path) -> None:
    """Streaming sibling of the 2026-08-01 regression, through the real
    pool: the pre-content failure event classifies as a client error and
    surfaces as HTTP 400 before SSE headers, with the upstream code intact
    in the client-visible body (only the traffic LOG ever scrubbed it)."""
    settings = make_settings(tmp_path)
    write_openai_auth(settings)
    app = create_app(settings)

    async def scripted_stream(payload, request_id, session_id):
        del payload, request_id, session_id
        for event in [_created_event(), *_oversized_input_failure_events()]:
            yield event

    with TestClient(app) as client:
        pool = client.app.state.backend
        pool._accounts[0].backend.stream_response_events = scripted_stream
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        benched = pool._accounts[0].is_limited(time.monotonic())

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"
    assert error["param"] == "input"
    assert "at their limits" not in error["message"]
    assert benched is False


def test_chat_stream_empty_upstream_returns_502(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        return
        yield  # pragma: no cover - makes this an async generator

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

    assert response.status_code == 502
    assert "without a response payload" in response.json()["error"]["message"]


def test_chat_stream_silent_death_emits_in_band_error(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hi"}))

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
    assert '"content": "Hi"' in response.text
    assert '"code": "incomplete_stream"' in response.text
    assert "data: [DONE]" not in response.text


def test_chat_stream_incomplete_finishes_with_length_and_done(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event("resp_partial")
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "part"}))
        yield SSEEvent(
            "response.incomplete",
            json.dumps(
                {
                    "response": {
                        "id": "resp_partial",
                        "status": "incomplete",
                        "usage": {"input_tokens": 1, "output_tokens": 5, "total_tokens": 6},
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
    assert '"finish_reason": "length"' in response.text
    assert "data: [DONE]" in response.text
    assert '"error"' not in response.text


def test_completions_stream_surfaces_mid_stream_failure_in_band(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        for event in _failure_events():
            yield event

    with TestClient(app) as client:
        client.app.state.backend.stream_response_events = fake_stream_response_events
        response = client.post(
            "/v1/completions",
            json={"model": "gpt-5.4-mini", "stream": True, "prompt": "hello"},
        )

    assert response.status_code == 200
    assert '"text": "Hel"' in response.text
    assert '"code": "server_is_overloaded"' in response.text
    assert "data: [DONE]" not in response.text


def test_responses_stream_first_event_failure_returns_http_error(tmp_path) -> None:
    """The passthrough lane used to commit SSE headers unconditionally; a
    stream that is dead on arrival now surfaces as a real HTTP error (and
    is retriable) instead of an SSE body a client must parse for failure."""
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        for event in _failure_events():
            yield event

    with TestClient(app) as client:
        client.app.state.backend.stream_response_events = fake_stream_response_events
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "stream": True,
            },
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "server_is_overloaded"


def test_responses_stream_invalid_request_returns_400_passthrough(tmp_path) -> None:
    """The /v1/responses passthrough shares the one classifier: the same
    pre-content invalid_request_error surfaces as a 400 with the upstream
    error object intact, not a 502 or an SSE body."""
    settings = make_settings(tmp_path)
    write_openai_auth(settings)
    app = create_app(settings)

    async def scripted_stream(payload, request_id, session_id):
        del payload, request_id, session_id
        for event in [_created_event(), *_oversized_input_failure_events()]:
            yield event

    with TestClient(app) as client:
        pool = client.app.state.backend
        pool._accounts[0].backend.stream_response_events = scripted_stream
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "stream": True,
            },
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"
    assert error["param"] == "input"


def test_responses_stream_passes_post_content_failure_events_verbatim(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        for event in _failure_events():
            yield event

    with TestClient(app) as client:
        client.app.state.backend.stream_response_events = fake_stream_response_events
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: error" in response.text
    assert "event: response.failed" in response.text


def test_responses_stream_translates_raised_backend_error_to_error_event(tmp_path) -> None:
    """A transport failure mid-stream used to kill the SSE body silently;
    it now surfaces as an in-band `error` event in the Responses grammar."""
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        raise BackendError(502, "Upstream connection failed: mid-stream timeout")

    with TestClient(app) as client:
        client.app.state.backend.stream_response_events = fake_stream_response_events
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "mid-stream timeout" in response.text


def test_chat_stream_translates_raised_backend_error_to_in_band_error(tmp_path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        raise BackendError(502, "Upstream connection failed: mid-stream timeout")

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
    assert '"content": "Hel"' in response.text
    assert "mid-stream timeout" in response.text
    assert "data: [DONE]" not in response.text


def test_chat_stream_does_not_retry_after_headers_are_committed(tmp_path) -> None:
    """Retry stops at the pre-header phase: a failure after content started
    streaming surfaces in-band exactly once, with no second upstream call."""
    settings = make_settings(
        tmp_path, openai_retry_attempts=2, openai_retry_backoff_seconds=(0.0,)
    )
    app = create_app(settings)
    calls = {"count": 0}

    async def fake_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        calls["count"] += 1
        yield _created_event()
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        for event in _failure_events():
            yield event

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

    assert calls["count"] == 1
    assert response.status_code == 200
    assert '"code": "server_is_overloaded"' in response.text
    assert "data: [DONE]" not in response.text


def _write_second_account(settings: Settings, slug: str, account_id: str) -> None:
    """A second pooled account, so route tests exercise the real pool."""
    import base64

    def segment(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = segment({"alg": "none"})
    claims = segment(
        {
            "email": f"{slug}@example.com",
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_account_id": account_id,
            },
        }
    )
    root = settings.data_dir / "accounts" / slug
    root.mkdir(parents=True, exist_ok=True)
    (root / "auth.json").write_text(
        json.dumps(
            {
                "bound_account_id": account_id,
                "tokens": {
                    "id_token": f"{header}.{claims}.sig",
                    "access_token": f"access-{account_id}",
                    "refresh_token": f"refresh-{account_id}",
                    "account_id": account_id,
                },
                "last_refresh": "2099-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _quiet_pool_background(monkeypatch) -> None:
    """Launch probes hit the network; route tests must stay offline."""
    from airelay.accounts import OpenAiAccountPool

    async def noop(self, request_id: str = "startup") -> None:
        return None

    monkeypatch.setattr(OpenAiAccountPool, "warm_start", noop)
    monkeypatch.setattr(OpenAiAccountPool, "usage_refresh_loop", noop)


def test_chat_route_rate_limited_account_fails_over_then_is_skipped(tmp_path, monkeypatch) -> None:
    """End to end through the real two-account pool: a rate-limited account
    fails over to the available one within the same request, and later
    requests route straight to the available account without touching the
    limited one again."""
    _quiet_pool_background(monkeypatch)
    settings = make_settings(
        tmp_path, openai_retry_attempts=2, openai_retry_backoff_seconds=(0.0,)
    )
    write_openai_auth(settings)
    _write_second_account(settings, "second", "acct_456")
    app = create_app(settings)
    calls = {"limited": 0, "healthy": 0}

    async def limited_collect(payload, request_id, session_id):
        calls["limited"] += 1
        raise BackendError(
            429,
            json.dumps(
                {
                    "error": {
                        "type": "usage_limit_reached",
                        "message": "weekly window spent",
                        "resets_in_seconds": 7200,
                    }
                }
            ),
        )

    async def healthy_collect(payload, request_id, session_id):
        calls["healthy"] += 1
        return {
            "id": "resp_ok",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4-mini",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    body = {
        "model": "gpt-5.4-mini",
        "stream": False,
        "messages": [{"role": "user", "content": "hello"}],
    }
    with TestClient(app) as client:
        pool = client.app.state.backend
        assert pool.size == 2
        pool._accounts[0].backend.collect_response = limited_collect
        pool._accounts[1].backend.collect_response = healthy_collect

        first = client.post("/v1/chat/completions", json=body)
        second = client.post("/v1/chat/completions", json=body)

    assert first.status_code == 200
    assert first.json()["choices"][0]["message"]["content"] == "ok"
    assert second.status_code == 200
    # The limited account was hit exactly once (the failover that benched
    # it); the second request routed directly to the available account.
    assert calls["limited"] == 1
    assert calls["healthy"] == 2


def test_chat_route_oversized_input_fails_fast_without_rotation_or_retry(tmp_path, monkeypatch) -> None:
    """The 2026-08-01 incident, end to end through the real two-account
    pool WITH retry enabled (req_7d3b0b16f7ee43a5a6569c38b6d46133): a
    request whose input exceeds the context window is rejected
    deterministically by the upstream, so it must cost exactly ONE paid
    upstream call — no failover to the second account, no backoff rounds,
    no benches — and the client must receive the upstream 400 verbatim.

    operator 2026-08-01: this exact shape previously burned 8 upstream
    calls across 4 backoff rounds on both accounts and answered 502 "All 2
    OpenAI accounts are at their limits (earliest retry in 25s)" while the
    accounts had 64%/33% of their weekly budgets left — a wrong error AND
    wasted inference spend for one client mistake."""
    _quiet_pool_background(monkeypatch)
    settings = make_settings(
        tmp_path, openai_retry_attempts=2, openai_retry_backoff_seconds=(0.0,)
    )
    write_openai_auth(settings)
    _write_second_account(settings, "second", "acct_456")
    app = create_app(settings)
    calls = {"first": 0, "second": 0}

    def scripted_stream(name: str):
        async def stream(payload, request_id, session_id):
            del payload, request_id, session_id
            calls[name] += 1
            for event in [_created_event(), *_oversized_input_failure_events()]:
                yield event

        return stream

    with TestClient(app) as client:
        pool = client.app.state.backend
        assert pool.size == 2
        # Override the raw per-account streams: the pool's REAL collect
        # logic and the REAL classifier run over the incident grammar.
        pool._accounts[0].backend.stream_response_events = scripted_stream("first")
        pool._accounts[1].backend.stream_response_events = scripted_stream("second")

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        now = time.monotonic()
        benched = [account.is_limited(now) for account in pool._accounts]

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"
    assert error["param"] == "input"
    assert "context window" in error["message"]
    # The message is the upstream's own: no fabricated account-limit claim.
    assert "at their limits" not in error["message"]
    # One attempt total: no rotation onto the second account, no retry.
    assert calls["first"] + calls["second"] == 1
    # And no account was benched over a request-scoped error.
    assert benched == [False, False]


def test_chat_route_retry_repass_survives_a_full_pool_outage(tmp_path, monkeypatch) -> None:
    """End to end: both accounts fail transiently on the first pass, the
    retry re-runs the pool pass after backoff, and the request succeeds."""
    _quiet_pool_background(monkeypatch)
    settings = make_settings(
        tmp_path, openai_retry_attempts=2, openai_retry_backoff_seconds=(0.0,)
    )
    write_openai_auth(settings)
    _write_second_account(settings, "second", "acct_456")
    app = create_app(settings)
    calls = {"first": 0, "second": 0}

    async def first_account_collect(payload, request_id, session_id):
        calls["first"] += 1
        raise BackendError(502, '{"error": {"code": "server_is_overloaded", "message": "down"}}')

    async def second_account_collect(payload, request_id, session_id):
        calls["second"] += 1
        if calls["second"] == 1:
            raise BackendError(502, '{"error": {"code": "server_is_overloaded", "message": "down"}}')
        return {
            "id": "resp_ok",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4-mini",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    with TestClient(app) as client:
        pool = client.app.state.backend
        assert pool.size == 2
        pool._accounts[0].backend.collect_response = first_account_collect
        pool._accounts[1].backend.collect_response = second_account_collect

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    # Pass 1 failed on both accounts; the retry pass reached the recovered
    # account. Nothing was retried more than the policy allows.
    assert calls["first"] + calls["second"] <= 4
    assert calls["second"] >= 2


def test_chat_route_retries_transient_upstream_failure(tmp_path) -> None:
    settings = make_settings(
        tmp_path, openai_retry_attempts=2, openai_retry_backoff_seconds=(0.0,)
    )
    app = create_app(settings)
    calls = {"count": 0}

    async def flaky_collect_response(payload, request_id, session_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise BackendError(502, '{"error": {"code": "server_is_overloaded", "message": "x"}}')
        return {
            "id": "resp_ok",
            "object": "response",
            "created_at": 1,
            "model": "gpt-5.4-mini",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    with TestClient(app) as client:
        client.app.state.backend.collect_response = flaky_collect_response
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert calls["count"] == 2
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "ok"
    assert body["usage"]["total_tokens"] == 2


def test_chat_route_returns_error_after_retries_exhausted(tmp_path) -> None:
    settings = make_settings(
        tmp_path, openai_retry_attempts=2, openai_retry_backoff_seconds=(0.0,)
    )
    app = create_app(settings)
    calls = {"count": 0}

    async def failing_collect_response(payload, request_id, session_id):
        calls["count"] += 1
        raise BackendError(502, '{"error": {"code": "server_is_overloaded", "message": "down"}}')

    with TestClient(app) as client:
        client.app.state.backend.collect_response = failing_collect_response
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": False,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert calls["count"] == 3  # initial attempt + 2 retries
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "server_is_overloaded"


def test_chat_stream_retries_pre_header_failure(tmp_path) -> None:
    settings = make_settings(
        tmp_path, openai_retry_attempts=1, openai_retry_backoff_seconds=(0.0,)
    )
    app = create_app(settings)
    calls = {"count": 0}

    async def flaky_stream_response_events(payload, request_id, session_id):
        del payload, request_id, session_id
        calls["count"] += 1
        if calls["count"] == 1:
            for event in _failure_events():
                yield event
            return
        yield _created_event("resp_ok")
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "ok"}))
        yield SSEEvent(
            "response.completed",
            json.dumps(
                {
                    "response": {
                        "id": "resp_ok",
                        "status": "completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }
                }
            ),
        )

    with TestClient(app) as client:
        client.app.state.backend.stream_response_events = flaky_stream_response_events
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.4-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert calls["count"] == 2
    assert response.status_code == 200
    assert '"content": "ok"' in response.text
    assert "data: [DONE]" in response.text
    assert '"error"' not in response.text


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


def test_models_route_advertises_configured_extra_models_with_dedupe(tmp_path) -> None:
    """The upstream catalog lags what the backend serves; configured extra
    ids appear in /v1/models exactly once even when the catalog catches up."""
    settings = make_settings(
        tmp_path,
        openai_extra_models=("gpt-5.6-sol", "gpt-5.5"),
    )
    app = create_app(settings)

    async def fake_list_models(request_id):
        del request_id
        return {"models": [{"slug": "gpt-5.5"}, {"slug": "gpt-5.4"}]}

    with TestClient(app) as client:
        client.app.state.backend.list_models = fake_list_models
        response = client.get("/v1/models")

    assert response.status_code == 200
    openai_models = [item for item in response.json()["data"] if not item["id"].startswith("claude:")]
    ids = [item["id"] for item in openai_models]
    assert "gpt-5.6-sol" in ids
    assert ids.count("gpt-5.5") == 1  # deduped against the catalog
    # Every OpenAI record advertises its reasoning modes for API consumers.
    for item in openai_models:
        assert item["airelays"]["reasoning"]["modes"] == ["none", "low", "medium", "high", "xhigh"]
        assert item["airelays"]["reasoning"]["default"] == "none"


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
    # Servers discover reasoning support straight from the API.
    reasoning = claude_model["airelays"]["reasoning"]
    assert reasoning["parameter"] == "reasoning_effort"
    assert reasoning["modes"] == ["low", "medium", "high", "xhigh", "max"]
    # ... and structured output support the same way.
    structured = claude_model["airelays"]["structured_output"]
    assert structured["parameter"] == "response_format"
    assert structured["types"] == ["json_schema", "json_object"]


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


def test_claude_stream_requests_validate_before_headers(tmp_path) -> None:
    """Regression (adversarial review C1): a validation error raised inside
    the response generator can only surface as an empty 200 stream. Invalid
    streaming requests must return real 4xx status codes."""
    settings = make_settings(
        tmp_path,
        require_bearer_auth=True,
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    settings.write_bearer_token("relay-token")
    app = create_app(settings)

    with TestClient(app) as client:
        bad_effort = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "reasoning_effort": "ultrathink",
            },
        )
        bad_format = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "response_format": {"type": "xml"},
            },
        )
        bad_completion = client.post(
            "/v1/completions",
            headers={"authorization": "Bearer relay-token"},
            json={
                "model": "claude:sonnet",
                "prompt": "hi",
                "stream": True,
                "response_format": {"type": "json_object"},
            },
        )

    assert bad_effort.status_code == 422
    assert "Supported values: low, medium, high, xhigh, max" in bad_effort.json()["detail"]
    assert bad_format.status_code == 422
    assert "Supported types: text, json_object, json_schema" in bad_format.json()["detail"]
    assert bad_completion.status_code == 422
    assert "response_format" in bad_completion.json()["detail"]


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
