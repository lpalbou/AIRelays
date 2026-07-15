from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from airelay.auth import AuthManager
from airelay.backend import ChatGptCodexBackend
from airelay.config import Settings
from airelay.traffic import TrafficLogger, snapshot_body
from airelay.transforms import chat_completion_chunk, completion_chunk


class ProviderError(RuntimeError):
    def __init__(self, status_code: int, detail: str, *, code: str = "provider_error") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    provider: str
    public_id: str
    upstream_id: str


# Reasoning modes per provider, verified against the live upstreams: every
# model the Codex subscription backend serves accepts these efforts on
# `reasoning.effort` (and rejects `minimal` with an explicit error), while
# the claude CLI's `--effort` flag accepts exactly the values below and
# would otherwise silently fall back to its default on unknown ones.
OPENAI_REASONING_MODES = ("none", "low", "medium", "high", "xhigh")
CLAUDE_REASONING_MODES = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    provider: str
    owned_by: str
    upstream_id: str
    routes: dict[str, bool]
    stateful_conversations: bool
    reasoning_modes: tuple[str, ...] = ()
    # The effort used when a request does not set one: the Codex upstream
    # runs at "none"; the claude CLI applies its own adaptive default, which
    # is model-controlled (reported as null).
    reasoning_default: str | None = None

    def as_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": self.owned_by,
            "airelays": {
                "provider": self.provider,
                "upstream_model": self.upstream_id,
                "capabilities": {
                    "routes": self.routes,
                    "stateful_conversations": self.stateful_conversations,
                },
                "reasoning": {
                    "parameter": "reasoning_effort",
                    "modes": list(self.reasoning_modes),
                    "default": self.reasoning_default,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class ClaudeTextRequest:
    public_model: str
    upstream_model: str
    system_prompt: str | None
    prompt: str
    include_usage: bool
    # Reasoning depth for the CLI's --effort flag; None uses the model's
    # own adaptive default.
    effort: str | None = None


def _openai_model_record(model_id: str) -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider="openai",
        owned_by="airelays-openai-subscription",
        upstream_id=model_id,
        routes={
            "responses": True,
            "chat_completions": True,
            "completions": True,
            "files": True,
            "conversations": True,
            "subscription_status": True,
        },
        stateful_conversations=True,
        reasoning_modes=OPENAI_REASONING_MODES,
        reasoning_default="none",
    )


def _claude_routes() -> dict[str, bool]:
    return {
        "responses": False,
        "chat_completions": True,
        "completions": True,
        "files": False,
        "conversations": False,
        "subscription_status": False,
    }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ProviderError(
            422,
            "The Claude runtime supports only string or text-part message content.",
            code="unsupported_for_provider",
        )
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise ProviderError(
                422,
                "The Claude runtime supports only text content parts.",
                code="unsupported_for_provider",
            )
        kind = part.get("type")
        if kind not in {"text", "input_text", "output_text"}:
            raise ProviderError(
                422,
                f"The Claude runtime does not support message content part `{kind}`.",
                code="unsupported_for_provider",
            )
        text = part.get("text", "")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _split_chat_messages(messages: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, str]]]:
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            text = _content_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role not in {"user", "assistant"}:
            raise ProviderError(
                422,
                "The Claude runtime supports only system, developer, user, and assistant messages.",
                code="unsupported_for_provider",
            )
        if message.get("tool_calls"):
            raise ProviderError(
                422,
                "The Claude runtime does not support tool calls.",
                code="unsupported_for_provider",
            )
        turns.append({"role": role, "text": _content_text(message.get("content"))})
    if not turns:
        raise ProviderError(
            422,
            "The Claude runtime requires at least one user or assistant turn.",
            code="unsupported_for_provider",
        )
    return system_parts, turns


def _chat_transcript(turns: list[dict[str, str]]) -> str:
    if len(turns) == 1 and turns[0]["role"] == "user":
        return turns[0]["text"]
    lines: list[str] = []
    for turn in turns:
        prefix = "Assistant:" if turn["role"] == "assistant" else "Human:"
        lines.append(f"{prefix} {turn['text']}")
    return "\n\n".join(lines) + "\n\nAssistant:"


def _finish_reason(stop_reason: str | None) -> str:
    if stop_reason == "max_tokens":
        return "length"
    return "stop"


def _provided(value: Any) -> bool:
    return value is not None and value is not False and value != [] and value != {}


def _claude_effort(body: dict[str, Any], route: str) -> str | None:
    """Validated reasoning effort for the claude CLI. The CLI silently
    ignores unknown --effort values and falls back to its default, which
    would be silent degradation — so unsupported values are rejected here
    with the supported list."""
    effort = body.get("reasoning_effort")
    if effort is None:
        return None
    if isinstance(effort, str) and effort.lower() in CLAUDE_REASONING_MODES:
        return effort.lower()
    supported = ", ".join(CLAUDE_REASONING_MODES)
    raise ProviderError(
        422,
        f"Unsupported `reasoning_effort` {effort!r} for Claude models on `{route}`. "
        f"Supported values: {supported}.",
        code="unsupported_for_provider",
    )


