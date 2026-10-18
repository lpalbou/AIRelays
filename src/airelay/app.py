from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from airelay import __version__
from airelay.accounts import build_pool
from airelay.auth import AuthManager, AuthenticationError
from airelay.backend import (
    FAILURE_EVENTS,
    BackendError,
    ChatGptCodexBackend,
    SSEEvent,
    encode_sse,
    failure_backend_error,
    stream_error_object,
)
from airelay.config import APP_NAME, Settings
from airelay.config import BALANCE_STRATEGIES, _normalized_balance
from airelay.chat_responses import (
    ChatStreamToResponses,
    chat_chunks,
    chat_to_response,
    items_to_messages,
    messages_to_items,
    request_to_chat,
    sse as chat_responses_sse,
)
from airelay.conversations import ConversationTurn
from airelay.html import render_home
from airelay.images import (
    IMAGE_ADAPTATION_REASON,
    EditImage,
    ImageRequest,
    coerce_image_form,
    edit_image_from_bytes,
    edit_image_from_data_url,
    image_stream_events,
    merge_image_responses,
    prepare_image_edit,
    prepare_image_generation,
    summarize_image_payload,
    unapplied_image_parameters,
)
from airelay.providers import (
    CLAUDE_CHAT_PARAMETERS,
    CLAUDE_COMPLETION_PARAMETERS,
    claude_ignored_tool_options,
    ProviderError,
    ProviderRegistry,
)
from airelay.retry import RetryPolicy, retry_call
from airelay.security import EndpointProtector
from airelay.errors import InvalidRequestError, error_body, error_type
from airelay.store import AppStore
from airelay.traffic import TrafficLogger, snapshot_body
from airelay.transforms import (
    CHAT_PARAMETERS,
    normalize_reset_credits_payload,
    normalize_reset_redeem_payload,
    COMPLETIONS_PARAMETERS,
    expand_local_files,
    inline_chat_files,
    is_responses_shape,
    strip_unknown_parameters,
    uses_legacy_functions,
    chat_route_request_to_responses,
    completion_chunk,
    completions_to_responses,
    strip_unsupported_response_parameters,
    TranslationError,
    conversation_id_of,
    refuse_tools_unless_allowed,
    validated_metadata,
    UnknownLocalFileError,
    chat_completion_chunk,
    chat_completions_to_responses,
    normalize_conversation_items,
    normalize_subscription_status_payload,
    prepare_response_request,
    response_item_to_chat_tool_call,
    responses_to_completion,
    responses_to_chat_completion,
    completion_usage_from_responses,
    created_timestamp,
    incomplete_finish_reason,
    reject_unhonored_parameters,
    stream_include_usage,
    usage_chunk,
)


# The Claude CLI exposes no sampling or output-token-limit controls, so the
# parameters standard OpenAI SDKs send by default get the same documented
# treatment as on the OpenAI runtime: stripped and disclosed, never a hard
# failure.
CLAUDE_ADAPTATION_REASON = (
    "The Claude runtime's local CLI has no sampling or output-token-limit "
    "controls, so the compatibility layer omitted these parameters."
)
CHAT_ROUTE_RESPONSES_SHAPE_REASON = (
    "The chat route accepted a Responses-shaped request body and translated "
    "the response back to chat.completions for client compatibility."
)

# OpenAI SDKs retry 5xx responses; a 501 cannot succeed on retry.
NOT_RETRYABLE = {"x-should-retry": "false"}

# `n` images are `n` upstream calls (the backend returns one image per call,
# verified live); this many run at once per request.
IMAGE_FANOUT_CONCURRENCY = 3

MAX_DISCLOSURE_HEADER = 2048

# Upload purposes OpenAI's files API accepts.
FILE_PURPOSES = ("assistants", "batch", "fine-tune", "vision", "user_data", "evals")

# Endpoints the desktop app and health checks poll continuously. Logging
# them to the JSONL traffic log floods it and evicts real requests from the
# recent-window the Traffic view reads, so they are never logged as traffic.
MONITORING_PATHS = frozenset(
    {
        "/healthz",
        "/v1/relay/status",
        "/v1/subscription/status",
        "/v1/account/rate_limits",
    }
)


def _request_id(request: Request | None = None) -> str:
    if request is None:
        return f"req_{uuid.uuid4().hex}"
    existing = getattr(request.state, "request_id", None)
    if existing:
        return existing
    created = f"req_{uuid.uuid4().hex}"
    request.state.request_id = created
    return created


def _stream_error_event(message: str, code: str, status_code: int, type_: str | None = None) -> bytes:
    """OpenAI-style in-band SSE error event, for failures that happen after
    the response headers are already on the wire."""
    payload = {"error": {"message": message, "type": type_ or error_type(status_code), "param": None, "code": code}}
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n".encode("utf-8")


async def _first_chunk(stream: AsyncIterator[bytes]) -> bytes:
    """Run a provider stream until its first chunk before the response
    starts: account selection, quota, sign-in and CLI failures then reach
    the client as real HTTP errors (retryable 429/503 with Retry-After)
    instead of in-band errors behind a 200."""
    try:
        return await anext(stream)
    except StopAsyncIteration:
        raise ProviderError(502, "The Claude CLI ended without any output.", code="provider_failure") from None
    except BaseException:
        await stream.aclose()
        raise


async def _with_first(first: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    async for chunk in rest:
        yield chunk


def _parse_event_data(event: SSEEvent) -> Any:
    try:
        return json.loads(event.data)
    except json.JSONDecodeError:
        return None


def _stream_failure_event(parsed: Any) -> bytes:
    """In-band error for an upstream failure event, carrying the upstream
    reason instead of a silently truncated stream."""
    error = stream_error_object(parsed) or {}
    message, code, type_ = error.get("message"), error.get("code") or error.get("type"), error.get("type")
    code = code if isinstance(code, str) and code else "upstream_error"
    return _stream_error_event(
        message if isinstance(message, str) and message else "The upstream response failed.",
        code,
        429 if code in _RATE_LIMIT_CODES else 502,
        type_ if isinstance(type_, str) and type_ else None,
    )


# Upstream failure codes that mean "retry later", typed `rate_limit_error`.
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "usage_limit_reached", "rate_limit_error"})


_INCOMPLETE_STREAM_EVENT = _stream_error_event(
    "Upstream stream ended before response.completed.", "incomplete_stream", 502
)


def _synthetic_failure_event(exc: BackendError) -> SSEEvent:
    """A BackendError raised after headers were committed (mid-stream pool
    exhaustion, transport death), rendered in the upstream's own failure-
    event grammar so each lane's existing failure handling applies: chat and
    completions translate it to an in-band error, the Responses passthrough
    delivers it verbatim as an `error` event."""
    payload = {"type": "error", "error": _backend_error_payload(exc)["error"]}
    return SSEEvent("error", json.dumps(payload, ensure_ascii=True))


def _backend_error_payload(exc: BackendError) -> dict[str, Any]:
    """OpenAI-shaped error body for upstream failures. The upstream error
    object is passed through when the detail already carries one. The
    top-level "detail" mirror keeps pre-existing readers of FastAPI's
    default shape working (the desktop usage view parses it)."""
    error: dict[str, Any] | None = None
    detail = exc.detail or ""
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        error = dict(parsed["error"])
    if error is None:
        # e.g. {"detail": "Unsupported parameter: stop"}: surface the message.
        message = parsed.get("detail") if isinstance(parsed, dict) else None
        error = {"message": message if isinstance(message, str) and message else detail,
                 "type": "upstream_error", "code": None}
    error.setdefault("type", "upstream_error")
    error.setdefault("param", None)
    error.setdefault("code", None)
    message = error.get("message")
    return {
        "error": error,
        "detail": message if isinstance(message, str) and message else detail,
    }


