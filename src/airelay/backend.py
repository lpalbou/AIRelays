from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from airelay.auth import AuthManager
from airelay.config import Settings
from airelay.image_usage import ImageUsageWindow, parse_image_usage_headers
from airelay.images import summarize_image_payload
from airelay.traffic import TrafficLogger, snapshot_body


class BackendError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        # The image bucket as reported by the failing image response's
        # headers (a 429 still carries them), so the pool can bench the
        # image quota without a second call. None on every other route.
        self.image_usage: ImageUsageWindow | None = None


# The status assigned to an upstream in-stream failure decides everything
# downstream: the account pool benches-and-fails-over on 429/5xx, the retry
# layer backs off on 429/5xx, and the client sees the status verbatim. The
# taxonomy has three classes, checked in this order:
#
#   limit  -> 429  this account's quota/rate window is spent; another
#                  account can serve, so bench this one and fail over.
#   client -> 400  deterministic: the REQUEST is unacceptable and every
#                  account rejects it identically. Surface immediately with
#                  the upstream error passed through — no rotation, no
#                  bench, no backoff, no second paid upstream call.
#   other  -> 502  the upstream's fault until proven otherwise; short bench,
#                  failover, and backoff retry (episodic bad windows like
#                  the 2026-08-01 `server_is_overloaded` incident recover
#                  exactly this way).
#
# operator 2026-08-01 (req_7d3b0b16f7ee43a5a6569c38b6d46133): before the
# client class existed, "anything else is the upstream's fault" mapped a
# deterministic context-window invalid_request_error to 502. That benched
# BOTH accounts as "transient", burned 8 paid upstream calls across 4
# backoff rounds on a 1.5MB request that could never succeed, and told the
# client "All 2 OpenAI accounts are at their limits (earliest retry in
# 25s)" while the accounts had 64%/33% of their weekly budgets left.
_USAGE_LIMIT_CODES = frozenset(
    {
        "usage_limit_reached",
        "rate_limit_reached",
        "rate_limit_exceeded",
        # Spent-balance vocabulary: capacity class, not request class — a
        # sibling account with quota can still serve the request.
        "insufficient_quota",
    }
)
# OpenAI's canonical client-error type, plus the unambiguous request-shape
# rejection codes. The limit check runs FIRST: if an upstream ever labels a
# quota rejection `invalid_request_error`, the limit code still wins and
# failover still happens.
_CLIENT_ERROR_TYPES = frozenset({"invalid_request_error"})
_CLIENT_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "invalid_prompt",
        "invalid_value",
        "unsupported_value",
        "unsupported_parameter",
        "unknown_parameter",
        "string_above_max_length",
    }
)

# Shared with app.py's streaming lanes: the events that legitimately end a
# response (with output and billed usage) versus the ones that abort it.
SUCCESS_TERMINAL_EVENTS = ("response.completed", "response.incomplete")
FAILURE_EVENTS = ("response.failed", "error")

# The image routes, relative to the upstream base URL.
IMAGE_OPERATION_PATHS = {"generation": "images/generations", "edit": "images/edits"}


def stream_error_object(parsed: Any) -> dict[str, Any] | None:
    """The upstream error object carried by a failure event, or None.

    `error` events nest it under "error" (the top-level "type" is the event
    discriminator, never an error classification); `response.failed` nests
    it under response["error"]. Some upstream variants inline code/message
    at the top level, so that shape is accepted last.
    """
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if isinstance(error, dict):
        return error
    response = parsed.get("response")
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        return response["error"]
    if isinstance(parsed.get("message"), str) or isinstance(parsed.get("code"), str):
        return {key: parsed[key] for key in ("code", "message", "param") if key in parsed}
    return None