def _usage_from_claude_result(payload: dict[str, Any] | None) -> dict[str, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _stderr_message(stderr: bytes, returncode: int) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    return text or f"claude exited with code {returncode}"


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The usage endpoint's budget is tiny (observed lockouts of an hour or
# more). Five minutes keeps the meter useful while treating every upstream
# call as precious; attempts are additionally spaced a minute apart no
# matter how they end.
CLAUDE_USAGE_CACHE_SECONDS = 300.0
CLAUDE_USAGE_MIN_ATTEMPT_INTERVAL = 60.0
CLAUDE_FIVE_HOUR_SECONDS = 5 * 3600
CLAUDE_SEVEN_DAY_SECONDS = 7 * 86400


class ClaudeCliRuntime:
    def __init__(self, settings: Settings, traffic: TrafficLogger | None = None) -> None:
        self._settings = settings
        self._traffic = traffic
        self._semaphore = asyncio.Semaphore(settings.claude_max_concurrent_requests)
        self._models = self._build_models(settings.claude_models)
        # Last CLI status probe, reused for identity in the usage payload
        # (probing spawns the Node CLI — too slow to repeat per usage call).
        self._last_probe: dict[str, Any] | None = None
        # Status probes spawn the Node CLI twice and block; the desktop
        # polls status every ~1.5s, so probes are cached and refreshed in a
        # background thread (stale-while-revalidate) — the status route must
        # never wait on a subprocess.
        self._probe_cached_at: float = 0.0
        self._probe_refreshing = False
        self._probe_lock = threading.Lock()
        # The usage endpoint is undocumented and rate-limited; cache briefly.
        self._usage_cache: dict[str, Any] | None = None
        self._usage_cache_at: float = 0.0
        # Last successful payload, kept indefinitely: when the upstream
        # rate-limits us (429 with retry-after up to an hour), serving
        # honest stale data beats serving nothing.
        self._usage_last_good: dict[str, Any] | None = None
        self._usage_last_good_epoch: float = 0.0
        # Monotonic deadline from the last 429's retry-after; until it
        # passes, no request is sent upstream (poking a rate-limited
        # endpoint extends the lockout).
        self._usage_blocked_until: float = 0.0
        # Single-flight: concurrent callers at cache expiry must not each
        # poke the aggressively rate-limited endpoint.
        self._usage_fetch_lock = asyncio.Lock()
        # Fingerprint of a token the upstream rejected (401/403). Claude
        # Code rotates its access token; retrying a dead one both fails and
        # burns the rate budget. Blocked until the resolved token CHANGES.
        self._usage_rejected_fingerprint: str | None = None
        # Source of the rejected token ("file" is static and needs user
        # action; CLI sources self-heal on rotation) — drives the message.
        self._usage_rejected_source: str | None = None
        # Attempt spacing across all outcome classes.
        self._usage_last_attempt_at: float = -CLAUDE_USAGE_MIN_ATTEMPT_INTERVAL
        # Guardrail state survives restarts: the block window and the last
        # good snapshot are persisted, so restarting the relay can never
        # turn into a fresh poke at a locked-out endpoint (the exact
        # hammering pattern that earns hour-long lockouts).
        self._usage_state_path = settings.data_dir / "claude-usage-state.json"
        self._load_usage_state()

    def _build_models(self, configured: tuple[str, ...]) -> dict[str, ProviderModel]:
        records: dict[str, ProviderModel] = {}
        for model_id in configured:
            upstream_id = model_id.split(":", 1)[1] if ":" in model_id else model_id
            record = ProviderModel(
                id=model_id,
                provider="claude",
                owned_by="airelays-claude-subscription",
                upstream_id=upstream_id,
                routes=_claude_routes(),
                stateful_conversations=False,
                reasoning_modes=CLAUDE_REASONING_MODES,
                reasoning_default=None,
            )
            records[model_id] = record
        return records

    def list_models(self) -> list[dict[str, Any]]:
        return [record.as_wire() for record in self._models.values()]

    def resolve_model(self, model_id: str) -> ResolvedModel | None:
        record = self._models.get(model_id)
        if record is None:
            return None
        return ResolvedModel(
            provider="claude",
            public_id=record.id,
            upstream_id=record.upstream_id,
        )

    def status(self) -> dict[str, Any]:
        probe = self._cached_probe()
        ready = bool(probe.get("installed") and probe.get("logged_in"))
        return {
            "enabled": True,
            "local_only": True,
            "requires_relay_bearer_auth": self._settings.require_bearer_auth,
            "stateless_only": True,
            "ready_for_requests": ready,
            "cli_installed": probe.get("installed", False),
            "cli_version": probe.get("version"),
            "auth_method": probe.get("auth_method"),
            "api_provider": probe.get("api_provider"),
            "logged_in": probe.get("logged_in", False),
            "email": probe.get("email"),
            "subscription_type": probe.get("subscription_type"),
            "models": [record.id for record in self._models.values()],
            "oauth_token_source": self._settings.claude_oauth_token_source(),
            "notes": [
                "Use `claude auth login --claudeai` for browser-based local login.",
                "For headless environments, run `claude setup-token` on a machine with a "
                "browser, then store the token with `airelays claude set-token`.",
            ],
        }

    async def create_chat_completion(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = self._prepare_chat_request(body)
        result = await self._run_json(request, request_id)
        return {
            "id": f"chatcmpl_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.public_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result.get("result", ""),
                    },
                    "finish_reason": _finish_reason(result.get("stop_reason")),
                }
            ],
            "usage": _usage_from_claude_result(result),
        }

    async def stream_chat_completion(
        self,
        body: dict[str, Any],
        request_id: str,
    ) -> AsyncIterator[bytes]:
        request = self._prepare_chat_request(body)
        response_id = f"chatcmpl_{uuid.uuid4().hex}"
        created_at = int(time.time())
        sent_role = False
        saw_text = False
        assistant_fallback = ""
        last_usage: dict[str, int] | None = None
        finish_reason = "stop"
        async for event in self._run_stream(request, request_id):
            event_type = event.get("type")
            if event_type == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if not isinstance(text, str) or not text:
                            continue
                        saw_text = True
                        delta_payload: dict[str, Any] = {"content": text}
                        if not sent_role:
                            delta_payload = {"role": "assistant", "content": text}
                            sent_role = True
                        chunk = chat_completion_chunk(
                            response_id,
                            created_at,
                            request.public_model,
                            delta_payload,
                        )
                        yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                elif inner.get("type") == "message_delta":
                    finish_reason = _finish_reason((inner.get("delta") or {}).get("stop_reason"))
                    usage = inner.get("usage")
                    if isinstance(usage, dict):
                        last_usage = {
                            "prompt_tokens": int(usage.get("input_tokens") or 0),
                            "completion_tokens": int(usage.get("output_tokens") or 0),
                            "total_tokens": int(usage.get("input_tokens") or 0)
                            + int(usage.get("output_tokens") or 0),
                        }
            elif event_type == "assistant" and not saw_text:
                message = event.get("message") or {}
                content = message.get("content") or []
                assistant_fallback = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            elif event_type == "result":
                last_usage = _usage_from_claude_result(event)
                finish_reason = _finish_reason(event.get("stop_reason"))
                if not assistant_fallback:
                    result_text = event.get("result")
                    if isinstance(result_text, str):
                        assistant_fallback = result_text
        if assistant_fallback and not saw_text:
            delta_payload = {"role": "assistant", "content": assistant_fallback}
            chunk = chat_completion_chunk(
                response_id,
                created_at,
                request.public_model,
                delta_payload,
            )
            yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        final_chunk = chat_completion_chunk(
            response_id,
            created_at,
            request.public_model,
            {},
            finish_reason=finish_reason,
        )
        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        if request.include_usage and last_usage is not None:
            usage_chunk = chat_completion_chunk(
                response_id,
                created_at,
                request.public_model,
                {},
                finish_reason=None,
                usage=last_usage,
            )
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    async def create_completion(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = self._prepare_completion_request(body)
        result = await self._run_json(request, request_id)
        return {
            "id": f"cmpl_{uuid.uuid4().hex}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": request.public_model,
            "choices": [
                {
                    "text": result.get("result", ""),
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": _finish_reason(result.get("stop_reason")),
                }
            ],
            "usage": _usage_from_claude_result(result),
        }

    async def stream_completion(
        self,
        body: dict[str, Any],
        request_id: str,
    ) -> AsyncIterator[bytes]:
        request = self._prepare_completion_request(body)
        response_id = f"cmpl_{uuid.uuid4().hex}"
        created_at = int(time.time())
        saw_text = False
        assistant_fallback = ""
        finish_reason = "stop"
        async for event in self._run_stream(request, request_id):
            event_type = event.get("type")
            if event_type == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if not isinstance(text, str) or not text:
                            continue
                        saw_text = True
                        chunk = completion_chunk(
                            response_id,
                            created_at,
                            request.public_model,
                            text,
                            finish_reason=None,
                        )
                        yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                elif inner.get("type") == "message_delta":
                    finish_reason = _finish_reason((inner.get("delta") or {}).get("stop_reason"))
            elif event_type == "assistant" and not saw_text:
                message = event.get("message") or {}
                content = message.get("content") or []
                assistant_fallback = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            elif event_type == "result":
                finish_reason = _finish_reason(event.get("stop_reason"))
                if not assistant_fallback:
                    result_text = event.get("result")
                    if isinstance(result_text, str):
                        assistant_fallback = result_text
        if assistant_fallback and not saw_text:
            chunk = completion_chunk(
                response_id,
                created_at,
                request.public_model,
                assistant_fallback,
                finish_reason=None,
            )
            yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        final_chunk = completion_chunk(
            response_id,
            created_at,
            request.public_model,
            "",
            finish_reason=finish_reason,
        )
        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    def _prepare_chat_request(self, body: dict[str, Any]) -> ClaudeTextRequest:
        resolved = self._resolved_model_from_body(body)
        if body.get("n") not in {None, 1}:
            raise ProviderError(422, "The Claude runtime supports only `n=1`.", code="unsupported_for_provider")
        # Sampling parameters (`temperature`, `top_p`, `presence_penalty`,
        # `frequency_penalty`) and output-token limits (`max_completion_tokens`,
        # `max_tokens`) are not listed here: the app layer strips and discloses
        # them (x-airelays-ignored-parameters), the same documented adaptation
        # the OpenAI runtime applies, because the local claude CLI exposes no
        # equivalent controls and standard SDKs send them by default.
        for field in (
            "tools",
            "functions",
            "tool_choice",
            "function_call",
            "response_format",
            "conversation",
            "store",
            "previous_response_id",
            "modalities",
            "audio",
            "prediction",
            "parallel_tool_calls",
        ):
            if _provided(body.get(field)):
                raise ProviderError(
                    422,
                    f"The Claude runtime does not support `{field}` on `/v1/chat/completions`.",
                    code="unsupported_for_provider",
                )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ProviderError(422, "Chat completions requests must include a non-empty `messages` array.")
        system_parts, turns = _split_chat_messages(messages)
        system_prompt = "\n\n".join(part for part in system_parts if part.strip()) or None
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        return ClaudeTextRequest(
            public_model=resolved.public_id,
            upstream_model=resolved.upstream_id,
            system_prompt=system_prompt,
            prompt=_chat_transcript(turns),
            include_usage=include_usage,
            effort=_claude_effort(body, "/v1/chat/completions"),
        )

    def _prepare_completion_request(self, body: dict[str, Any]) -> ClaudeTextRequest:
        resolved = self._resolved_model_from_body(body)
        if body.get("n") not in {None, 1}:
            raise ProviderError(422, "The Claude runtime supports only `n=1`.", code="unsupported_for_provider")
        # Sampling parameters and output-token limits are stripped and
        # disclosed by the app layer instead of rejected here — see
        # _prepare_chat_request for rationale.
        for field in (
            "best_of",
            "echo",
            "logprobs",
            "suffix",
            "conversation",
            "store",
            "stop",
        ):
            if _provided(body.get(field)):
                raise ProviderError(
                    422,
                    f"The Claude runtime does not support `{field}` on `/v1/completions`.",
                    code="unsupported_for_provider",
                )
        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            if len(prompt) != 1 or not isinstance(prompt[0], str):
                raise ProviderError(
                    422,
                    "The Claude runtime supports only a single string prompt.",
                    code="unsupported_for_provider",
                )
            prompt = prompt[0]
        if not isinstance(prompt, str) or not prompt:
            raise ProviderError(422, "Completions requests must include a non-empty `prompt` string.")
        return ClaudeTextRequest(
            public_model=resolved.public_id,
            upstream_model=resolved.upstream_id,
            system_prompt=None,
            prompt=prompt,
            include_usage=False,
            effort=_claude_effort(body, "/v1/completions"),
        )

    def _resolved_model_from_body(self, body: dict[str, Any]) -> ResolvedModel:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ProviderError(422, "Requests must include a non-empty `model` string.")
        resolved = self.resolve_model(model)
        if resolved is None:
            raise ProviderError(
                422,
                f"Unknown Claude model `{model}`. Configure it under `[providers.claude].models` first.",
                code="unsupported_for_provider",
            )
        return resolved

    async def _run_json(self, request: ClaudeTextRequest, request_id: str) -> dict[str, Any]:
        command = self._build_command(request, stream=False)
        self._log_command(request_id, request.public_model, command, request.prompt)
        async with self._semaphore:
            with tempfile.TemporaryDirectory(prefix="airelays-claude-") as workdir:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=workdir,
                        env=self._subprocess_env(),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except FileNotFoundError as exc:
                    raise ProviderError(
                        503,
                        f"Claude CLI not found at `{self._settings.claude_bin}`.",
                        code="provider_unavailable",
                    ) from exc
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(request.prompt.encode("utf-8")),
                        timeout=self._settings.claude_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    process.kill()
                    await process.wait()
                    raise ProviderError(
                        504,
                        "Claude CLI timed out while generating a response.",
                        code="provider_timeout",
                    ) from exc
        self._log_result(request_id, stdout)
        if process.returncode != 0:
            # The CLI often exits nonzero with the real error (e.g. a 401
            # for an invalid token) as JSON on stdout; prefer that message
            # over the opaque "exited with code N" stderr fallback.
            try:
                parsed = json.loads(stdout.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("result"):
                raise ProviderError(502, str(parsed["result"]), code="provider_failure")
            raise ProviderError(502, _stderr_message(stderr, process.returncode), code="provider_failure")
        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(502, "Claude CLI returned invalid JSON.", code="provider_failure") from exc
        if parsed.get("subtype") == "error" or parsed.get("is_error") is True:
            raise ProviderError(502, str(parsed.get("result") or "Claude CLI returned an error."), code="provider_failure")
        return parsed

    async def _run_stream(
        self,
        request: ClaudeTextRequest,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        command = self._build_command(request, stream=True)
        self._log_command(request_id, request.public_model, command, request.prompt)
        async with self._semaphore:
            with tempfile.TemporaryDirectory(prefix="airelays-claude-") as workdir:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=workdir,
                        env=self._subprocess_env(),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except FileNotFoundError as exc:
                    raise ProviderError(
                        503,
                        f"Claude CLI not found at `{self._settings.claude_bin}`.",
                        code="provider_unavailable",
                    ) from exc

                assert process.stdin is not None
                process.stdin.write(request.prompt.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()

                assert process.stdout is not None
                try:
                    while True:
                        raw_line = await asyncio.wait_for(
                            process.stdout.readline(),
                            timeout=self._settings.claude_timeout_seconds,
                        )
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        self._log_stream_line(request_id, line)
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
                except asyncio.TimeoutError as exc:
                    process.kill()
                    await process.wait()
                    raise ProviderError(
                        504,
                        "Claude CLI timed out while streaming a response.",
                        code="provider_timeout",
                    ) from exc
                stderr = await process.stderr.read() if process.stderr is not None else b""
                returncode = await process.wait()
                if returncode != 0:
                    raise ProviderError(502, _stderr_message(stderr, returncode), code="provider_failure")

    def _build_command(self, request: ClaudeTextRequest, *, stream: bool) -> list[str]:
        command = [
            self._settings.claude_bin,
            "-p",
            "--model",
            request.upstream_model,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        if request.system_prompt:
            command.extend(["--system-prompt", request.system_prompt])
        if request.effort:
            command.extend(["--effort", request.effort])
        if stream:
            command.extend(["--output-format", "stream-json", "--include-partial-messages", "--verbose"])
        else:
            command.extend(["--output-format", "json"])
        return command

    def _subprocess_env(self) -> dict[str, str]:
        allowed = (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "TMPDIR",
            "TMP",
            "TEMP",
            "USER",
            "LOGNAME",
            "SHELL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        )
        env = {name: value for name, value in os.environ.items() if name in allowed}
        # A stored token file beats ambient env: it is the only mechanism
        # that survives service managers (systemd, launchd, docker) where
        # shell exports never reach the relay process.
        stored_token = self._settings.resolve_claude_oauth_token()
        if stored_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = stored_token
        if self._settings.claude_strip_api_key_env:
            for name in (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
            ):
                env.pop(name, None)
        return env

    def _cached_probe(self) -> dict[str, Any]:
        """Stale-while-revalidate CLI probe. The synchronous probe spawns
        the Node CLI twice (seconds under load); running it in the request
        path blocked the event loop, stalled every in-flight request, and
        made the desktop's health poll time out — the relay looked down
        while it was serving traffic. Status calls now always return
        immediately; a stale cache triggers one background refresh."""
        now = time.monotonic()
        with self._probe_lock:
            if self._last_probe is None:
                # First call: pay the probe cost once, synchronously — a
                # one-shot CLI command (doctor, status) needs a truthful
                # answer, and one startup probe is harmless.
                probe = self._run_status_command()
                self._probe_cached_at = time.monotonic()
                return probe
            fresh = now - self._probe_cached_at < 30.0
            if fresh or self._probe_refreshing:
                return self._last_probe
            self._probe_refreshing = True

        def refresh() -> None:
            try:
                self._run_status_command()  # assigns self._last_probe
            finally:
                with self._probe_lock:
                    self._probe_cached_at = time.monotonic()
                    self._probe_refreshing = False

        threading.Thread(target=refresh, name="claude-probe", daemon=True).start()
        return self._last_probe

    # ----- subscription usage (same normalized shape as OpenAI) -----

    async def get_subscription_status(self, request_id: str) -> dict[str, Any]:
        """Subscription usage normalized to the exact shape the OpenAI
        runtime produces, so every consumer renders both providers with one
        code path. Source: the endpoint Claude Code's own `/usage` command
        calls (undocumented; degraded gracefully when it changes)."""
        del request_id
        now = time.monotonic()
        if self._usage_cache is not None and now - self._usage_cache_at < CLAUDE_USAGE_CACHE_SECONDS:
            return json.loads(json.dumps(self._usage_cache))
        # Inside a rate-limit window: never poke the upstream again (that
        # can extend the lockout). Serve honest stale data when we have it.
        if now < self._usage_blocked_until:
            return self._stale_or_usage_error(now, "rate_limited")
        async with self._usage_fetch_lock:
            # Re-check after acquiring: a concurrent caller may have just
            # refilled the cache or hit a 429 while this one waited.
            now = time.monotonic()
            if (
                self._usage_cache is not None
                and now - self._usage_cache_at < CLAUDE_USAGE_CACHE_SECONDS
            ):
                return json.loads(json.dumps(self._usage_cache))
            if now < self._usage_blocked_until:
                return self._stale_or_usage_error(now, "rate_limited")
            return await self._fetch_usage()

    async def _fetch_usage(self) -> dict[str, Any]:
        # Token resolution can shell out to the macOS keychain — never on
        # the event loop.
        token, source = await asyncio.to_thread(self._resolve_usage_token)
        now = time.monotonic()
        if not token:
            if source == "expired":
                # Signed in, but the access token lapsed between rotations.
                # The CLI mints a fresh one on its next served request; this
                # is not a sign-in problem, so never say "sign in".
                return self._stale_or_usage_error(now, "credential_rejected")
            raise ProviderError(
                503,
                "No Claude sign-in found. Sign in with `claude auth login`, "
                "or store a token with `airelays claude set-token`.",
                code="provider_unavailable",
            )
        # A token the upstream already rejected stays rejected: retrying it
        # burns rate budget for a guaranteed failure. A CLI-owned token
        # clears itself the moment the CLI rotates it (its next real
        # request, including relay-served ones); a stored-file token is
        # static, so recovery there needs the user to replace/remove it.
        if _token_fingerprint(token) == self._usage_rejected_fingerprint:
            return self._stale_or_usage_error(now, "credential_rejected", source=source)
        # Attempt spacing: whatever the outcome class (network error, 5xx),
        # never hit the upstream more than once a minute.
        if now - self._usage_last_attempt_at < CLAUDE_USAGE_MIN_ATTEMPT_INTERVAL:
            return self._stale_or_usage_error(now, "cooling_down")
        self._usage_last_attempt_at = now
        self._save_usage_state()
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            # Without a claude-code User-Agent the endpoint answers from an
            # aggressively rate-limited bucket (persistent 429s).
            "User-Agent": f"claude-code/{(self._last_probe or {}).get('version') or '2.0.0'}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(CLAUDE_USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(
                502,
                f"The Claude usage endpoint is unreachable: {exc}",
                code="provider_failure",
            ) from exc
        if response.status_code == 429:
            # Fresh monotonic: the timestamps above predate the keychain
            # shell-out and the HTTP round-trip (up to ~25s), which would
            # under-wait the window and re-poke a still-limited endpoint.
            arrived = time.monotonic()
            self._usage_blocked_until = arrived + _retry_after_seconds(
                response.headers.get("retry-after")
            )
            self._save_usage_state()
            return self._stale_or_usage_error(arrived, "rate_limited")
        if response.status_code in (401, 403):
            self._usage_rejected_fingerprint = _token_fingerprint(token)
            self._usage_rejected_source = source
            self._save_usage_state()
            return self._stale_or_usage_error(time.monotonic(), "credential_rejected", source=source)
        if response.status_code >= 400:
            raise ProviderError(
                502,
                f"The Claude usage endpoint answered {response.status_code}.",
                code="provider_failure",
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(
                502, "The Claude usage endpoint returned a non-JSON payload.", code="provider_failure"
            ) from exc
        normalized = self._normalize_usage(payload if isinstance(payload, dict) else {})
        self._usage_cache = normalized
        self._usage_cache_at = time.monotonic()
        self._usage_last_good = normalized
        self._usage_last_good_epoch = time.time()
        self._usage_blocked_until = 0.0
        self._usage_rejected_fingerprint = None
        self._usage_rejected_source = None
        self._save_usage_state()
        return json.loads(json.dumps(normalized))

    def _stale_or_usage_error(
        self, now: float, reason: str, source: str | None = None
    ) -> dict[str, Any]:
        """Usage is temporarily unobtainable (rate-limited, dead credential
        awaiting rotation, or attempt spacing): the last good snapshot
        annotated as stale, or an explicit error when none exists yet.
        Callers can rely on `stale`/`stale_reason`/`as_of_epoch`/
        `retry_after_seconds` to present the state honestly."""
        retry_after = max(0, int(self._usage_blocked_until - now))
        # A stored-file token is static: CLI rotation can't fix it, so the
        # recovery guidance differs from a CLI-owned credential.
        file_sourced = source == "file"
        if self._usage_last_good is not None:
            stale = json.loads(json.dumps(self._usage_last_good))
            _refresh_stale_windows(stale)
            stale["stale"] = True
            stale["stale_reason"] = reason
            stale["as_of_epoch"] = int(self._usage_last_good_epoch)
            if reason == "credential_rejected" and file_sourced:
                stale["stale_reason"] = "credential_rejected_file"
            if reason == "rate_limited" and retry_after:
                stale["retry_after_seconds"] = retry_after
            return stale
        if reason == "credential_rejected":
            if file_sourced:
                raise ProviderError(
                    503,
                    "The stored Claude token was rejected. Replace it with "
                    "`airelays claude set-token`, or remove it to fall back to "
                    "the claude CLI's own sign-in. Requests are unaffected.",
                    code="provider_rate_limited",
                )
            raise ProviderError(
                503,
                "Claude's usage credential renews automatically on the claude "
                "CLI's next request; usage will appear then. Requests are "
                "unaffected.",
                code="provider_rate_limited",
            )
        minutes = max(1, retry_after // 60)
        raise ProviderError(
            503,
            f"Claude rate-limits its usage endpoint; retrying in ~{minutes}m. "
            "Requests are unaffected.",
            code="provider_rate_limited",
        )

    # ----- usage guardrail persistence -----
    # The block window, rejected-token fingerprint, and last snapshot are
    # persisted so a relay restart can never turn into a fresh poke at a
    # locked-out endpoint (restart-loops were exactly how the hour-long
    # lockouts were earned).

    def _load_usage_state(self) -> None:
        try:
            state = json.loads(self._usage_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(state, dict):
            return
        blocked_epoch = state.get("blocked_until_epoch")
        if isinstance(blocked_epoch, (int, float)):
            remaining = blocked_epoch - time.time()
            if remaining > 0:
                self._usage_blocked_until = time.monotonic() + min(remaining, 7200)
        fingerprint = state.get("rejected_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            self._usage_rejected_fingerprint = fingerprint
        rejected_source = state.get("rejected_source")
        if isinstance(rejected_source, str) and rejected_source:
            self._usage_rejected_source = rejected_source
        attempt_epoch = state.get("last_attempt_epoch")
        if isinstance(attempt_epoch, (int, float)):
            elapsed = time.time() - attempt_epoch
            if 0 <= elapsed < CLAUDE_USAGE_MIN_ATTEMPT_INTERVAL:
                # Map remaining wall-clock spacing onto monotonic so a
                # restart loop can't poke once per restart.
                self._usage_last_attempt_at = time.monotonic() - elapsed
        last_good = state.get("last_good")
        last_good_epoch = state.get("last_good_epoch")
        if isinstance(last_good, dict) and isinstance(last_good_epoch, (int, float)):
            # Snapshots older than a day describe windows that have long
            # since rolled over; showing them helps nobody.
            if time.time() - last_good_epoch < 86400:
                self._usage_last_good = last_good
                self._usage_last_good_epoch = float(last_good_epoch)

    def _save_usage_state(self) -> None:
        mono = time.monotonic()
        remaining = max(0.0, self._usage_blocked_until - mono)
        attempt_ago = max(0.0, mono - self._usage_last_attempt_at)
        state = {
            "blocked_until_epoch": time.time() + remaining if remaining else 0,
            "rejected_fingerprint": self._usage_rejected_fingerprint,
            "rejected_source": self._usage_rejected_source,
            "last_attempt_epoch": time.time() - attempt_ago,
            "last_good": self._usage_last_good,
            "last_good_epoch": self._usage_last_good_epoch,
        }
        try:
            self._usage_state_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a second relay sharing data_dir must never read
            # a half-written state file (a torn read would fail open and poke
            # a locked endpoint).
            tmp = self._usage_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
            os.replace(tmp, self._usage_state_path)
        except OSError:
            pass  # best-effort: guardrails still hold for this process

    def _resolve_usage_token(self) -> tuple[str | None, str]:
        """OAuth access token for the usage endpoint plus its source, in the
        same precedence the request path uses: stored file, then the
        `CLAUDE_CODE_OAUTH_TOKEN` env var, then the claude CLI's own stores
        (macOS keychain, ~/.claude/.credentials.json).

        Returns ``(token, source)``. When no usable token is found the source
        is ``"expired"`` if a CLI-owned credential exists but its access
        token has lapsed between rotations (renews on the CLI's next
        request), or ``"none"`` if there is genuinely no sign-in."""
        stored = self._settings.resolve_claude_oauth_token()
        if stored:
            return stored, "file"
        env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if env_token:
            return env_token, "env"
        saw_expired = False
        if sys.platform == "darwin":
            try:
                probe = subprocess.run(
                    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if probe.returncode == 0:
                    payload = json.loads(probe.stdout.strip())
                    token = _fresh_oauth_access_token(payload)
                    if token:
                        return token, "keychain"
                    saw_expired = saw_expired or _has_oauth_access_token(payload)
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass
        credentials = Path.home() / ".claude" / ".credentials.json"
        try:
            payload = json.loads(credentials.read_text(encoding="utf-8"))
            token = _fresh_oauth_access_token(payload)
            if token:
                return token, "credentials"
            saw_expired = saw_expired or _has_oauth_access_token(payload)
        except (OSError, json.JSONDecodeError):
            pass
        return None, ("expired" if saw_expired else "none")

    def _normalize_usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        probe = self._last_probe or {}
        primary = _claude_usage_window(payload.get("five_hour"), CLAUDE_FIVE_HOUR_SECONDS)
        secondary = _claude_usage_window(payload.get("seven_day"), CLAUDE_SEVEN_DAY_SECONDS)
        additional = []
        for key, label in (("seven_day_sonnet", "Sonnet"), ("seven_day_opus", "Opus")):
            window = _claude_usage_window(payload.get(key), CLAUDE_SEVEN_DAY_SECONDS)
            if window is not None:
                additional.append(
                    {
                        "limit_name": label,
                        "metered_feature": None,
                        "rate_limit": {
                            "allowed": None,
                            "limit_reached": window["used_percent"] >= 100,
                            "primary_window": window,
                            "secondary_window": None,
                        },
                    }
                )
        reached_type = None
        if primary is not None and primary["used_percent"] >= 100:
            reached_type = "five_hour"
        elif secondary is not None and secondary["used_percent"] >= 100:
            reached_type = "seven_day"
        return {
            "object": "subscription_status",
            "provider": "claude",
            "account": {
                "email": probe.get("email"),
                "plan_type": probe.get("subscription_type"),
            },
            "rate_limit_reached_type": reached_type,
            "rate_limits": {
                "default": {
                    "allowed": None,
                    "limit_reached": reached_type is not None,
                    "primary_window": primary,
                    "secondary_window": secondary,
                },
                "additional": additional,
            },
        }

    def _run_status_command(self) -> dict[str, Any]:
        installed = True
        version = None
        status_payload: dict[str, Any] | None = None
        try:
            version = self._status_version_probe()
        except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired):
            installed = False
        if installed:
            try:
                status_payload = self._status_auth_probe()
            except (RuntimeError, subprocess.TimeoutExpired):
                status_payload = None
            except FileNotFoundError:
                installed = False
        probe = {
            "installed": installed,
            "version": version,
            "logged_in": bool(status_payload and status_payload.get("loggedIn")),
            "auth_method": status_payload.get("authMethod") if status_payload else None,
            "api_provider": status_payload.get("apiProvider") if status_payload else None,
            "email": status_payload.get("email") if status_payload else None,
            "subscription_type": status_payload.get("subscriptionType") if status_payload else None,
        }
        self._last_probe = probe
        return probe

    def _status_version_probe(self) -> str | None:
        process = subprocess.run(
            [self._settings.claude_bin, "--version"],
            capture_output=True,
            env=self._subprocess_env(),
            check=False,
            text=True,
            timeout=5,
        )
        if process.returncode != 0:
            return None
        text = process.stdout.strip()
        return text or None

    def _status_auth_probe(self) -> dict[str, Any] | None:
        process = subprocess.run(
            [self._settings.claude_bin, "auth", "status", "--json"],
            capture_output=True,
            env=self._subprocess_env(),
            check=False,
            text=True,
            timeout=5,
        )
        if process.returncode != 0:
            return None
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _log_command(self, request_id: str, model: str, command: list[str], prompt: str) -> None:
        if self._traffic is None:
            return
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_request",
                "provider": "claude",
                "model": model,
                "command": command,
                "body": snapshot_body("text/plain; charset=utf-8", prompt.encode("utf-8")),
            }
        )

    def _log_result(self, request_id: str, raw: bytes) -> None:
        if self._traffic is None:
            return
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_response",
                "provider": "claude",
                "body": snapshot_body("application/json", raw),
            }
        )

    def _log_stream_line(self, request_id: str, line: str) -> None:
        # Same opt-in as the OpenAI per-line stream logging: one streamed
        # Claude response is hundreds of lines, which floods the traffic
        # log and evicts real request records from every reader's window.
        if self._traffic is None or not self._settings.log_stream_lines:
            return
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_stream_line",
                "provider": "claude",
                "line": line,
            }
        )


def _refresh_stale_windows(snapshot: dict[str, Any]) -> None:
    """Re-derives the time-dependent fields of a cached usage snapshot.
    `reset_after_seconds` was computed at fetch time and would otherwise be
    served frozen for up to two hours; a window whose reset has passed also
    no longer justifies an "at limit" state."""
    wall_now = int(datetime.now(timezone.utc).timestamp())
    windows: list[dict[str, Any]] = []
    limits = snapshot.get("rate_limits") or {}
    default = limits.get("default") or {}
    for key in ("primary_window", "secondary_window"):
        if isinstance(default.get(key), dict):
            windows.append(default[key])
    for extra in limits.get("additional") or []:
        rate = (extra or {}).get("rate_limit") or {}
        for key in ("primary_window", "secondary_window"):
            if isinstance(rate.get(key), dict):
                windows.append(rate[key])

    any_still_limited = False
    for window in windows:
        reset_at = window.get("reset_at")
        if isinstance(reset_at, (int, float)) and reset_at > 0:
            remaining = max(0, int(reset_at) - wall_now)
            window["reset_after_seconds"] = remaining
            if remaining == 0:
                # The window rolled over while we were locked out: its
                # percentages are unknown but a reached limit is certainly
                # gone.
                window["used_percent"] = None
                window["remaining_percent"] = None
        used = window.get("used_percent")
        if isinstance(used, (int, float)) and used >= 100:
            any_still_limited = True
    if not any_still_limited:
        snapshot["rate_limit_reached_type"] = None
        if isinstance(default, dict) and default:
            default["limit_reached"] = False


def _token_fingerprint(token: str) -> str:
    """Stable non-reversible identity for a credential, safe to persist."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _retry_after_seconds(header: str | None) -> int:
    """Upstream retry-after header → a sane wait. Missing or malformed
    values default to the endpoint's observed window (1h); the clamp keeps
    a hostile or buggy header (including inf/overflow) from wedging usage
    reporting for days or turning it into a hammer."""
    try:
        seconds = int(float(header)) if header else 3600
    except (ValueError, OverflowError):
        seconds = 3600
    return max(60, min(seconds, 7200))


def _has_oauth_access_token(payload: Any) -> bool:
    """True when a claude CLI credential payload carries an access token at
    all — used to tell "signed in but token lapsed between rotations" from
    "no sign-in", which need different messages."""
    return bool(
        isinstance(payload, dict)
        and isinstance(payload.get("claudeAiOauth"), dict)
        and payload["claudeAiOauth"].get("accessToken")
    )


def _fresh_oauth_access_token(payload: Any) -> str | None:
    """Access token from a claude CLI credential payload, skipping tokens
    the payload itself declares expired (they only earn a confusing 401
    from the usage endpoint; the CLI refreshes them on its next own run)."""
    if not isinstance(payload, dict):
        return None
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not token:
        return None
    expires_at_ms = oauth.get("expiresAt")
    if isinstance(expires_at_ms, (int, float)) and expires_at_ms > 0:
        if expires_at_ms / 1000.0 <= datetime.now(timezone.utc).timestamp():
            return None
    return str(token)


def _claude_usage_window(bucket: Any, window_seconds: int) -> dict[str, Any] | None:
    """One usage bucket → the same window shape transforms.py produces for
    OpenAI, so the UI's single renderer covers both providers."""
    if not isinstance(bucket, dict):
        return None
    used_raw = bucket.get("utilization")
    used = float(used_raw) if isinstance(used_raw, (int, float)) else 0.0
    used = max(0.0, min(100.0, used))
    resets_at_iso = bucket.get("resets_at")
    reset_at: int | None = None
    reset_after_seconds: int | None = None
    if isinstance(resets_at_iso, str) and resets_at_iso:
        try:
            parsed = datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
            reset_at = int(parsed.timestamp())
            reset_after_seconds = max(0, reset_at - int(datetime.now(timezone.utc).timestamp()))
        except ValueError:
            pass
    return {
        "used_percent": used,
        "remaining_percent": round(100.0 - used, 2),
        "window_seconds": window_seconds,
        "window_minutes": window_seconds // 60,
        "window_label": "weekly" if window_seconds == CLAUDE_SEVEN_DAY_SECONDS else "5h",
        "reset_after_seconds": reset_after_seconds,
        "reset_at": reset_at,
        "reset_at_iso": resets_at_iso if isinstance(resets_at_iso, str) else None,
    }


class ProviderRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        openai_auth: AuthManager,
        openai_backend: Any = None,
        traffic: TrafficLogger | None = None,
        account_pool: Any = None,
    ) -> None:
        self._settings = settings
        self._openai_auth = openai_auth
        self._openai_backend = openai_backend
        self._traffic = traffic
        # Optional multi-account pool; when present, provider status lists
        # every enrolled account.
        self._account_pool = account_pool
        self._openai_models_cache_payload: dict[str, Any] | None = None
        self._openai_models_cache_fetched_at: float | None = None
        self._openai_models_cache_key: tuple[str | None, ...] | None = None
        self._openai_models_cache_lock = asyncio.Lock()
        self._claude = (
            ClaudeCliRuntime(settings, traffic)
            if settings.enable_claude
            else None
        )

    @property
    def claude_runtime(self) -> ClaudeCliRuntime | None:
        return self._claude

    def resolve_model(self, model_id: str) -> ResolvedModel:
        if self._claude is not None:
            resolved = self._claude.resolve_model(model_id)
            if resolved is not None:
                return resolved
            if model_id.startswith("claude:") or model_id.startswith("claude-"):
                raise ProviderError(
                    422,
                    f"Unknown Claude model `{model_id}`. Configure it under `[providers.claude].models` first.",
                    code="unsupported_for_provider",
                )
        elif model_id.startswith("claude:") or model_id.startswith("claude-"):
            raise ProviderError(
                422,
                "The Claude runtime is disabled for this AIRelays process.",
                code="unsupported_for_provider",
            )
        if self._settings.enable_openai_provider:
            return ResolvedModel(provider="openai", public_id=model_id, upstream_id=model_id)
        raise ProviderError(
            422,
            f"Unknown model `{model_id}` and the OpenAI runtime is disabled.",
            code="unsupported_for_provider",
        )

    async def list_models(self, request_id: str) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        openai_error: Exception | None = None
        if self._settings.enable_openai_provider and self._openai_backend is not None:
            try:
                payload = await self._openai_models_payload(request_id)
                models = payload.get("models")
                if isinstance(models, list):
                    for item in models:
                        if not isinstance(item, dict):
                            continue
                        slug = item.get("slug")
                        if isinstance(slug, str) and slug:
                            data.append(_openai_model_record(slug).as_wire())
            except Exception as exc:  # noqa: BLE001
                openai_error = exc
            # The upstream catalog lags what the backend actually serves
            # (verified live: ids like gpt-5.6-sol answer requests while the
            # catalog omits them). Configured extra ids are advertised so
            # list-driven clients can discover them; deduped in case the
            # catalog catches up later. Only on a successful catalog fetch —
            # extras extend a working runtime, they must never mask an
            # upstream auth or availability error.
            if openai_error is None:
                listed = {item["id"] for item in data}
                for model_id in self._settings.openai_extra_models:
                    if model_id and model_id not in listed:
                        data.append(_openai_model_record(model_id).as_wire())
        if self._claude is not None:
            data.extend(self._claude.list_models())
        if data:
            return {"object": "list", "data": data}
        if openai_error is not None:
            raise openai_error
        return {"object": "list", "data": []}

    def _models_cache_ttl_seconds(self) -> float:
        return max(0.0, float(self._settings.models_cache_ttl_seconds))

    def _current_openai_models_cache_key(self) -> tuple[str | None, ...] | None:
        record = self._openai_auth.load()
        if record is None or not record.authenticated or not record.account_matches_binding():
            return None
        return (
            self._settings.upstream_base_url,
            self._settings.client_version,
            record.account_id,
            record.bound_account_id,
        )

    def _clear_openai_models_cache(self) -> None:
        self._openai_models_cache_payload = None
        self._openai_models_cache_fetched_at = None
        self._openai_models_cache_key = None

    def _cached_openai_models_payload(
        self, now: float, cache_key: tuple[str | None, ...] | None
    ) -> dict[str, Any] | None:
        ttl = self._models_cache_ttl_seconds()
        if ttl <= 0:
            return None
        if cache_key is None:
            self._clear_openai_models_cache()
            return None
        if self._openai_models_cache_payload is None or self._openai_models_cache_fetched_at is None:
            return None
        if self._openai_models_cache_key != cache_key:
            self._clear_openai_models_cache()
            return None
        if now - self._openai_models_cache_fetched_at >= ttl:
            return None
        return self._openai_models_cache_payload

    async def _openai_models_payload(self, request_id: str) -> dict[str, Any]:
        if self._openai_backend is None:
            return {"models": []}
        now = time.monotonic()
        cache_key = self._current_openai_models_cache_key()
        cached = self._cached_openai_models_payload(now, cache_key)
        if cached is not None:
            self._log_openai_models_cache(request_id, "hit", now)
            return cached

        ttl = self._models_cache_ttl_seconds()
        if ttl <= 0:
            self._log_openai_models_cache(request_id, "disabled", now)
            return await self._openai_backend.list_models(request_id)

        self._log_openai_models_cache(request_id, "miss", now)
        async with self._openai_models_cache_lock:
            now = time.monotonic()
            cache_key = self._current_openai_models_cache_key()
            cached = self._cached_openai_models_payload(now, cache_key)
            if cached is not None:
                self._log_openai_models_cache(request_id, "hit", now)
                return cached
            payload = await self._openai_backend.list_models(request_id)
            models = payload.get("models")
            cache_key = self._current_openai_models_cache_key()
            if isinstance(models, list) and cache_key is not None:
                self._openai_models_cache_payload = payload
                self._openai_models_cache_fetched_at = time.monotonic()
                self._openai_models_cache_key = cache_key
                self._log_openai_models_cache(
                    request_id, "refresh", self._openai_models_cache_fetched_at
                )
            return payload

    def openai_models_cache_status(self) -> dict[str, Any]:
        ttl = self._models_cache_ttl_seconds()
        configured = ttl > 0
        enabled = self._settings.enable_openai_provider and configured
        status: dict[str, Any] = {
            "configured": configured,
            "enabled": enabled,
            "ttl_seconds": ttl,
        }
        if not self._settings.enable_openai_provider:
            status.update(
                {
                    "state": "provider_disabled",
                    "age_seconds": None,
                    "expires_in_seconds": None,
                    "cached_model_count": 0,
                }
            )
            return status
        payload = self._openai_models_cache_payload
        fetched_at = self._openai_models_cache_fetched_at
        if payload is None or fetched_at is None:
            status.update(
                {
                    "state": "disabled" if not configured else "empty",
                    "age_seconds": None,
                    "expires_in_seconds": None,
                    "cached_model_count": 0,
                }
            )
            return status

        age = max(0.0, time.monotonic() - fetched_at)
        expires_in = max(0.0, ttl - age) if enabled else 0.0
        models = payload.get("models")
        status.update(
            {
                "state": "fresh" if enabled and age < ttl else "expired",
                "age_seconds": round(age, 3),
                "expires_in_seconds": round(expires_in, 3) if enabled else 0.0,
                "cached_model_count": len(models) if isinstance(models, list) else 0,
            }
        )
        return status

    def _log_openai_models_cache(self, request_id: str, cache_state: str, now: float) -> None:
        if self._traffic is None:
            return
        status = self.openai_models_cache_status()
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_models_cache",
                "provider": "openai",
                "cache": cache_state,
                "ttl_seconds": status["ttl_seconds"],
                "age_seconds": status["age_seconds"],
                "expires_in_seconds": status["expires_in_seconds"],
                "cached_model_count": status["cached_model_count"],
                "monotonic_time": round(now, 3),
            }
        )

    def provider_statuses(self) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        openai_status = {
            "enabled": self._settings.enable_openai_provider,
            "models_cache": self.openai_models_cache_status(),
            **self._openai_auth.status(),
        }
        if not self._settings.enable_openai_provider:
            openai_status["ready_for_requests"] = False
        if self._account_pool is not None:
            self._account_pool.refresh_if_changed()
        if self._account_pool is not None and self._account_pool.size > 1:
            openai_status["accounts"] = self._account_pool.account_statuses()
            openai_status["balance"] = self._settings.openai_balance
            openai_status["ready_for_requests"] = openai_status["ready_for_requests"] or any(
                account.get("ready_for_requests") for account in openai_status["accounts"]
            )
        elif self._account_pool is not None and self._account_pool.size == 1:
            # Single-account installs get no accounts array, but the token
            # breakdown behind the usage bars must not vanish with it.
            statuses = self._account_pool.account_statuses()
            if statuses and "window_tokens" in statuses[0]:
                openai_status["window_tokens"] = statuses[0]["window_tokens"]
        providers["openai"] = openai_status
        if self._claude is not None:
            providers["claude"] = self._claude.status()
        else:
            providers["claude"] = {
                "enabled": False,
                "ready_for_requests": False,
                "notes": [
                    "The Claude runtime is disabled for this AIRelays process. Set `[providers.claude].enabled = true` or `AIRELAYS_ENABLE_CLAUDE=true` to enable it."
                ],
            }
        return providers

    @property
    def claude(self) -> ClaudeCliRuntime | None:
        return self._claude
