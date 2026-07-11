from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from airelay import __version__
from airelay.accounts import build_pool
from airelay.auth import AuthManager, AuthenticationError
from airelay.backend import BackendError, ChatGptCodexBackend, SSEEvent, encode_sse
from airelay.config import APP_NAME, Settings
from airelay.html import render_home
from airelay.providers import ProviderError, ProviderRegistry
from airelay.security import EndpointProtector
from airelay.store import AppStore
from airelay.traffic import TrafficLogger, snapshot_body
from airelay.transforms import (
    completion_chunk,
    completions_to_responses,
    strip_unsupported_response_parameters,
    TranslationError,
    chat_completion_chunk,
    chat_completions_to_responses,
    normalize_subscription_status_payload,
    prepare_response_request,
    responses_to_completion,
    responses_to_chat_completion,
)


# The Claude CLI exposes no sampling controls, so the sampling parameters
# standard OpenAI SDKs send by default get the same documented treatment as
# on the OpenAI runtime: stripped and disclosed, never a hard failure.
CLAUDE_ADAPTATION_REASON = (
    "The Claude runtime's local CLI has no sampling controls, "
    "so the compatibility layer omitted these parameters."
)

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


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, BackendError):
        if exc.status_code == 401:
            return AuthenticationError(
                "Upstream ChatGPT login is missing or expired. Run `airelays login` first.",
                code="upstream_auth_rejected",
            )
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    if isinstance(exc, ProviderError):
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    if isinstance(exc, AuthenticationError):
        return exc
    if isinstance(exc, TranslationError):
        return HTTPException(status_code=422, detail=str(exc))
    raise HTTPException(status_code=500, detail=str(exc))


