from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

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


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    provider: str
    owned_by: str
    upstream_id: str
    experimental: bool
    routes: dict[str, bool]
    stateful_conversations: bool

    def as_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": self.owned_by,
            "airelays": {
                "provider": self.provider,
                "upstream_model": self.upstream_id,
                "experimental": self.experimental,
                "capabilities": {
                    "routes": self.routes,
                    "stateful_conversations": self.stateful_conversations,
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


def _openai_model_record(model_id: str) -> ProviderModel:
    return ProviderModel(
        id=model_id,
        provider="openai",
        owned_by="airelays-openai-subscription",
        upstream_id=model_id,
        experimental=False,
        routes={
            "responses": True,
            "chat_completions": True,
            "completions": True,
            "files": True,
            "conversations": True,
            "subscription_status": True,
        },
        stateful_conversations=True,
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
            "Claude experimental mode supports only string or text-part message content.",
            code="unsupported_for_provider",
        )
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise ProviderError(
                422,
                "Claude experimental mode supports only text content parts.",
                code="unsupported_for_provider",
            )
        kind = part.get("type")
        if kind not in {"text", "input_text", "output_text"}:
            raise ProviderError(
                422,
                f"Claude experimental mode does not support message content part `{kind}`.",
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
                "Claude experimental mode supports only system, developer, user, and assistant messages.",
                code="unsupported_for_provider",
            )
        if message.get("tool_calls"):
            raise ProviderError(
                422,
                "Claude experimental mode does not support tool calls.",
                code="unsupported_for_provider",
            )
        turns.append({"role": role, "text": _content_text(message.get("content"))})
    if not turns:
        raise ProviderError(
            422,
            "Claude experimental mode requires at least one user or assistant turn.",
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


class ClaudeCliRuntime:
    def __init__(self, settings: Settings, traffic: TrafficLogger | None = None) -> None:
        self._settings = settings
        self._traffic = traffic
        self._semaphore = asyncio.Semaphore(settings.claude_max_concurrent_requests)
        self._models = self._build_models(settings.claude_models)

    def _build_models(self, configured: tuple[str, ...]) -> dict[str, ProviderModel]:
        records: dict[str, ProviderModel] = {}
        for model_id in configured:
            upstream_id = model_id.split(":", 1)[1] if ":" in model_id else model_id
            record = ProviderModel(
                id=model_id,
                provider="claude",
                owned_by="airelays-claude-experimental",
                upstream_id=upstream_id,
                experimental=True,
                routes=_claude_routes(),
                stateful_conversations=False,
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
        probe = self._run_status_command()
        ready = bool(probe.get("installed") and probe.get("logged_in"))
        return {
            "enabled": True,
            "experimental": True,
            "local_only": True,
            "requires_relay_bearer_auth": True,
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
            "notes": [
                "Use `claude auth login --claudeai` for browser-based local login.",
                "For headless environments, generate a token with `claude setup-token` and export `CLAUDE_CODE_OAUTH_TOKEN` before launching AIRelays.",
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
            raise ProviderError(422, "Claude experimental mode supports only `n=1`.", code="unsupported_for_provider")
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
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "max_completion_tokens",
        ):
            if _provided(body.get(field)):
                raise ProviderError(
                    422,
                    f"Claude experimental mode does not support `{field}` on `/v1/chat/completions`.",
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
        )

    def _prepare_completion_request(self, body: dict[str, Any]) -> ClaudeTextRequest:
        resolved = self._resolved_model_from_body(body)
        if body.get("n") not in {None, 1}:
            raise ProviderError(422, "Claude experimental mode supports only `n=1`.", code="unsupported_for_provider")
        for field in (
            "best_of",
            "echo",
            "logprobs",
            "suffix",
            "conversation",
            "store",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "max_tokens",
            "stop",
        ):
            if _provided(body.get(field)):
                raise ProviderError(
                    422,
                    f"Claude experimental mode does not support `{field}` on `/v1/completions`.",
                    code="unsupported_for_provider",
                )
        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            if len(prompt) != 1 or not isinstance(prompt[0], str):
                raise ProviderError(
                    422,
                    "Claude experimental mode supports only a single string prompt.",
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

    def _run_status_command(self) -> dict[str, Any]:
        installed = True
        version = None
        status_payload: dict[str, Any] | None = None
        try:
            version = self._status_version_probe()
        except RuntimeError:
            installed = False
        except FileNotFoundError:
            installed = False
        if installed:
            try:
                status_payload = self._status_auth_probe()
            except RuntimeError:
                status_payload = None
            except FileNotFoundError:
                installed = False
        return {
            "installed": installed,
            "version": version,
            "logged_in": bool(status_payload and status_payload.get("loggedIn")),
            "auth_method": status_payload.get("authMethod") if status_payload else None,
            "api_provider": status_payload.get("apiProvider") if status_payload else None,
            "email": status_payload.get("email") if status_payload else None,
            "subscription_type": status_payload.get("subscriptionType") if status_payload else None,
        }

    def _status_version_probe(self) -> str | None:
        process = subprocess.run(
            [self._settings.claude_bin, "--version"],
            capture_output=True,
            env=self._subprocess_env(),
            check=False,
            text=True,
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
        if self._traffic is None:
            return
        self._traffic.write(
            {
                "request_id": request_id,
                "phase": "provider_stream_line",
                "provider": "claude",
                "line": line,
            }
        )


class ProviderRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        openai_auth: AuthManager,
        openai_backend: ChatGptCodexBackend | None = None,
        traffic: TrafficLogger | None = None,
    ) -> None:
        self._settings = settings
        self._openai_auth = openai_auth
        self._openai_backend = openai_backend
        self._traffic = traffic
        self._openai_models_cache_payload: dict[str, Any] | None = None
        self._openai_models_cache_fetched_at: float | None = None
        self._openai_models_cache_key: tuple[str | None, ...] | None = None
        self._openai_models_cache_lock = asyncio.Lock()
        self._claude = (
            ClaudeCliRuntime(settings, traffic)
            if settings.enable_claude_experimental
            else None
        )

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
                "Claude experimental mode is disabled for this AIRelays process.",
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
            "experimental": False,
            "models_cache": self.openai_models_cache_status(),
            **self._openai_auth.status(),
        }
        if not self._settings.enable_openai_provider:
            openai_status["ready_for_requests"] = False
        providers["openai"] = openai_status
        if self._claude is not None:
            providers["claude"] = self._claude.status()
        else:
            providers["claude"] = {
                "enabled": False,
                "experimental": True,
                "ready_for_requests": False,
                "notes": [
                    "Set `[providers.claude].enabled = true` or `AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL=true` to enable the local experimental Claude adapter."
                ],
            }
        return providers

    @property
    def claude(self) -> ClaudeCliRuntime | None:
        return self._claude