class _ApiError(HTTPException):
    """An HTTP error that keeps its machine-readable OpenAI error `code`."""

    def __init__(self, status_code: int, detail: str, *, code: str | None = None, param: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.param = param


def _http_error(exc: Exception) -> Exception:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, BackendError):
        if exc.status_code == 401:
            return AuthenticationError(
                "Upstream ChatGPT login is missing or expired. Run `airelays login` first.",
                code="upstream_auth_rejected",
            )
        # Returned as-is so the BackendError handler renders an OpenAI-shaped
        # error body instead of FastAPI's {"detail": ...} envelope.
        return exc
    if isinstance(exc, ProviderError):
        status = exc.status_code
        # A Claude CLI sign-in problem is the relay's, not the caller's API
        # key: 401 would tell OpenAI clients their own credentials are bad.
        headers: dict[str, str] = {}
        if exc.code == "provider_auth_error":
            status = 503
            # Retrying cannot sign the CLI in; tell OpenAI SDKs not to.
            headers["x-should-retry"] = "false"
        if exc.retry_after_seconds and status in (429, 503):
            headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_seconds)))
            if exc.retry_after_seconds > 60:
                # OpenAI SDKs would retry on a short backoff; a wait this long cannot be met.
                headers["x-should-retry"] = "false"
        return _ApiError(status, exc.detail, code=exc.code, headers=headers or None)
    if isinstance(exc, AuthenticationError):
        return exc
    if isinstance(exc, InvalidRequestError):
        return _ApiError(exc.status_code, str(exc), code=exc.code, param=exc.param)
    raise _ApiError(500, str(exc))