def _file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_directories()
    settings.validate_provider_guardrails()
    traffic = TrafficLogger(settings.logs_dir)
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
    supported_routes = [
        "/v1/models",
        "/v1/subscription/status",
        "/v1/account/rate_limits",
        "/v1/relay/status",
        "/v1/relay/accounts/refresh",
        "/v1/completions",
        "/v1/responses",
        "/v1/chat/completions",
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
        yield
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
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
        payload = {
            "error": {
                "message": message,
                "type": "authentication_error",
                "code": code,
            }
        }
        return 503, payload, headers

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
        request_id = _request_id(request)
        status_code, payload, headers = authentication_error_payload(exc)
        return logged_json(request_id, payload, status_code=status_code, headers=headers)

    @app.middleware("http")
    async def guard_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = _request_id(request)
        lease, rejection = protector.acquire(request_id, request)
        request.state.client_ip = lease.client_ip
        if rejection is not None:
            return rejection
        try:
            return await call_next(request)
        finally:
            protector.release(lease)

    # Count of real (non-monitoring) requests served by this process. The
    # desktop reads it from /v1/relay/status to blink the tray on activity.
    request_counter = {"total": 0}

    async def log_inbound(request_id: str, request: Request, body: bytes) -> None:
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
                "body": snapshot_body(request.headers.get("content-type"), body),
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
        return {
            "x-airelays-ignored-parameters": ",".join(ignored_parameters),
        }

    def log_adaptation(
        request_id: str,
        ignored_parameters: list[str],
        reason: str = (
            "The ChatGPT subscription backend rejects these OpenAI sampling "
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

    def require_openai_runtime() -> None:
        if not settings.enable_openai_provider:
            raise HTTPException(
                status_code=501,
                detail="This route is currently available only when the OpenAI runtime is enabled.",
            )

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
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
    async def list_models(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = await providers.list_models(request_id)
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
                payload = await runtime.get_subscription_status(request_id)
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
            if all_accounts and backend.size > 1:
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
                                        entry["payload"], include_raw=raw
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
                )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return logged_json(request_id, payload, loggable=False)

    @app.post("/v1/relay/accounts/refresh")
    async def refresh_accounts(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        # Clears usage-limit benches and re-probes so recovered accounts
        # return to rotation immediately, without waiting out an estimated
        # reset or restarting the relay.
        try:
            accounts = await backend.hard_refresh(request_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return logged_json(
            request_id, {"object": "accounts.refresh", "accounts": accounts}, loggable=False
        )

    @app.get("/v1/relay/status")
    async def relay_status(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
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

    @app.post("/v1/files")
    async def upload_file(request: Request, file: UploadFile = File(...), purpose: str = "assistants") -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
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
    async def list_files(request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        payload = {"object": "list", "data": store.list_files()}
        return logged_json(request_id, payload)

    @app.get("/v1/files/{file_id}")
    async def get_file(file_id: str, request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = store.get_file(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown file id `{file_id}`.") from exc
        return logged_json(request_id, payload)

    @app.get("/v1/files/{file_id}/content")
    async def get_file_content(file_id: str, request: Request) -> Response:
        require_openai_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload, raw = store.get_file_bytes(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown file id `{file_id}`.") from exc
        return logged_body(request_id, raw, payload["content_type"])

    @app.delete("/v1/files/{file_id}")
    async def delete_file(file_id: str, request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        deleted = store.delete_file(file_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Unknown file id `{file_id}`.")
        return logged_json(request_id, {"id": file_id, "object": "file", "deleted": True})

    @app.post("/v1/conversations")
    async def create_conversation(request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        payload = load_json(body)
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        seed_items = payload.get("items") if isinstance(payload, dict) else None
        conversation = store.create_conversation(metadata=metadata, seed_items=seed_items)
        return logged_json(request_id, conversation)

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        try:
            payload = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.") from exc
        return logged_json(request_id, payload)

    @app.post("/v1/conversations/{conversation_id}")
    async def update_conversation(conversation_id: str, request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        payload = load_json(body)
        try:
            conversation = store.update_conversation(conversation_id, metadata=payload.get("metadata"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.") from exc
        return logged_json(request_id, conversation)

    @app.delete("/v1/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request) -> JSONResponse:
        require_openai_runtime()
        request_id = _request_id(request)
        await log_inbound(request_id, request, b"")
        deleted = store.delete_conversation(conversation_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.")
        return logged_json(
            request_id, {"id": conversation_id, "object": "conversation", "deleted": True}
        )

    async def _responses_route(request: Request, allow_tools: bool) -> Response:
        request_id = _request_id(request)
        body_bytes = await request.body()
        await log_inbound(request_id, request, body_bytes)
        try:
            body = load_json(body_bytes)
            model = body.get("model")
            if isinstance(model, str):
                resolved = providers.resolve_model(model)
                traffic.write(
                    {
                        "request_id": request_id,
                        "phase": "provider_resolution",
                        "provider": resolved.provider,
                        "model": resolved.public_id,
                    }
                )
                if resolved.provider != "openai":
                    raise ProviderError(
                        422,
                        "The Claude runtime supports `/v1/chat/completions` and `/v1/completions` only.",
                        code="unsupported_for_provider",
                    )
            payload, wants_stream, conversation_id = prepare_response_request(body, store, allow_tools)
            ignored_parameters = strip_unsupported_response_parameters(payload)
            log_adaptation(request_id, ignored_parameters)
            response_headers = _adaptation_headers(ignored_parameters)
            if conversation_id:
                conversation = store.get_conversation(conversation_id)
                if conversation["seed_items"] and not conversation["latest_response_id"]:
                    payload["input"] = conversation["seed_items"] + payload["input"]
            else:
                conversation = None
            if not wants_stream:
                response_payload = await backend.collect_response(payload, request_id, conversation_id)
                if conversation_id:
                    store.touch_conversation(conversation_id, response_payload.get("id"))
                return logged_json(request_id, response_payload, headers=response_headers)

            async def event_stream() -> AsyncIterator[bytes]:
                latest_response_id: str | None = None
                async for event in backend.stream_response_events(payload, request_id, conversation_id):
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        response_obj = parsed.get("response")
                        if isinstance(response_obj, dict):
                            latest_response_id = response_obj.get("id")
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
                if conversation_id:
                    store.touch_conversation(conversation_id, latest_response_id)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{conversation_id}`.") from exc
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
            model = body.get("model")
            if isinstance(model, str):
                resolved = providers.resolve_model(model)
                traffic.write(
                    {
                        "request_id": request_id,
                        "phase": "provider_resolution",
                        "provider": resolved.provider,
                        "model": resolved.public_id,
                    }
                )
                if resolved.provider == "claude":
                    claude_runtime = providers.claude
                    if claude_runtime is None:
                        raise ProviderError(
                            503,
                            "The Claude runtime is not available in this AIRelays process.",
                            code="provider_unavailable",
                        )
                    ignored_parameters = strip_unsupported_response_parameters(body)
                    log_adaptation(request_id, ignored_parameters, reason=CLAUDE_ADAPTATION_REASON)
                    claude_headers = _adaptation_headers(ignored_parameters)
                    if not body.get("stream"):
                        payload = await claude_runtime.create_chat_completion(body, request_id)
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

                    async def claude_event_stream() -> AsyncIterator[bytes]:
                        async for chunk in claude_runtime.stream_chat_completion(body, request_id):
                            traffic.write(
                                {
                                    "request_id": request_id,
                                    "phase": "outbound_stream_chunk",
                                    "provider": "claude",
                                    "body": snapshot_body("text/event-stream", chunk),
                                }
                            )
                            yield chunk

                    return StreamingResponse(
                        claude_event_stream(),
                        media_type="text/event-stream",
                        headers=claude_headers,
                    )
            payload, wants_stream, conversation_id = chat_completions_to_responses(body, store, allow_tools)
            ignored_parameters = strip_unsupported_response_parameters(payload)
            log_adaptation(request_id, ignored_parameters)
            response_headers = _adaptation_headers(ignored_parameters)
            if conversation_id:
                conversation = store.get_conversation(conversation_id)
                if conversation["seed_items"] and not conversation["latest_response_id"]:
                    payload["input"] = conversation["seed_items"] + payload["input"]
            else:
                conversation = None
            if not wants_stream:
                response_payload = await backend.collect_response(payload, request_id, conversation_id)
                if conversation_id:
                    store.touch_conversation(conversation_id, response_payload.get("id"))
                chat_payload = responses_to_chat_completion(response_payload)
                return logged_json(request_id, chat_payload, headers=response_headers)

            async def event_stream() -> AsyncIterator[bytes]:
                response_id = f"chatcmpl_{uuid.uuid4().hex}"
                created_at = int(time.time())
                model = body.get("model", "unknown")
                sent_role = False
                tool_index = 0
                saw_tool_calls = False
                usage_requested = bool((body.get("stream_options") or {}).get("include_usage"))
                latest_response_id: str | None = None
                latest_usage: dict[str, Any] | None = None
                async for event in backend.stream_response_events(payload, request_id, conversation_id):
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        continue
                    if event.event == "response.created":
                        response_obj = parsed.get("response") or {}
                        response_id = response_obj.get("id", response_id)
                        created_at = response_obj.get("created_at", created_at)
                        model = response_obj.get("model", model)
                        continue
                    if event.event == "response.output_text.delta":
                        delta_payload = {"content": parsed.get("delta", "")}
                        if not sent_role:
                            delta_payload = {"role": "assistant", "content": parsed.get("delta", "")}
                            sent_role = True
                        chunk = chat_completion_chunk(response_id, created_at, model, delta_payload)
                        encoded = f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        yield encoded
                        continue
                    if event.event == "response.output_item.done":
                        item = parsed.get("item") or {}
                        if item.get("type") == "function_call":
                            saw_tool_calls = True
                            delta_payload = {
                                "tool_calls": [
                                    {
                                        "index": tool_index,
                                        "id": item.get("call_id"),
                                        "type": "function",
                                        "function": {
                                            "name": item.get("name"),
                                            "arguments": item.get("arguments", "{}"),
                                        },
                                    }
                                ]
                            }
                            tool_index += 1
                            chunk = chat_completion_chunk(response_id, created_at, model, delta_payload)
                            encoded = f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                            yield encoded
                        continue
                    if event.event == "response.completed":
                        response_obj = parsed.get("response") or {}
                        latest_response_id = response_obj.get("id")
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
                        finish_reason = "tool_calls" if saw_tool_calls else "stop"
                        final_chunk = chat_completion_chunk(
                            response_id,
                            created_at,
                            model,
                            {},
                            finish_reason=finish_reason,
                        )
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        if usage_requested and latest_usage is not None:
                            usage_chunk = chat_completion_chunk(
                                response_id,
                                created_at,
                                model,
                                {},
                                finish_reason=None,
                                usage=latest_usage,
                            )
                            yield f"data: {json.dumps(usage_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                if conversation_id:
                    store.touch_conversation(conversation_id, latest_response_id)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{body.get('conversation')}`.")
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
            model = body.get("model")
            if isinstance(model, str):
                resolved = providers.resolve_model(model)
                traffic.write(
                    {
                        "request_id": request_id,
                        "phase": "provider_resolution",
                        "provider": resolved.provider,
                        "model": resolved.public_id,
                    }
                )
                if resolved.provider == "claude":
                    claude_runtime = providers.claude
                    if claude_runtime is None:
                        raise ProviderError(
                            503,
                            "The Claude runtime is not available in this AIRelays process.",
                            code="provider_unavailable",
                        )
                    ignored_parameters = strip_unsupported_response_parameters(body)
                    log_adaptation(request_id, ignored_parameters, reason=CLAUDE_ADAPTATION_REASON)
                    claude_headers = _adaptation_headers(ignored_parameters)
                    if not body.get("stream"):
                        payload = await claude_runtime.create_completion(body, request_id)
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

                    async def claude_event_stream() -> AsyncIterator[bytes]:
                        async for chunk in claude_runtime.stream_completion(body, request_id):
                            traffic.write(
                                {
                                    "request_id": request_id,
                                    "phase": "outbound_stream_chunk",
                                    "provider": "claude",
                                    "body": snapshot_body("text/event-stream", chunk),
                                }
                            )
                            yield chunk

                    return StreamingResponse(
                        claude_event_stream(),
                        media_type="text/event-stream",
                        headers=claude_headers,
                    )
            payload, wants_stream, conversation_id = completions_to_responses(body)
            ignored_parameters = strip_unsupported_response_parameters(payload)
            log_adaptation(request_id, ignored_parameters)
            response_headers = _adaptation_headers(ignored_parameters)
            if conversation_id:
                conversation = store.get_conversation(conversation_id)
                if conversation["seed_items"] and not conversation["latest_response_id"]:
                    payload["input"] = conversation["seed_items"] + payload["input"]
            if not wants_stream:
                response_payload = await backend.collect_response(payload, request_id, conversation_id)
                if conversation_id:
                    store.touch_conversation(conversation_id, response_payload.get("id"))
                return logged_json(
                    request_id,
                    responses_to_completion(response_payload),
                    headers=response_headers,
                )

            async def event_stream() -> AsyncIterator[bytes]:
                response_id = f"cmpl_{uuid.uuid4().hex}"
                created_at = int(time.time())
                model = body.get("model", "unknown")
                latest_response_id: str | None = None
                async for event in backend.stream_response_events(payload, request_id, conversation_id):
                    try:
                        parsed = json.loads(event.data)
                    except json.JSONDecodeError:
                        continue
                    if event.event == "response.created":
                        response_obj = parsed.get("response") or {}
                        response_id = response_obj.get("id", response_id)
                        created_at = response_obj.get("created_at", created_at)
                        model = response_obj.get("model", model)
                        continue
                    if event.event == "response.output_text.delta":
                        chunk = completion_chunk(
                            response_id,
                            created_at,
                            model,
                            parsed.get("delta", ""),
                            finish_reason=None,
                        )
                        yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        continue
                    if event.event == "response.completed":
                        response_obj = parsed.get("response") or {}
                        latest_response_id = response_obj.get("id")
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
                            finish_reason="stop",
                        )
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                if conversation_id:
                    store.touch_conversation(conversation_id, latest_response_id)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers=response_headers,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown conversation id `{body.get('conversation')}`.")
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/v1/completions")
    async def completions_route(request: Request) -> Response:
        return await _completions_route(request)

    @app.post("/no-tools/v1/completions")
    async def completions_route_no_tools(request: Request) -> Response:
        return await _completions_route(request)

    @app.post("/v1/embeddings")
    @app.post("/v1/images/{operation}")
    @app.post("/v1/audio/{operation}")
    @app.post("/v1/realtime/sessions")
    async def unsupported_route(request: Request) -> JSONResponse:
        request_id = _request_id(request)
        body = await request.body()
        await log_inbound(request_id, request, body)
        payload = {
            "error": {
                "message": (
                    "This subscription-backed server does not expose a verified upstream "
                    "implementation for this route yet."
                ),
                "type": "unsupported_error",
            }
        }
        return logged_json(request_id, payload, status_code=501)

    return app
