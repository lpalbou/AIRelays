from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from airelay.auth import AuthManager
from airelay.config import Settings
from airelay.traffic import TrafficLogger, snapshot_body


class BackendError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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
        response = await self._request_json(
            request_id=request_id,
            method="GET",
            path=f"/models?client_version={self._settings.client_version}",
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

    async def collect_response(
        self, payload: dict[str, Any], request_id: str, session_id: str | None
    ) -> dict[str, Any]:
        latest_response: dict[str, Any] | None = None
        output_by_index: dict[int, dict[str, Any]] = {}
        async for event in self.stream_response_events(payload, request_id, session_id):
            try:
                parsed = json.loads(event.data)
            except json.JSONDecodeError:
                continue
            response = parsed.get("response")
            if isinstance(response, dict):
                latest_response = {**(latest_response or {}), **response}
            if event.event == "response.output_item.done":
                item = parsed.get("item")
                output_index = parsed.get("output_index")
                if isinstance(item, dict) and isinstance(output_index, int):
                    output_by_index[output_index] = item
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

    def _log_stream_summary(self, request_id: str, event: SSEEvent) -> None:
        if event.event != "response.completed":
            return
        try:
            parsed = json.loads(event.data)
        except json.JSONDecodeError:
            return
        response = parsed.get("response")
        if not isinstance(response, dict):
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
            response = await self._client.request(method, url, json=body, headers=headers)
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