def _file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _first_exception(group: BaseException) -> BaseException:
    """The error to report for a failed image fan-out: the first upstream
    or request error inside the (possibly nested) group, else the first
    exception of any kind."""
    flat: list[BaseException] = []

    def walk(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for inner in error.exceptions:
                walk(inner)
        else:
            flat.append(error)

    walk(group)
    for error in flat:
        if isinstance(error, (BackendError, HTTPException, AuthenticationError, InvalidRequestError, ProviderError)):
            return error
    return flat[0] if flat else group


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_directories()
    settings.validate_provider_guardrails()
    traffic = TrafficLogger(settings.logs_dir, settings.log_policy())
    store = AppStore(settings.data_dir)
    # One user may have several of their own subscriptions enrolled; the
    # pool balances across them and degenerates to plain single-account
    # behavior when only the legacy login exists.
    backend = build_pool(settings, traffic)
    auth = backend.manager_for_primary() if backend.size > 0 else AuthManager(
        settings.data_dir,
        settings.auth_storage_mode,
        settings.issuer_base_url,
        client_id=settings.client_id,
    )
    providers = ProviderRegistry(
        settings,
        openai_auth=auth,
        openai_backend=backend,
        traffic=traffic,
        account_pool=backend,
    )
    protector = EndpointProtector(settings, traffic)
    retry_policy = RetryPolicy(
        attempts=settings.openai_retry_attempts,
        backoff_seconds=settings.openai_retry_backoff_seconds,
    )
    supported_routes = [
        "/v1/models",
        "/v1/subscription/status",
        "/v1/account/rate_limits",
        "/v1/relay/status",
        "/v1/relay/accounts/refresh",
        "/v1/relay/accounts/resets",
        "/v1/relay/accounts/resets/redeem",
        "/v1/relay/logging",
        "/v1/relay/balance",
        "/v1/completions",
        "/v1/responses",
        "/v1/chat/completions",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/files",
        "/v1/conversations",
        "/no-tools/v1/models",
        "/no-tools/v1/completions",
        "/no-tools/v1/responses",
        "/no-tools/v1/chat/completions",
    ]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        had_relay_token = bool(settings.resolve_bearer_token())
        settings.ensure_runtime_state()
        app.state.settings = settings
        app.state.traffic = traffic
        app.state.store = store
        app.state.auth = auth
        app.state.backend = backend
        app.state.providers = providers
        app.state.protector = protector
        await asyncio.to_thread(traffic.maintain)
        traffic.write(
            {
                "phase": "endpoint_security_ready",
                "request_id": "startup",
                "require_bearer_auth": settings.require_bearer_auth,
                "bearer_token_created": settings.require_bearer_auth
                and not had_relay_token
                and bool(settings.resolve_bearer_token()),
                "bearer_token_file": str(settings.bearer_token_file),
                "rate_limit_per_minute": settings.rate_limit_per_minute,
                "rate_limit_burst": settings.rate_limit_burst,
                "concurrent_requests_per_ip": settings.concurrent_requests_per_ip,
            }
        )
        # Launch-time pool warm-up (multi-account only): benches accounts
        # that are already at their usage limit and fills the per-account
        # model catalogs, so balancing routes correctly from the first
        # request instead of relearning capacity by wasting a 429. Runs in
        # the background — serving never waits on it.
        warm_task = (
            asyncio.get_running_loop().create_task(backend.warm_start())
            if hasattr(backend, "warm_start")
            else None
        )
        # Keeps the balanced strategy's capacity signal fresh and benches
        # exhausted accounts proactively between manual status loads.
        usage_task = (
            asyncio.get_running_loop().create_task(backend.usage_refresh_loop())
            if hasattr(backend, "usage_refresh_loop")
            else None
        )
        claude_warm_task = asyncio.create_task(providers.claude.warm_start()) if providers.claude else None
        claude_usage_task = asyncio.create_task(providers.claude.usage_refresh_loop()) if providers.claude else None
        async def maintain_logs() -> None:
            while True:
                await asyncio.sleep(traffic.cleanup_interval_seconds)
                await asyncio.to_thread(traffic.maintain)

        retention_task = asyncio.create_task(maintain_logs())
        try:
            yield
        finally:
            tasks = [task for task in (warm_task, usage_task, retention_task, claude_warm_task, claude_usage_task) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await backend.close()

    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)

    def authentication_error_payload(exc: AuthenticationError) -> tuple[int, dict[str, Any], dict[str, str] | None]:
        message = str(exc)
        code = getattr(exc, "code", "upstream_auth_error")
        headers: dict[str, str] | None = None
        if code == "upstream_auth_missing" or message.startswith("No ChatGPT login found."):
            code = "upstream_auth_missing"
            headers = {"x-airelays-upstream-auth": "missing"}
        elif code == "upstream_auth_refresh_failed" or message.startswith("Token refresh failed:"):
            code = "upstream_auth_refresh_failed"
            headers = {"x-airelays-upstream-auth": "refresh_failed"}
        elif code == "upstream_auth_incomplete" or message.startswith("Stored auth does not include"):
            code = "upstream_auth_incomplete"
            headers = {"x-airelays-upstream-auth": "incomplete"}
        elif code == "upstream_auth_account_mismatch" or "bound to a different upstream account" in message:
            code = "upstream_auth_account_mismatch"
            headers = {"x-airelays-upstream-auth": "account_mismatch"}
        elif code == "upstream_auth_rejected":
            headers = {"x-airelays-upstream-auth": "rejected"}
        payload = error_body(message, status_code=503, code=code, type_="authentication_error")
        # Retrying cannot repair a missing or expired sign-in.
        headers = {**(headers or {}), "x-should-retry": "false"}
        return 503, payload, headers

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
        request_id = _request_id(request)
        status_code, payload, headers = authentication_error_payload(exc)
        return logged_json(request_id, payload, status_code=status_code, headers=headers)

    @app.exception_handler(BackendError)
    async def backend_error_handler(request: Request, exc: BackendError) -> JSONResponse:
        request_id = _request_id(request)
        payload = _backend_error_payload(exc)
        resets = payload["error"].get("resets_in_seconds")
        headers = None
        if (exc.status_code in (429, 503) and isinstance(resets, (int, float))
                and not isinstance(resets, bool) and resets > 0):
            # OpenAI SDKs wait this long, or give up when it exceeds their
            # retry ceiling, instead of retrying a spent quota blindly.
            headers = {"Retry-After": str(max(1, math.ceil(resets)))}
        return logged_json(request_id, payload, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = _request_id(request)
        message = "; ".join(f"{'.'.join(str(p) for p in error.get('loc', ()))}: {error.get('msg')}"
                            for error in exc.errors()) or "Invalid request."
        return logged_json(request_id, error_body(message, status_code=422), status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Runs outside the HTTP middleware, so it sets x-request-id itself.
        request_id = _request_id(request)
        return logged_json(request_id, error_body(f"Internal error: {exc}", status_code=500), status_code=500,
                           headers={"x-request-id": request_id})

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Every remaining error (validation, unknown model, unknown route...)
        # in OpenAI's error shape.
        request_id = _request_id(request)
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        payload = error_body(detail, status_code=exc.status_code, code=getattr(exc, "code", None),
                             param=getattr(exc, "param", None))
        headers = dict(getattr(exc, "headers", None) or {})
        if exc.status_code == 501:
            headers.update(NOT_RETRYABLE)
        return logged_json(request_id, payload, status_code=exc.status_code, headers=headers or None)

    @app.middleware("http")
    async def guard_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = _request_id(request)
        lease, rejection = protector.acquire(request_id, request)
        request.state.client_ip = lease.client_ip
        try:
            response = rejection if rejection is not None else await call_next(request)
        finally:
            protector.release(lease)
        # OpenAI SDKs surface this as the error/response `request_id`.
        response.headers["x-request-id"] = request_id
        return response

    # Count of real (non-monitoring) requests served by this process. The
    # desktop reads it from /v1/relay/status to blink the tray on activity.
    request_counter = {"total": 0}

    async def log_inbound(request_id: str, request: Request, body: bytes,
                          snapshot: dict[str, Any] | None = None) -> None:
        if request.url.path in MONITORING_PATHS:
            return
        request_counter["total"] += 1
        traffic.write(
            {
                "request_id": request_id,
                "phase": "inbound_request",
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params),
                "headers": dict(request.headers.items()),
                "client_ip": getattr(request.state, "client_ip", None),
                # Callers whose bodies are mostly image bytes pass a summary.
                "body": snapshot if snapshot is not None else snapshot_body(request.headers.get("content-type"), body),
            }
        )

    def _response_meta(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        meta: dict[str, Any] = {}
        for key in ("id", "object", "model", "usage"):
            if key in payload:
                meta[key] = payload[key]
        return meta

    def _adaptation_headers(ignored_parameters: list[str]) -> dict[str, str]:
        if not ignored_parameters:
            return {}
        # Names come from client JSON keys: percent-encoded so any key is a
        # valid header value, and capped (the traffic log keeps them all).
        listed = ",".join(quote(name, safe="_-.") for name in ignored_parameters)
        if len(listed) > MAX_DISCLOSURE_HEADER:
            listed = listed[:MAX_DISCLOSURE_HEADER].rsplit(",", 1)[0] + ",..."
        return {"x-airelays-ignored-parameters": listed}

    def log_adaptation(
        request_id: str,
        ignored_parameters: list[str],
        reason: str = (
            "The ChatGPT subscription backend does not support these OpenAI "
            "parameters, so the compatibility layer omitted them."
        ),
    ) -> None:
        if not ignored_parameters:
            return
        traffic.write(
            {
                "request_id": request_id,
                "phase": "compatibility_adaptation",
                "ignored_parameters": ignored_parameters,
                "reason": reason,
            }
        )

    async def resolve_request_model(model: Any, request_id: str):
        if not isinstance(model, str):
            return None
        if model.startswith(("claude:", "claude-")) and providers.claude_runtime is not None:
            await providers.claude_runtime.refresh_models()
        resolved = providers.resolve_model(model)
        traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_resolution",
                "provider": resolved.provider,
                "model": resolved.public_id,
            }
        )
        if resolved.provider == "openai":
            await providers.ensure_openai_model_supported(resolved.upstream_id, request_id)
        return resolved

    def logged_json(
        request_id: str,
        payload: Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        loggable: bool = True,
    ) -> JSONResponse:
        # Monitoring endpoints pass loggable=False so their inbound skip
        # (see MONITORING_PATHS) isn't undone by an orphan outbound record
        # that would surface as a junk "/" row in the Traffic view.
        if loggable:
            encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            entry = {
                "request_id": request_id,
                "phase": "outbound_response",
                "status_code": status_code,
                "body": snapshot_body("application/json", encoded),
            }
            entry.update(_response_meta(payload))
            traffic.write(entry)
        return JSONResponse(payload, status_code=status_code, headers=headers)

    def logged_body(
        request_id: str,
        body: bytes,
        media_type: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        traffic.write(
            {
                "request_id": request_id,
                "phase": "outbound_response",
                "status_code": status_code,
                "body": snapshot_body(media_type, body),
            }
        )
        return Response(body, media_type=media_type, status_code=status_code, headers=headers)

    def load_json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return parsed

    def require_text_runtime() -> None:
        """Files and conversations are relay-local and serve both runtimes."""
        if not (settings.enable_openai_provider or settings.enable_claude):
            raise HTTPException(
                status_code=501,
                detail="This route needs the OpenAI or the Claude runtime enabled.",
            )

    def claude_runtime() -> Any:
        runtime = providers.claude
        if runtime is None:
            raise ProviderError(503, "The Claude runtime is not available in this AIRelays process.",
                                code="provider_unavailable")
        return runtime

    # ----- images -----

    def require_openai_runtime() -> None:
        """The image routes exist on the ChatGPT subscription only."""
        if not settings.enable_openai_provider:
            raise HTTPException(
                status_code=501,
                detail="Image generation needs the OpenAI runtime enabled; Claude models do not generate images.",
            )

    def edit_image_from_reference(value: Any) -> EditImage:
        """An `image` entry given as text: a base64 data URL, or the id of
        a file uploaded to `/v1/files`."""
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequestError("`image` entries must be base64 `data:` URLs or local file ids (`file_...`).",
                                      param="image")
        if value.strip().lower().startswith("data:"):
            return edit_image_from_data_url(value)
        file_id = value.strip()
        try:
            record, raw = store.get_file_bytes(file_id)
        except KeyError as exc:
            raise InvalidRequestError(
                f"Unknown local file id `{file_id}`; `image` entries must be base64 `data:` URLs or ids of files "
                "uploaded to `/v1/files`.", param="image",
            ) from exc
        return edit_image_from_bytes(raw, what=f"file `{record.get('filename') or file_id}`")

    def logged_image_json(request_id: str, payload: dict[str, Any], headers: dict[str, str] | None) -> JSONResponse:
        """`logged_json` for an Images response: the traffic log keeps the
        shape and sizes, never megabytes of base64."""
        entry = {
            "request_id": request_id,
            "phase": "outbound_response",
            "status_code": 200,
            "body": {"kind": "json", "json": summarize_image_payload(payload)},
        }
        entry.update(_response_meta(payload))
        traffic.write(entry)
        return JSONResponse(payload, headers=headers or None)

    async def run_image_request(request: Request, request_id: str, prepared: ImageRequest) -> Response:
        """The shared tail of both image routes: `n` upstream calls (each
        with account failover and automatic retry) merged into one OpenAI
        Images response, delivered as JSON or as OpenAI's image stream
        events. The upstream answers whole, so every failure here is a real
        HTTP status, even for `stream: true`."""

        async def one_image() -> dict[str, Any]:
            return await retry_call(
                lambda: backend.create_image(prepared.operation, prepared.payload, request_id),
                policy=retry_policy,
                request_id=request_id,
                traffic=traffic,
                should_abort=request.is_disconnected,
            )

        if prepared.count == 1:
            parts = [await one_image()]
        else:
            semaphore = asyncio.Semaphore(IMAGE_FANOUT_CONCURRENCY)

            async def one_of_many() -> dict[str, Any]:
                async with semaphore:
                    return await one_image()

            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(one_of_many()) for _ in range(prepared.count)]
            except BaseExceptionGroup as errors:
                # One failed image fails the request, as on OpenAI; the
                # siblings were cancelled by the group.
                raise _first_exception(errors) from None
            parts = [task.result() for task in tasks]
        response = merge_image_responses(parts)
        # The answer is known before any byte leaves, so a `quality` or
        # `size` the service replaced is disclosed on this very response.
        ignored = [*prepared.ignored, *unapplied_image_parameters(prepared.payload, response)]
        log_adaptation(request_id, ignored, reason=IMAGE_ADAPTATION_REASON)
        headers = _adaptation_headers(ignored)
        traffic.write(
            {
                "request_id": request_id,
                "phase": "outbound_usage",
                "provider": "openai",
                "model": prepared.model,
                "images": len(response.get("data") or []),
                "usage": response.get("usage"),
            }
        )
        if not prepared.stream:
            return logged_image_json(request_id, response, headers)
        events = image_stream_events(response, prepared.operation)

        async def event_stream() -> AsyncIterator[bytes]:
            for event in events:
                chunk = f"data: {json.dumps(event, ensure_ascii=True)}\n\n".encode("utf-8")
                traffic.write(
                    {
                        "request_id": request_id,
                        "phase": "outbound_stream_chunk",
                        "body": {"kind": "json", "bytes": len(chunk), "json": summarize_image_payload(event)},
                    }
                )
                yield chunk

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers or None)

    async def claude_chat(request_id: str, body: dict[str, Any], conversation_id: Any,
                          turn_items: list[Any]) -> tuple[Any, ConversationTurn | None]:
        """One Claude run for every route: the chat payload, or its SSE
        stream (primed, so failures before the first byte are real HTTP
        errors), and the conversation turn to record."""
        runtime = claude_runtime()
        conversation_id = conversation_id_of(conversation_id)
        turn = None
        if conversation_id:
            try:
                turn, history = ConversationTurn.open(store, conversation_id, turn_items)
            except KeyError as exc:
                raise _ApiError(404, f"Unknown conversation id `{conversation_id}`.") from exc
            # The stored history goes after the system messages and before
            # the request's own messages, which are sent as they came.
            messages = body.get("messages") or []
            system = [m for m in messages if m.get("role") in ("system", "developer")]
            turn_messages = [m for m in messages if m.get("role") not in ("system", "developer")]
            body = {**body, "messages": system + items_to_messages(history) + turn_messages}
        body = {**body, "messages": inline_chat_files(body.get("messages") or [], store)}
        if not body.get("stream"):
            payload = await runtime.create_chat_completion(body, request_id)
            if payload.get("usage") is not None:
                traffic.write({"request_id": request_id, "phase": "outbound_usage", "provider": "claude",
                               "model": payload.get("model"), "usage": payload["usage"]})
            return payload, turn
        stream = runtime.stream_chat_completion(body, request_id)
        first = await _first_chunk(stream)
        return (first, stream), turn

    async def claude_chat_stream(request_id: str, primed: tuple[bytes, AsyncIterator[bytes]],
                                 turn: ConversationTurn | None) -> AsyncIterator[bytes]:
        """The Claude chat SSE stream for a client; records the conversation
        turn from the chunks seen once the stream completes."""
        first, stream = primed
        recorder = ChatStreamToResponses({}, "") if turn else None
        try:
            async for chunk in _with_first(first, stream):
                traffic.write({"request_id": request_id, "phase": "outbound_stream_chunk", "provider": "claude",
                               "body": snapshot_body("text/event-stream", chunk)})
                if recorder is not None:
                    for item in chat_chunks(chunk):
                        if item == "[DONE]":
                            recorder.finish()
                            turn.complete(recorder.response)
                        else:
                            recorder.feed(item)
                yield chunk
        except ProviderError as exc:
            # Headers are already on the wire; signal the failure in-band
            # the way OpenAI does instead of silently truncating the stream.
            yield _stream_error_event(exc.detail, exc.code, exc.status_code)
        finally:
            await stream.aclose()

    async def claude_responses(request_id: str, body: dict[str, Any], allow_tools: bool) -> Response:
        """`/v1/responses` on a Claude model: translated to the chat runtime
        and back (see airelay.chat_responses)."""
        refuse_tools_unless_allowed(body, allow_tools)
        chat, turn_items, ignored = request_to_chat(body)
        ignored += claude_ignored_tool_options(chat)
        result, turn = await claude_chat(request_id, chat, body.get("conversation"), turn_items)
        headers = _adaptation_headers(ignored)
        log_adaptation(request_id, ignored, reason=CLAUDE_ADAPTATION_REASON)
        if isinstance(result, dict):
            response = chat_to_response(result, body)
            if turn:
                turn.complete(response)
            return logged_json(request_id, response, headers=headers)

        async def events() -> AsyncIterator[bytes]:
            converter = ChatStreamToResponses(body, body.get("model") or "")
            first, stream = result
            for event in converter.start():
                yield chat_responses_sse(event)
            try:
                async for chunk in _with_first(first, stream):
                    for item in chat_chunks(chunk):
                        if item == "[DONE]":
                            closing = converter.finish()
                            if turn:  # recorded before the client sees the end of the stream
                                turn.complete(converter.response)
                            for event in closing:
                                yield chat_responses_sse(event)
                        else:
                            for event in converter.feed(item):
                                yield chat_responses_sse(event)
            except ProviderError as exc:
                for event in converter.fail(exc.status_code, exc.detail):
                    yield chat_responses_sse(event)
            finally:
                await stream.aclose()

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    def begin_turn(conversation_id: str | None, payload: dict[str, Any]) -> ConversationTurn | None:
        """Attach the conversation history, then inline local files into the
        final upstream input (the history keeps file references)."""
        try:
            turn = ConversationTurn.begin(store, conversation_id, payload)
        except KeyError as exc:
            raise _ApiError(404, f"Unknown conversation id `{conversation_id}`.") from exc
        try:
            expand_local_files(payload, store)
        except UnknownLocalFileError as exc:
            if turn is None:
                raise
            # The turn's own files were checked during translation, so the
            # missing one is in the stored history.
            raise TranslationError(
                f"Conversation `{conversation_id}` references the deleted file `{exc.file_id}` and cannot "
                "continue; start a new conversation."
            ) from exc
        return turn

    async def collect_response_with_retry(
        payload: dict[str, Any],
        request_id: str,
        conversation_id: str | None,
        request: Request,
    ) -> dict[str, Any]:
        return await retry_call(
            lambda: backend.collect_response(payload, request_id, conversation_id),
            policy=retry_policy,
            request_id=request_id,
            traffic=traffic,
            should_abort=request.is_disconnected,
        )

    async def open_upstream_stream(
        payload: dict[str, Any], request_id: str, conversation_id: str | None
    ) -> tuple[AsyncIterator[SSEEvent], SSEEvent]:
        """Opens the upstream stream and awaits its first event — the
        pre-header phase of the streaming lanes. A stream that is dead on
        arrival (no events, or a failure event first) raises the structured
        error here, while no byte has reached the client, which is also
        what makes this phase safely retriable."""
        upstream_events = backend.stream_response_events(payload, request_id, conversation_id)
        try:
            first_event = await anext(upstream_events)
        except StopAsyncIteration:
            raise BackendError(502, "Upstream stream ended without a response payload.")
        if first_event.event in FAILURE_EVENTS:
            await upstream_events.aclose()
            raise failure_backend_error(stream_error_object(_parse_event_data(first_event)))
        return upstream_events, first_event

    async def open_upstream_stream_with_retry(
        payload: dict[str, Any],
        request_id: str,
        conversation_id: str | None,
        request: Request,
    ) -> tuple[AsyncIterator[SSEEvent], SSEEvent]:
        return await retry_call(
            lambda: open_upstream_stream(payload, request_id, conversation_id),
            policy=retry_policy,
            request_id=request_id,
            traffic=traffic,
            should_abort=request.is_disconnected,
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        await providers.warm_status_probes()
        provider_statuses = providers.provider_statuses()
        body = render_home(
            relay_token_ready=bool(settings.resolve_bearer_token()),
            require_bearer_auth=settings.require_bearer_auth,
            host=settings.host,
            port=settings.port,
            client_base_url=settings.client_base_url(),
            bearer_token_file=str(settings.bearer_token_file),
            security=protector.summary(),
            providers=provider_statuses,
        ).encode("utf-8")
        return logged_body(request_id, body, "text/html; charset=utf-8")

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        payload = {"ok": True, "app_name": APP_NAME, "version": __version__}
        return logged_json(request_id, payload, loggable=False)

    @app.get("/v1/models")
    @app.get("/no-tools/v1/models")
    async def list_models(request: Request, refresh: bool = False) -> JSONResponse:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = await providers.list_models(request_id, refresh=refresh)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return logged_json(request_id, payload)

    @app.get("/v1/models/{model_id:path}")
    @app.get("/no-tools/v1/models/{model_id:path}")
    async def retrieve_model(model_id: str, request: Request) -> JSONResponse:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = await providers.retrieve_model(model_id, request_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return logged_json(request_id, payload)

    @app.get("/v1/subscription/status")
    @app.get("/v1/account/rate_limits")
    async def subscription_status(
        request: Request,
        raw: bool = False,
        provider: str = "openai",
        account: str | None = None,
        all_accounts: bool = False,
    ) -> JSONResponse:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        if provider == "claude":
            runtime = providers.claude_runtime
            if runtime is None:
                raise HTTPException(
                    status_code=501,
                    detail="The Claude runtime is disabled for this AIRelays process.",
                )
            try:
                if all_accounts:
                    payload = {"object": "subscription_status_list", "provider": "claude",
                               "accounts": await runtime.subscription_statuses(request_id)}
                else:
                    payload = await runtime.get_subscription_status(request_id, slug=account)
            except Exception as exc:  # noqa: BLE001
                raise _http_error(exc) from exc
            return logged_json(request_id, payload, loggable=False)
        if provider != "openai":
            raise HTTPException(
                status_code=501,
                detail="Subscription status is currently verified only for the OpenAI subscription runtime.",
            )
        if not settings.enable_openai_provider:
            raise HTTPException(
                status_code=501,
                detail="The OpenAI subscription runtime is disabled for this AIRelays process.",
            )
        try:
            backend.refresh_if_changed()
            # Always the list shape when the caller asked for all accounts —
            # folding a single account into the bare status shape turned its
            # probe failure into a whole-request 503, so a lone account with
            # an upstream-invalidated token had no way to report *why* it was
            # broken (the desktop showed "Ready" next to a blank meter).
            if all_accounts:
                entries = await backend.subscription_statuses(request_id)
                payload = {
                    "object": "subscription_status_list",
                    "accounts": [
                        {
                            "slug": entry["slug"],
                            "email": entry.get("email"),
                            **(
                                {
                                    "status": normalize_subscription_status_payload(
                                        entry["payload"], include_raw=raw,
                                        captured_at=entry.get("captured_at"),
                                        image_usage=entry.get("image_usage"),
                                    )
                                }
                                if "payload" in entry
                                else {"error": entry.get("error")}
                            ),
                        }
                        for entry in entries
                    ],
                }
            else:
                upstream_payload = (
                    await backend.get_subscription_status(request_id, slug=account)
                    if account is not None
                    else await backend.get_subscription_status(request_id)
                )
                payload = normalize_subscription_status_payload(
                    upstream_payload,
                    include_raw=raw,
                    captured_at=backend.subscription_captured_at(account)
                    if hasattr(backend, "subscription_captured_at") else None,
                    image_usage=backend.image_usage_snapshot(account)
                    if hasattr(backend, "image_usage_snapshot") else None,
                )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return logged_json(request_id, payload, loggable=False)

    @app.post("/v1/relay/accounts/refresh")
    async def refresh_accounts(request: Request, provider: str = "openai") -> JSONResponse:
        request_id = _request_id(request)
        # Clears usage-limit benches and re-probes so recovered accounts
        # return to rotation immediately, without waiting out an estimated
        # reset or restarting the relay.
        try:
            if provider == "claude" and providers.claude:
                accounts = await providers.claude.hard_refresh(request_id)
            elif provider == "openai":
                accounts = await backend.hard_refresh(request_id)
            else:
                raise HTTPException(501, "The requested provider is unavailable.")
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return logged_json(
            request_id, {"object": "accounts.refresh", "accounts": accounts}, loggable=False
        )

    def require_openai_accounts() -> None:
        if not settings.enable_openai_provider:
            raise HTTPException(
                status_code=501,
                detail="The OpenAI subscription runtime is disabled for this AIRelays process.",
            )

    @app.get("/v1/relay/accounts/resets")
    async def account_reset_credits(
        request: Request,
        account: str | None = None,
        raw: bool = False,
        force: bool = False,
    ) -> JSONResponse:
        """Usage-limit reset credits per OpenAI account: what Codex shows
        under "Redeem reset", read from the same subscription so the user
        need not open an OpenAI app to know a reset is waiting."""
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        require_openai_accounts()
        try:
            entries = await backend.reset_credits(request_id, account, force=force)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        accounts = []
        for entry in entries:
            item: dict[str, Any] = {"slug": entry["slug"], "email": entry.get("email")}
            if "details" in entry:
                item["resets"] = normalize_reset_credits_payload(
                    entry["details"],
                    usage_summary=entry.get("usage_summary"),
                    include_raw=raw,
                    captured_at=entry.get("captured_at"),
                )
            else:
                item["error"] = entry.get("error")
            accounts.append(item)
        payload = {
            "object": "accounts.resets",
            "available_count": sum(
                item["resets"]["available_count"] or 0 for item in accounts if "resets" in item
            ),
            "accounts": accounts,
        }
        return logged_json(request_id, payload, loggable=False)

    @app.post("/v1/relay/accounts/resets/redeem")
    async def redeem_account_reset(request: Request) -> JSONResponse:
        """Redeems one reset credit on the upstream for one account (the
        call Codex makes behind "Yes, use reset"), then re-reads that
        account's usage so a lifted limit returns it to rotation at once.
        The upstream's outcome code is reported verbatim; `reset_applied` is
        true only for `reset`."""
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        require_openai_accounts()
        payload = load_json(body) if body.strip() else {}
        if not isinstance(payload, dict):
            raise _ApiError(422, "The request body must be a JSON object.", code="invalid_body")
        account = payload.get("account")
        credit_id = payload.get("credit_id")
        redeem_request_id = payload.get("redeem_request_id")
        for name, value in (("account", account), ("credit_id", credit_id), ("redeem_request_id", redeem_request_id)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise _ApiError(422, f"`{name}` must be a non-empty string.", code="invalid_value", param=name)
        try:
            result = await backend.redeem_reset_credit(
                request_id,
                account.strip() if isinstance(account, str) else None,
                credit_id=credit_id.strip() if isinstance(credit_id, str) else None,
                redeem_request_id=redeem_request_id.strip() if isinstance(redeem_request_id, str) else None,
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        outcome = normalize_reset_redeem_payload(
            result["response"], credit_id=credit_id, redeem_request_id=result["redeem_request_id"]
        )
        response = {
            "object": "accounts.reset_redeem",
            "slug": result["slug"],
            "email": result.get("email"),
            **outcome,
            "status": normalize_subscription_status_payload(
                result["usage"], captured_at=result.get("usage_captured_at")
            )
            if isinstance(result.get("usage"), dict)
            else None,
        }
        return logged_json(request_id, response, loggable=False)

    @app.get("/v1/relay/status")
    async def relay_status(request: Request, activity_only: bool = False) -> JSONResponse:
        if activity_only:
            # Frequent tray polling must not scan storage or probe providers.
            # This uses the same authentication as the full status endpoint.
            return JSONResponse({"requests_total": request_counter["total"]})
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        await providers.warm_status_probes()
        provider_statuses = providers.provider_statuses()
        openai_status = provider_statuses.get("openai", {})
        auth_status = dict(openai_status)
        auth_status.pop("models_cache", None)
        enabled_provider_statuses = [
            status for status in provider_statuses.values() if isinstance(status, dict) and status.get("enabled")
        ]
        any_provider_ready = any(bool(status.get("ready_for_requests")) for status in enabled_provider_statuses)
        payload = {
            "object": "relay.status",
            "app_name": APP_NAME,
            "version": __version__,
            "requests_total": request_counter["total"],
            "ready": {
                "upstream_auth": any_provider_ready,
                "openai_upstream_auth": bool(openai_status.get("ready_for_requests")),
                "relay_token": (not settings.require_bearer_auth) or bool(settings.resolve_bearer_token()),
                "any_provider": any_provider_ready,
                "providers": {
                    name: bool(status.get("ready_for_requests"))
                    for name, status in provider_statuses.items()
                    if status.get("enabled")
                },
            },
            "auth": auth_status,
            "providers": provider_statuses,
            "relay": settings.summary(),
            "security": protector.diagnostics(getattr(request.state, "client_ip", None)),
            "storage": {
                **store.file_usage(),
                **store.conversation_usage(),
            },
            "supported_routes": supported_routes,
        }
        return logged_json(request_id, payload, loggable=False)

    @app.get("/v1/relay/logging")
    async def log_settings() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(traffic.status)
        except (OSError, ValueError, TypeError) as error:
            raise HTTPException(status_code=503, detail=f"Cannot read log retention: {error}") from error

    @app.put("/v1/relay/logging")
    async def configure_logs(changes: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(traffic.configure, changes)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(status_code=503, detail=f"Cannot save log retention: {error}") from error

    def _balance_payload() -> dict[str, Any]:
        return {
            "object": "relay.balance",
            "openai": settings.openai_balance,
            "claude": settings.claude_balance,
            "strategies": list(BALANCE_STRATEGIES),
            # The switch lives in this process; the config file and the
            # AIRELAYS_*_BALANCE variables still decide the next start.
            "runtime_only": True,
        }

    @app.get("/v1/relay/balance")
    async def balance_settings() -> dict[str, Any]:
        return _balance_payload()

    @app.put("/v1/relay/balance")
    async def configure_balance(changes: dict[str, Any]) -> dict[str, Any]:
        """Switches the account-routing strategy of the RUNNING relay. The
        pools read the strategy from settings on every selection, so the
        change applies to the next request without a restart."""
        if not isinstance(changes, dict) or not changes or set(changes) - {"openai", "claude"}:
            raise HTTPException(
                status_code=422,
                detail='Body must set "openai" and/or "claude" to one of: ' + ", ".join(BALANCE_STRATEGIES) + ".",
            )
        try:
            if "openai" in changes:
                settings.openai_balance = _normalized_balance(changes["openai"], "openai")
            if "claude" in changes:
                settings.claude_balance = _normalized_balance(changes["claude"], "claude")
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _balance_payload()

    @app.post("/v1/files")
    async def upload_file(request: Request, file: UploadFile = File(...),
                          purpose: str = Form("assistants")) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        if purpose not in FILE_PURPOSES:
            raise _ApiError(422, f"`purpose` must be one of: {', '.join(FILE_PURPOSES)}.")
        if any(key.startswith("expires_after") for key in (await request.form()).keys()):
            raise _ApiError(422, "`expires_after` is not supported; delete files with `DELETE /v1/files/{file_id}`.")
        content_type = file.content_type or "application/octet-stream"
        filename = file.filename or "upload.bin"
        chunk_size = 1024 * 1024
        total_bytes = 0
        digest = hashlib.sha256()
        temp_dir = settings.data_dir / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_handle = tempfile.NamedTemporaryFile(delete=False, dir=temp_dir)
        temp_path = Path(temp_handle.name)
        moved = False
        try:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Uploaded file exceeds the configured AIRelays upload limit "
                            f"of {settings.max_upload_bytes} bytes."
                        ),
                    )
                digest.update(chunk)
                temp_handle.write(chunk)
            temp_handle.flush()
            usage = store.file_usage()
            if usage["total_bytes"] + total_bytes > settings.max_total_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Stored uploads would exceed the configured AIRelays upload quota "
                        f"of {settings.max_total_upload_bytes} bytes."
                    ),
                )
            traffic.write(
                {
                    "request_id": request_id,
                    "phase": "inbound_request",
                    "method": request.method,
                    "path": request.url.path,
                    "query": dict(request.query_params),
                    "headers": dict(request.headers.items()),
                    "client_ip": getattr(request.state, "client_ip", None),
                    "body": {
                        "kind": "multipart_upload",
                        "filename": filename,
                        "purpose": purpose,
                        "content_type": content_type,
                        "bytes": total_bytes,
                        "sha256": digest.hexdigest(),
                    },
                }
            )
            # Windows cannot rename a file that is still open.
            temp_handle.close()
            record = store.create_file_from_path(
                filename=filename,
                purpose=purpose,
                content_type=content_type,
                storage_path=temp_path,
                size_bytes=total_bytes,
                sha256=digest.hexdigest(),
            )
            moved = True
            return logged_json(request_id, record)
        finally:
            temp_handle.close()
            await file.close()
            if not moved and temp_path.exists():
                temp_path.unlink()

    @app.get("/v1/files")
    async def list_files(request: Request, after: str | None = None, limit: int = 10_000,
                         order: str = "desc", purpose: str | None = None) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        if not 1 <= limit <= 10_000:
            raise _ApiError(422, "`limit` must be between 1 and 10000.")
        if order not in ("asc", "desc"):
            raise _ApiError(422, "`order` must be `asc` or `desc`.")
        try:
            files, has_more = store.list_files(after=after, limit=limit, order=order, purpose=purpose)
        except KeyError as exc:
            raise _ApiError(404, f"Unknown file id `{after}` in `after`.") from exc
        payload = {
            "object": "list",
            "data": files,
            "first_id": files[0]["id"] if files else None,
            "last_id": files[-1]["id"] if files else None,
            "has_more": has_more,
        }
        return logged_json(request_id, payload)

    @app.get("/v1/files/{file_id}")
    async def get_file(file_id: str, request: Request) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = store.get_file(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown file id `{file_id}`.") from exc
        return logged_json(request_id, payload)

    @app.get("/v1/files/{file_id}/content")
    async def get_file_content(file_id: str, request: Request) -> Response:
        require_text_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload, raw = store.get_file_bytes(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown file id `{file_id}`.") from exc
        return logged_body(request_id, raw, payload["content_type"])

    @app.delete("/v1/files/{file_id}")
    async def delete_file(file_id: str, request: Request) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        deleted = store.delete_file(file_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Unknown file id `{file_id}`.")
        return logged_json(request_id, {"id": file_id, "object": "file", "deleted": True})

    @app.post("/v1/conversations")
    async def create_conversation(request: Request) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        payload = load_json(body)
        try:
            metadata = validated_metadata(payload.get("metadata"))
            items = normalize_conversation_items(payload.get("items"), store)
        except InvalidRequestError as exc:
            raise _http_error(exc) from exc
        conversation = store.create_conversation(metadata=metadata, items=items)
        return logged_json(request_id, conversation)

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.") from exc
        return logged_json(request_id, payload)

    @app.post("/v1/conversations/{conversation_id}")
    async def update_conversation(conversation_id: str, request: Request) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        payload = load_json(body)
        try:
            conversation = store.update_conversation(conversation_id, metadata=validated_metadata(payload.get("metadata")))
        except InvalidRequestError as exc:
            raise _http_error(exc) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.") from exc
        return logged_json(request_id, conversation)

    @app.delete("/v1/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request) -> JSONResponse:
        require_text_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        deleted = store.delete_conversation(conversation_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.")
        return logged_json(
            request_id, {"id": conversation_id, "object": "conversation.deleted", "deleted": True}
        )

    async def _responses_route(request: Request, allow_tools: bool) -> Response:
        request_id = _request_id(request)
        body_bytes = await request.body()
        await log_inbound(request_id, request, body_bytes)
        try:
            body = load_json(body_bytes)
            resolved = await resolve_request_model(body.get("model"), request_id)
            if resolved is not None and resolved.provider == "claude":
                return await claude_responses(request_id, body, allow_tools)
            ignored_parameters = strip_unsupported_response_parameters(body)
            payload, wants_stream, conversation_id = prepare_response_request(body, store, allow_tools)
            log_adaptation(request_id, ignored_parameters)
            response_headers = _adaptation_headers(ignored_parameters)
            turn = begin_turn(conversation_id, payload)
            if not wants_stream:
                response_payload = await collect_response_with_retry(
                    payload, request_id, conversation_id, request
                )
                if turn:
                    turn.complete(response_payload)
                return logged_json(request_id, response_payload, headers=response_headers)

            # Same eager first-event contact as the translated lanes: dead-
            # on-arrival streams become real status codes (with automatic
            # retry) instead of empty 200 streams. Events are still passed
            # through verbatim once the stream has started — including any
            # later `response.failed`, per the Responses API contract.
            upstream_events, first_event = await open_upstream_stream_with_retry(
                payload, request_id, conversation_id, request
            )

            async def event_stream() -> AsyncIterator[bytes]:
                async def upstream_iter() -> AsyncIterator[SSEEvent]:
                    try:
                        yield first_event
                        async for upstream_event in upstream_events:
                            yield upstream_event
                    except BackendError as error:
                        yield _synthetic_failure_event(error)

                async for event in upstream_iter():
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        parsed = None
                    if turn:
                        turn.observe(event.event, parsed)
                    if isinstance(parsed, dict):
                        response_obj = parsed.get("response")
                        if isinstance(response_obj, dict):
                            if event.event == "response.completed":
                                traffic.write(
                                    {
                                        "request_id": request_id,
                                        "phase": "outbound_usage",
                                        "response_id": response_obj.get("id"),
                                        "model": response_obj.get("model"),
                                        "usage": response_obj.get("usage"),
                                    }
                                )
                    chunk = encode_sse(event)
                    traffic.write(
                        {
                            "request_id": request_id,
                            "phase": "outbound_stream_chunk",
                            "body": snapshot_body("text/event-stream", chunk),
                        }
                    )
                    yield chunk

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/v1/responses")
    async def responses_route(request: Request) -> Response:
        return await _responses_route(request, allow_tools=True)

    @app.post("/no-tools/v1/responses")
    async def responses_route_no_tools(request: Request) -> Response:
        return await _responses_route(request, allow_tools=False)

    async def _chat_route(request: Request, allow_tools: bool) -> Response:
        request_id = _request_id(request)
        body_bytes = await request.body()
        await log_inbound(request_id, request, body_bytes)
        try:
            body = load_json(body_bytes)
            resolved = await resolve_request_model(body.get("model"), request_id)
            if resolved is not None:
                if resolved.provider == "claude":
                    claude_runtime()
                    refuse_tools_unless_allowed(body, allow_tools)
                    conversation_id = conversation_id_of(body.pop("conversation", None))
                    ignored_parameters = strip_unsupported_response_parameters(body)
                    ignored_parameters += strip_unknown_parameters(body, CLAUDE_CHAT_PARAMETERS)
                    ignored_parameters += claude_ignored_tool_options(body)
                    reject_unhonored_parameters(body)
                    turn_items = messages_to_items(body.get("messages") or []) if conversation_id else []
                    result, turn = await claude_chat(request_id, body, conversation_id, turn_items)
                    headers = _adaptation_headers(ignored_parameters)
                    log_adaptation(request_id, ignored_parameters, reason=CLAUDE_ADAPTATION_REASON)
                    if isinstance(result, dict):
                        if turn:
                            turn.complete(chat_to_response(result, {}))
                        return logged_json(request_id, result, headers=headers)
                    return StreamingResponse(claude_chat_stream(request_id, result, turn),
                                             media_type="text/event-stream", headers=headers)
            ignored_parameters = strip_unsupported_response_parameters(body)
            responses_shape_on_chat_route = is_responses_shape(body)
            if not responses_shape_on_chat_route:
                ignored_parameters += strip_unknown_parameters(body, CHAT_PARAMETERS)
                reject_unhonored_parameters(body)
            payload, wants_stream, conversation_id = chat_route_request_to_responses(body, store, allow_tools)
            legacy_functions = not responses_shape_on_chat_route and uses_legacy_functions(body)
            log_adaptation(request_id, ignored_parameters)
            if responses_shape_on_chat_route:
                traffic.write(
                    {
                        "request_id": request_id,
                        "phase": "compatibility_adaptation",
                        "reason": CHAT_ROUTE_RESPONSES_SHAPE_REASON,
                    }
                )
            response_headers = _adaptation_headers(ignored_parameters)
            turn = begin_turn(conversation_id, payload)
            if not wants_stream:
                response_payload = await collect_response_with_retry(
                    payload, request_id, conversation_id, request
                )
                if turn:
                    turn.complete(response_payload)
                chat_payload = responses_to_chat_completion(response_payload, legacy_functions=legacy_functions)
                return logged_json(request_id, chat_payload, headers=response_headers)

            # Contact the upstream before committing SSE headers: an upstream
            # rejection (e.g. an invalid reasoning effort) raised inside the
            # response generator can only surface as an empty 200 stream, so
            # the first upstream event is awaited eagerly and errors become
            # real status codes. The same applies to a stream that dies (or
            # fails) before its first event — and since no byte has reached
            # the client yet, this phase is where automatic retry happens.
            upstream_events, first_event = await open_upstream_stream_with_retry(
                payload, request_id, conversation_id, request
            )

            async def event_stream() -> AsyncIterator[bytes]:
                response_id = f"chatcmpl_{uuid.uuid4().hex}"
                created_at = int(time.time())
                model = body.get("model", "unknown")
                sent_role = False
                tool_index = 0
                saw_tool_calls = False
                usage_requested = stream_include_usage(body)
                latest_usage: dict[str, Any] | None = None
                finished = False

                async def upstream_iter() -> AsyncIterator[SSEEvent]:
                    try:
                        yield first_event
                        async for upstream_event in upstream_events:
                            yield upstream_event
                    except BackendError as error:
                        yield _synthetic_failure_event(error)

                async for event in upstream_iter():
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        continue
                    if turn:
                        turn.observe(event.event, parsed)
                    if event.event == "response.created":
                        response_obj = parsed.get("response") or {}
                        response_id = response_obj.get("id", response_id)
                        created_at = created_timestamp(response_obj.get("created_at"), created_at)
                        model = response_obj.get("model", model)
                        continue
                    if event.event in ("response.output_text.delta", "response.refusal.delta"):
                        field = "refusal" if event.event == "response.refusal.delta" else "content"
                        delta_payload = {field: parsed.get("delta", "")}
                        if not sent_role:
                            delta_payload = {"role": "assistant", field: parsed.get("delta", "")}
                            sent_role = True
                        chunk = chat_completion_chunk(response_id, created_at, model, delta_payload,
                                                      include_usage=usage_requested)
                        encoded = f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        yield encoded
                        continue
                    if event.event == "response.output_item.done":
                        item = parsed.get("item") or {}
                        tool_call = response_item_to_chat_tool_call(item)
                        if legacy_functions and (saw_tool_calls or (tool_call or {}).get("type") != "function"):
                            tool_call = None  # the deprecated interface carries one function call
                        if tool_call is not None:
                            saw_tool_calls = True
                            if legacy_functions:
                                delta_payload: dict[str, Any] = {"function_call": dict(tool_call["function"])}
                            else:
                                delta_payload = {"tool_calls": [{"index": tool_index, **tool_call}]}
                            if not sent_role:
                                delta_payload["role"] = "assistant"
                                sent_role = True
                            tool_index += 1
                            chunk = chat_completion_chunk(response_id, created_at, model, delta_payload,
                                                          include_usage=usage_requested)
                            encoded = f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                            yield encoded
                        continue
                    if event.event in FAILURE_EVENTS:
                        # Headers are already on the wire; signal the failure
                        # in-band (no [DONE]) instead of silently truncating.
                        yield _stream_failure_event(parsed)
                        await upstream_events.aclose()
                        return
                    if event.event in ("response.completed", "response.incomplete"):
                        response_obj = parsed.get("response") or {}
                        latest_usage = response_obj.get("usage")
                        traffic.write(
                            {
                                "request_id": request_id,
                                "phase": "outbound_usage",
                                "response_id": response_obj.get("id"),
                                "model": response_obj.get("model"),
                                "usage": latest_usage,
                            }
                        )
                        if event.event == "response.incomplete":
                            # Stopped early: token limit ("length") or the
                            # upstream's content filter ("content_filter").
                            finish_reason = incomplete_finish_reason(response_obj)
                        elif saw_tool_calls:
                            finish_reason = "function_call" if legacy_functions else "tool_calls"
                        else:
                            finish_reason = "stop"
                        final_chunk = chat_completion_chunk(
                            response_id,
                            created_at,
                            model,
                            {},
                            finish_reason=finish_reason,
                            include_usage=usage_requested,
                        )
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        usage = completion_usage_from_responses(latest_usage)
                        if usage_requested and usage is not None:
                            closing = usage_chunk("chat.completion.chunk", response_id, created_at, model, usage)
                            yield f"data: {json.dumps(closing, ensure_ascii=True)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        finished = True
                if not finished:
                    # The upstream died without any terminal event.
                    yield _INCOMPLETE_STREAM_EVENT

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/v1/chat/completions")
    async def chat_route(request: Request) -> Response:
        return await _chat_route(request, allow_tools=True)

    @app.post("/no-tools/v1/chat/completions")
    async def chat_route_no_tools(request: Request) -> Response:
        return await _chat_route(request, allow_tools=False)

    async def _completions_route(request: Request) -> Response:
        request_id = _request_id(request)
        body_bytes = await request.body()
        await log_inbound(request_id, request, body_bytes)
        try:
            body = load_json(body_bytes)
            resolved = await resolve_request_model(body.get("model"), request_id)
            if resolved is not None:
                if resolved.provider == "claude":
                    runtime = claude_runtime()
                    ignored_parameters = strip_unsupported_response_parameters(body)
                    ignored_parameters += strip_unknown_parameters(body, CLAUDE_COMPLETION_PARAMETERS)
                    reject_unhonored_parameters(body)
                    log_adaptation(request_id, ignored_parameters, reason=CLAUDE_ADAPTATION_REASON)
                    claude_headers = _adaptation_headers(ignored_parameters)
                    if not body.get("stream"):
                        payload = await runtime.create_completion(body, request_id)
                        usage = payload.get("usage")
                        if usage is not None:
                            traffic.write(
                                {
                                    "request_id": request_id,
                                    "phase": "outbound_usage",
                                    "provider": "claude",
                                    "model": payload.get("model"),
                                    "usage": usage,
                                }
                            )
                        return logged_json(request_id, payload, headers=claude_headers)

                    # Same eager-validation contract as the chat route.
                    claude_stream = runtime.stream_completion(body, request_id)
                    first_chunk = await _first_chunk(claude_stream)

                    async def claude_event_stream() -> AsyncIterator[bytes]:
                        try:
                            async for chunk in _with_first(first_chunk, claude_stream):
                                traffic.write(
                                    {
                                        "request_id": request_id,
                                        "phase": "outbound_stream_chunk",
                                        "provider": "claude",
                                        "body": snapshot_body("text/event-stream", chunk),
                                    }
                                )
                                yield chunk
                        except ProviderError as exc:
                            yield _stream_error_event(exc.detail, exc.code, exc.status_code)
                        finally:
                            await claude_stream.aclose()

                    return StreamingResponse(
                        claude_event_stream(),
                        media_type="text/event-stream",
                        headers=claude_headers,
                    )
            ignored_parameters = strip_unsupported_response_parameters(body)
            ignored_parameters += strip_unknown_parameters(body, COMPLETIONS_PARAMETERS)
            reject_unhonored_parameters(body)
            payload, wants_stream, conversation_id = completions_to_responses(body)
            log_adaptation(request_id, ignored_parameters)
            response_headers = _adaptation_headers(ignored_parameters)
            turn = begin_turn(conversation_id, payload)
            if not wants_stream:
                response_payload = await collect_response_with_retry(
                    payload, request_id, conversation_id, request
                )
                if turn:
                    turn.complete(response_payload)
                return logged_json(
                    request_id,
                    responses_to_completion(response_payload),
                    headers=response_headers,
                )

            # Same eager first-event contact as the chat route, for real
            # pre-stream status codes and pre-header automatic retry.
            upstream_events, first_event = await open_upstream_stream_with_retry(
                payload, request_id, conversation_id, request
            )

            async def event_stream() -> AsyncIterator[bytes]:
                response_id = f"cmpl_{uuid.uuid4().hex}"
                created_at = int(time.time())
                model = body.get("model", "unknown")
                usage_requested = stream_include_usage(body)
                refused = False
                finished = False

                async def upstream_iter() -> AsyncIterator[SSEEvent]:
                    try:
                        yield first_event
                        async for upstream_event in upstream_events:
                            yield upstream_event
                    except BackendError as error:
                        yield _synthetic_failure_event(error)

                async for event in upstream_iter():
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        continue
                    if turn:
                        turn.observe(event.event, parsed)
                    if event.event == "response.created":
                        response_obj = parsed.get("response") or {}
                        response_id = response_obj.get("id", response_id)
                        created_at = created_timestamp(response_obj.get("created_at"), created_at)
                        model = response_obj.get("model", model)
                        continue
                    if event.event == "response.refusal.delta":
                        # Completions has no refusal field; the finish
                        # reason says the content was withheld.
                        refused = True
                        continue
                    if event.event == "response.output_text.delta":
                        chunk = completion_chunk(
                            response_id,
                            created_at,
                            model,
                            parsed.get("delta", ""),
                            finish_reason=None,
                            include_usage=usage_requested,
                        )
                        yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        continue
                    if event.event in FAILURE_EVENTS:
                        # Headers are already on the wire; signal the failure
                        # in-band (no [DONE]) instead of silently truncating.
                        yield _stream_failure_event(parsed)
                        await upstream_events.aclose()
                        return
                    if event.event in ("response.completed", "response.incomplete"):
                        response_obj = parsed.get("response") or {}
                        traffic.write(
                            {
                                "request_id": request_id,
                                "phase": "outbound_usage",
                                "response_id": response_obj.get("id"),
                                "model": response_obj.get("model"),
                                "usage": response_obj.get("usage"),
                            }
                        )
                        final_chunk = completion_chunk(
                            response_id,
                            created_at,
                            model,
                            "",
                            finish_reason="content_filter" if refused
                            else incomplete_finish_reason(response_obj)
                            if event.event == "response.incomplete" else "stop",
                            include_usage=usage_requested,
                        )
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        usage = completion_usage_from_responses(response_obj.get("usage"))
                        if usage_requested and usage is not None:
                            closing = usage_chunk("text_completion", response_id, created_at, model, usage)
                            yield f"data: {json.dumps(closing, ensure_ascii=True)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        finished = True
                if not finished:
                    # The upstream died without any terminal event.
                    yield _INCOMPLETE_STREAM_EVENT

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/v1/completions")
    async def completions_route(request: Request) -> Response:
        return await _completions_route(request)

    @app.post("/no-tools/v1/completions")
    async def completions_route_no_tools(request: Request) -> Response:
        return await _completions_route(request)

    @app.post("/v1/images/generations")
    async def images_generations(request: Request) -> Response:
        request_id = _request_id(request)
        body_bytes = await request.body()
        try:
            parsed = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            parsed = None
        await log_inbound(request_id, request, body_bytes, snapshot=None if parsed is None else {
            "kind": "json", "bytes": len(body_bytes), "json": summarize_image_payload(parsed)})
        try:
            require_openai_runtime()
            prepared = prepare_image_generation(load_json(body_bytes))
            return await run_image_request(request, request_id, prepared)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/v1/images/edits")
    async def images_edits(request: Request) -> Response:
        """OpenAI's multipart form (`image` files, text fields), or a JSON
        body whose `image` entries are data URLs or local file ids."""
        request_id = _request_id(request)
        content_type = (request.headers.get("content-type") or "").lower()
        multipart = content_type.startswith("multipart/form-data")
        body_bytes = await request.body()
        if multipart:
            snapshot: dict[str, Any] | None = {"kind": "multipart", "bytes": len(body_bytes),
                                               "note": "Image uploads are not logged."}
        else:
            try:
                parsed = json.loads(body_bytes) if body_bytes else {}
            except json.JSONDecodeError:
                parsed = None
            snapshot = None if parsed is None else {
                "kind": "json", "bytes": len(body_bytes), "json": summarize_image_payload(parsed)}
        await log_inbound(request_id, request, body_bytes, snapshot=snapshot)
        try:
            require_openai_runtime()
            images: list[EditImage] = []
            has_mask = False
            if multipart:
                fields: dict[str, str] = {}
                form = await request.form()
                for name, value in form.multi_items():
                    key = "image" if name == "image[]" else name
                    if isinstance(value, StarletteUploadFile):
                        if key == "image":
                            raw = await value.read()
                            images.append(edit_image_from_bytes(
                                raw, what=f"image `{value.filename or len(images) + 1}`"))
                        elif key == "mask":
                            has_mask = True
                        else:
                            fields[key] = value.filename or "file"
                    elif key == "image":
                        images.append(edit_image_from_reference(str(value)))
                    elif key == "mask":
                        has_mask = has_mask or bool(str(value).strip())
                    else:
                        fields[key] = str(value)
                body = coerce_image_form(fields)
            else:
                body = load_json(body_bytes)
                references = body.pop("image", None)
                has_mask = body.pop("mask", None) not in (None, "", [], {})
                if isinstance(references, list):
                    images = [edit_image_from_reference(reference) for reference in references]
                elif references is not None:
                    images = [edit_image_from_reference(references)]
            prepared = prepare_image_edit(body, images, has_mask=has_mask)
            return await run_image_request(request, request_id, prepared)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.api_route("/v1/conversations/{conversation_id}/items", methods=["GET", "POST"])
    @app.api_route("/v1/conversations/{conversation_id}/items/{item_id}", methods=["GET", "DELETE"])
    @app.post("/v1/embeddings")
    @app.post("/v1/images/{operation}")
    @app.post("/v1/audio/{operation}")
    @app.post("/v1/realtime/sessions")
    async def unsupported_route(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        payload = error_body(
            "This subscription-backed server does not expose a verified upstream "
            "implementation for this route yet.",
            status_code=501, type_="unsupported_error",
        )
        return logged_json(request_id, payload, status_code=501, headers=NOT_RETRYABLE)

    return app