def failure_backend_error(error: dict[str, Any] | None) -> BackendError:
    """A structured BackendError for a stream that ended without success.

    The detail is the OpenAI error JSON (never fabricated emptiness) so
    clients see the upstream reason with its real type/code/message/param,
    and the status carries the taxonomy class every downstream layer (pool
    failover, retry backoff, the final HTTP response) acts on. This is the
    single place stream failure events are classified — the non-streaming
    collect path, the pool's streaming path, and the app's first-event
    probe (chat, completions, and the /v1/responses passthrough) all raise
    through here, so the taxonomy cannot drift between lanes.
    """
    if error is None:
        error = {
            "message": "Upstream stream ended before response.completed.",
            "type": "upstream_error",
            "code": "incomplete_stream",
        }
    labels = {error.get("code"), error.get("type")}
    if labels & _USAGE_LIMIT_CODES:
        status = 429
    elif labels & _CLIENT_ERROR_TYPES or labels & _CLIENT_ERROR_CODES or (
        # A `param` naming a request field is OpenAI's own statement that
        # the REQUEST was at fault (the incident error carried
        # param="input"); capacity and server errors ship param as null.
        isinstance(error.get("param"), str)
        and error.get("param")
    ):
        status = 400
    else:
        # Unknown/ambiguous stream errors default to the retriable class.
        # Deliberate: genuine upstream bad windows arrive with vocabulary
        # this relay has never seen, and failing fast on them would trade
        # away the resilience the retry layer exists for. Deterministic
        # client rejections, by contrast, arrive with OpenAI's explicit
        # type/code/param labels (the 2026-08-01 incident error carried all
        # three), so the client class above catches them before any paid
        # rotation happens.
        status = 502
    return BackendError(status, json.dumps({"error": error}, ensure_ascii=True))


@dataclass(slots=True)
class SSEEvent:
    event: str
    data: str


def encode_sse(event: SSEEvent) -> bytes:
    return f"event: {event.event}\ndata: {event.data}\n\n".encode("utf-8")


class ChatGptCodexBackend:
    def __init__(
        self,
        settings: Settings,
        auth_manager: AuthManager,
        traffic: TrafficLogger,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._auth_manager = auth_manager
        self._traffic = traffic
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=True
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def list_models(self, request_id: str) -> dict[str, Any]:
        from airelay.model_discovery import codex_catalog_version

        version = await asyncio.to_thread(codex_catalog_version, self._settings.client_version)
        response = await self._request_json(
            request_id=request_id,
            method="GET",
            path=f"/models?client_version={version}",
            body=None,
            session_id=None,
        )
        return response

    async def get_subscription_status(self, request_id: str) -> dict[str, Any]:
        response = await self._request_json(
            request_id=request_id,
            method="GET",
            path="/wham/usage",
            body=None,
            session_id=None,
            base_url=self._usage_base_url(),
        )
        if not isinstance(response, dict):
            raise BackendError(502, "Upstream usage endpoint returned a non-object payload.")
        return response

    # ----- images -----
    #
    # The subscription backend serves OpenAI's Images API shape on
    # `images/generations` and `images/edits` (JSON bodies on both; verified
    # live 2026-10-18) for the Codex client's built-in image tool. Answers
    # are not streamed: one JSON document per call, images as `b64_json`.

    async def create_image(
        self, operation: str, payload: dict[str, Any], request_id: str
    ) -> tuple[dict[str, Any], ImageUsageWindow | None]:
        """One image generation (`operation="generation"`) or edit
        (`"edit"`). Returns the upstream Images response and the image
        quota window its headers report."""
        path = IMAGE_OPERATION_PATHS[operation]
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        url = f"{self._settings.upstream_base_url.rstrip('/')}/{path}"
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "upstream_request",
                "method": "POST",
                "url": url,
                "body": {
                    "kind": "json",
                    "bytes": len(body),
                    "json": summarize_image_payload(payload),
                },
            }
        )
        response = await self._post_json_bytes(url, body, request_id, self._settings.image_timeout_seconds)
        window = parse_image_usage_headers(response.headers)
        raw = response.content
        if response.status_code >= 400:
            self._traffic.write(
                {
                    "request_id": request_id,
                    "phase": "upstream_response_error",
                    "status_code": response.status_code,
                    "body": snapshot_body(response.headers.get("content-type"), raw),
                }
            )
            error = BackendError(response.status_code, raw.decode("utf-8", errors="replace"))
            error.image_usage = window
            raise error
        try:
            parsed = response.json()
        except ValueError as exc:
            raise BackendError(502, "Upstream image response was not JSON.") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
            raise BackendError(502, "Upstream image response carried no image data.")
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "upstream_response",
                "method": "POST",
                "url": url,
                "status_code": response.status_code,
                "body": {"kind": "json", "bytes": len(raw), "json": summarize_image_payload(parsed)},
            }
        )
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "upstream_usage",
                "model": payload.get("model"),
                "images": len(parsed["data"]),
                "usage": parsed.get("usage"),
                "image_quota": None if window is None else {
                    "limit_id": window.limit_id,
                    "used_percent": window.used_percent,
                    "reset_at": window.reset_at,
                },
            }
        )
        return parsed, window

    async def probe_image_usage(self, request_id: str) -> ImageUsageWindow | None:
        """The image quota window without generating anything: a body
        with no prompt is refused upstream (400, no quota spent) and the
        refusal carries the same `x-codex-*` usage headers as a success.
        None when the answer carries no usage headers."""
        url = f"{self._settings.upstream_base_url.rstrip('/')}/{IMAGE_OPERATION_PATHS['generation']}"
        response = await self._post_json_bytes(url, b"{}", request_id, min(30.0, self._settings.image_timeout_seconds))
        window = parse_image_usage_headers(response.headers)
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "upstream_image_usage_probe",
                "status_code": response.status_code,
                "image_quota": None if window is None else {
                    "limit_id": window.limit_id,
                    "used_percent": window.used_percent,
                    "window_seconds": window.window_seconds,
                    "reset_at": window.reset_at,
                },
            }
        )
        if response.status_code == 401:
            raise BackendError(401, response.content.decode("utf-8", errors="replace"))
        return window

    async def _post_json_bytes(
        self, url: str, body: bytes, request_id: str, timeout: float
    ) -> httpx.Response:
        """A non-streaming JSON POST with the subscription headers and the
        one refresh-and-retry on 401 every upstream call gets. Transport
        failures become 502s like everywhere else."""
        del request_id
        retried = False
        while True:
            record = await self._auth_manager.ensure_fresh_tokens()
            headers = {
                "Authorization": f"Bearer {record.access_token}",
                "ChatGPT-Account-ID": record.account_id or "",
                "Content-Type": "application/json",
                "originator": "codex_cli_rs",
            }
            try:
                response = await self._client.post(url, content=body, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                raise BackendError(502, f"Upstream connection failed: {exc}") from exc
            if response.status_code == 401 and not retried:
                retried = True
                await self._auth_manager.refresh_tokens()
                continue
            return response

    async def list_rate_limit_reset_credits(self, request_id: str) -> dict[str, Any]:
        """The account's usage-limit reset credits ("Redeem reset" in Codex):
        every credit with its status, grant and expiry times, plus counts.
        Same personal usage surface as the usage report, so the pool caches
        it with the same care."""
        response = await self._request_json(
            request_id=request_id,
            method="GET",
            path="/wham/rate-limit-reset-credits",
            body=None,
            session_id=None,
            base_url=self._usage_base_url(),
        )
        if not isinstance(response, dict):
            raise BackendError(
                502, "Upstream reset-credits endpoint returned a non-object payload."
            )
        return response

    async def consume_rate_limit_reset_credit(
        self, request_id: str, redeem_request_id: str, credit_id: str | None = None
    ) -> dict[str, Any]:
        """Redeems one reset credit, the call Codex makes behind "Yes, use
        reset". ``redeem_request_id`` is the idempotency key the upstream
        requires (a retry with the same key cannot spend a second credit);
        ``credit_id`` picks a specific credit, otherwise the upstream chooses.
        The response ``code`` is one of reset, nothing_to_reset, no_credit,
        already_redeemed."""
        body: dict[str, Any] = {"redeem_request_id": redeem_request_id}
        if credit_id:
            body["credit_id"] = credit_id
        response = await self._request_json(
            request_id=request_id,
            method="POST",
            path="/wham/rate-limit-reset-credits/consume",
            body=body,
            session_id=None,
            base_url=self._usage_base_url(),
        )
        if not isinstance(response, dict):
            raise BackendError(
                502, "Upstream reset-credit redeem returned a non-object payload."
            )
        return response

    async def collect_response(
        self, payload: dict[str, Any], request_id: str, session_id: str | None
    ) -> dict[str, Any]:
        latest_response: dict[str, Any] | None = None
        output_by_index: dict[int, dict[str, Any]] = {}
        # `response.incomplete` counts as success: it carries real partial
        # output and billed usage (e.g. token-limit truncation). Anything
        # short of a success terminal must raise — a merged `failed`
        # response returned as a result becomes an empty 200 downstream.
        succeeded = False
        saw_failure = False
        failure: dict[str, Any] | None = None
        async for event in self.stream_response_events(payload, request_id, session_id):
            try:
                parsed = json.loads(event.data)
            except json.JSONDecodeError:
                continue
            response = parsed.get("response")
            if isinstance(response, dict):
                latest_response = {**(latest_response or {}), **response}
            if event.event in SUCCESS_TERMINAL_EVENTS:
                succeeded = True
            elif event.event in FAILURE_EVENTS:
                saw_failure = True
                if failure is None:
                    failure = stream_error_object(parsed)
            if event.event == "response.output_item.done":
                item = parsed.get("item")
                output_index = parsed.get("output_index")
                if isinstance(item, dict) and isinstance(output_index, int):
                    output_by_index[output_index] = item
        if not succeeded:
            if saw_failure:
                raise failure_backend_error(failure)
            if latest_response is None:
                raise BackendError(502, "Upstream stream ended without a response payload.")
            raise failure_backend_error(None)
        if latest_response is None:
            raise BackendError(502, "Upstream stream ended without a response payload.")
        if output_by_index:
            latest_response["output"] = [
                output_by_index[index] for index in sorted(output_by_index)
            ]
        return latest_response

    async def stream_response_events(
        self, payload: dict[str, Any], request_id: str, session_id: str | None
    ) -> AsyncIterator[SSEEvent]:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "upstream_request",
                "method": "POST",
                "url": f"{self._settings.upstream_base_url}/responses",
                "body": snapshot_body("application/json", body),
                "session_id": session_id,
            }
        )

        retried = False
        while True:
            record = await self._auth_manager.ensure_fresh_tokens()
            headers = {
                "Authorization": f"Bearer {record.access_token}",
                "ChatGPT-Account-ID": record.account_id or "",
                "Content-Type": "application/json",
                "originator": "codex_cli_rs",
            }
            if session_id:
                headers["session_id"] = session_id

            try:
                async with self._client.stream(
                    "POST",
                    f"{self._settings.upstream_base_url}/responses",
                    content=body,
                    headers=headers,
                ) as response:
                    if response.status_code == 401 and not retried:
                        retried = True
                        await self._auth_manager.refresh_tokens()
                        continue
                    if response.status_code >= 400:
                        text = await response.aread()
                        self._traffic.write(
                            {
                                "request_id": request_id,
                                "phase": "upstream_response_error",
                                "status_code": response.status_code,
                                "body": snapshot_body(
                                    response.headers.get("content-type"), text
                                ),
                            }
                        )
                        raise BackendError(response.status_code, text.decode("utf-8", errors="replace"))

                    event_name = "message"
                    data_lines: list[str] = []
                    # Per-line logging is opt-in (config [logging] stream_lines):
                    # a single streamed response is hundreds of lines, which
                    # bloats the traffic log ~50x under load and evicts real
                    # request records from every log reader's window. Summary
                    # records (upstream_request/usage/response, errors) are
                    # always written regardless.
                    log_lines = self._settings.log_stream_lines
                    async for raw_line in response.aiter_lines():
                        if log_lines:
                            self._traffic.write(
                                {
                                    "request_id": request_id,
                                    "phase": "upstream_stream_line",
                                    "line": raw_line,
                                }
                            )
                        if raw_line == "":
                            if data_lines:
                                event = SSEEvent(event=event_name, data="\n".join(data_lines))
                                self._log_stream_summary(request_id, event)
                                yield event
                            event_name = "message"
                            data_lines = []
                            continue
                        if raw_line.startswith("event:"):
                            event_name = raw_line.removeprefix("event:").strip()
                            continue
                        if raw_line.startswith("data:"):
                            data_lines.append(raw_line.removeprefix("data:").lstrip())
                    if data_lines:
                        event = SSEEvent(event=event_name, data="\n".join(data_lines))
                        self._log_stream_summary(request_id, event)
                        yield event
                    return
            except httpx.HTTPError as exc:
                # Transport failures (DNS, connect, TLS, timeouts) become
                # structured 502s so the account pool can classify them and
                # fail over to another account instead of surfacing a raw
                # exception to the client.
                raise BackendError(502, f"Upstream connection failed: {exc}") from exc

    def _log_stream_summary(self, request_id: str, event: SSEEvent) -> None:
        if event.event not in SUCCESS_TERMINAL_EVENTS and event.event not in FAILURE_EVENTS:
            return
        try:
            parsed = json.loads(event.data)
        except json.JSONDecodeError:
            return
        response = parsed.get("response")
        if not isinstance(response, dict):
            response = {}
        if event.event in FAILURE_EVENTS:
            # Failure events must leave a trace: without this record a bad
            # upstream window is invisible in the traffic log (requests jump
            # from upstream_request straight to outbound_response).
            self._traffic.write(
                {
                    "request_id": request_id,
                    "phase": "upstream_stream_error",
                    "event": event.event,
                    "response_id": response.get("id"),
                    "model": response.get("model"),
                    "status": response.get("status"),
                    "error": stream_error_object(parsed),
                }
            )
            return
        if not response:
            return
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "upstream_usage",
                "response_id": response.get("id"),
                "model": response.get("model"),
                "status": response.get("status"),
                "usage": response.get("usage"),
            }
        )

    def _usage_base_url(self) -> str:
        return self._settings.upstream_base_url.rstrip("/").removesuffix("/codex")

    async def _request_json(
        self,
        request_id: str,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        session_id: str | None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        retried = False
        while True:
            record = await self._auth_manager.ensure_fresh_tokens()
            headers = {
                "Authorization": f"Bearer {record.access_token}",
                "ChatGPT-Account-ID": record.account_id or "",
                "originator": "codex_cli_rs",
            }
            if session_id:
                headers["session_id"] = session_id
            root_url = (base_url or self._settings.upstream_base_url).rstrip("/")
            url = f"{root_url}{path}"
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = await self._client.request(method, url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise BackendError(502, f"Upstream connection failed: {exc}") from exc
            if response.status_code == 401 and not retried:
                retried = True
                await self._auth_manager.refresh_tokens()
                continue
            raw = response.content
            self._traffic.write(
                {
                    "request_id": request_id,
                    "phase": "upstream_response",
                    "method": method,
                    "url": url,
                    "status_code": response.status_code,
                    "body": snapshot_body(response.headers.get("content-type"), raw),
                }
            )
            if response.status_code >= 400:
                raise BackendError(response.status_code, raw.decode("utf-8", errors="replace"))
            return response.json()
