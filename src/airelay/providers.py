from __future__ import annotations

import asyncio
import difflib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
import contextlib
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from airelay.accounts import keyring_era_sign_ins
from airelay.auth import AuthManager
from airelay.backend import ChatGptCodexBackend
from airelay.config import Settings
from airelay.claude_auth import ClaudeProfile, account_identity, token_profile
from airelay.claude_content import (
    conversation_from_chat,
    conversation_from_prompt,
    transcript_text,
    user_line,
)
from airelay import claude_tool_guard, claude_tool_stub
from airelay.claude_launch import ArgumentsTooLong, ClaudeLauncher, workdir
from airelay.errors import InvalidRequestError, RequestTooLargeError
from airelay.media import MAX_REQUEST_MEDIA_BYTES
from airelay.private_io import write_private
from airelay.traffic import TrafficLogger, snapshot_body
from airelay.transforms import (
    UNHONORED_CHAT_PARAMETERS,
    UNSUPPORTED_UPSTREAM_PARAMETERS,
    is_requested,
    output_token_limit,
    chat_completion_chunk,
    completion_chunk,
    completion_usage_from_anthropic,
    stream_include_usage,
    usage_chunk,
)

# Requested features the Claude runtime rejects (ADR 0001). Sampling
# parameters (`temperature`, `top_p`, `presence_penalty`,
# `frequency_penalty`) are not listed: the app layer strips and discloses them
# (x-airelays-ignored-parameters), the same documented adaptation the
# OpenAI runtime applies, because the local claude CLI exposes no
# equivalent controls and standard SDKs send them by default.
# `response_format` is honored on chat (mapped to the CLI's --json-schema);
# completions has no such parameter, so it is rejected there rather than
# silently ignored.
CLAUDE_CHAT_REJECTED = (
    "functions", "function_call", "store",
    "previous_response_id", "modalities", "audio", "prediction",
    "web_search_options", "moderation",
)
CLAUDE_COMPLETION_REJECTED = (
    "best_of", "echo", "logprobs", "suffix", "conversation", "store", "stop", "response_format",
)
_CLAUDE_COMMON = ("model", "stream", "stream_options", "n", "reasoning_effort", "tools", "tool_choice",
                  "max_completion_tokens", "max_tokens",
                  "parallel_tool_calls",
                  *UNSUPPORTED_UPSTREAM_PARAMETERS, *UNHONORED_CHAT_PARAMETERS)
# Everything else is dropped and disclosed by the app layer.
CLAUDE_CHAT_PARAMETERS = frozenset({*_CLAUDE_COMMON, "messages", "response_format", *CLAUDE_CHAT_REJECTED})
CLAUDE_COMPLETION_PARAMETERS = frozenset({*_CLAUDE_COMMON, "prompt", *CLAUDE_COMPLETION_REJECTED})


class ProviderError(RuntimeError):
    def __init__(self, status_code: int, detail: str, *, code: str = "provider_error",
                 retry_after_seconds: float | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.retry_after_seconds = retry_after_seconds


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
# The serialized response_format schema rides the claude CLI's argv. Cap it
# here (OpenAI itself caps schema sizes well under this); the launcher
# also rejects any command line over the OS limit with a 413.
CLAUDE_MAX_SCHEMA_BYTES = 200_000


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
    # `response_format` types honored on chat completions ("text" is always
    # accepted and therefore not listed). OpenAI models support json_schema
    # (translated for the subscription backend); Claude models support
    # json_schema and json_object (mapped to the CLI's --json-schema flag,
    # which enforces the schema natively).
    structured_output_types: tuple[str, ...] = ()
    resolved_model: str | None = None
    discovery_source: str = "configured"
    display_name: str | None = None
    account_availability: dict[str, int] | None = None
    catalog_visibility: str | None = None
    description: str | None = None

    def as_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": self.owned_by,
            "airelays": {
                "provider": self.provider,
                "upstream_model": self.upstream_id,
                "resolved_model": self.resolved_model,
                "discovery_source": self.discovery_source,
                "display_name": self.display_name,
                "account_availability": self.account_availability,
                "catalog_visibility": self.catalog_visibility,
                "description": self.description,
                "capabilities": {
                    "routes": self.routes,
                    "stateful_conversations": self.stateful_conversations,
                    # Both runtimes: function tool calls, images and documents
                    # (PDF, text) as input.
                    "tool_calls": True,
                    "inputs": ["text", "image", "document"],
                },
                "reasoning": {
                    "parameter": "reasoning_effort",
                    "modes": list(self.reasoning_modes),
                    "default": self.reasoning_default,
                },
                "structured_output": {
                    "parameter": "response_format",
                    "types": list(self.structured_output_types),
                },
            },
        }


@dataclass(frozen=True, slots=True)
class ClaudeTextRequest:
    public_model: str
    upstream_model: str
    system_prompt: str | None
    # The one stream-json user message: transcript text with images and
    # documents in place (see airelay.claude_content).
    content: tuple[dict[str, Any], ...]
    include_usage: bool
    # Reasoning depth for the CLI's --effort flag; None uses the model's
    # own adaptive default.
    effort: str | None = None
    # Serialized JSON Schema for the CLI's --json-schema flag (from
    # `response_format.json_schema`); None means plain text output.
    output_schema: str | None = None
    # `response_format: {"type": "json_object"}`: the answer must be one
    # JSON object, checked locally (see _json_object_text).
    json_mode: bool = False
    # Images and documents sent; the CLI's replay must show all of them.
    media_count: int = 0
    # The client's function tools as MCP definitions, served by
    # airelay.claude_tool_stub; Claude's calls are returned, never run.
    tools: tuple[dict[str, Any], ...] = ()
    # The client's output-token limit: each CLI model call is capped
    # (CLAUDE_CODE_MAX_OUTPUT_TOKENS) and the run stops when the first one
    # reaches it, before the CLI's own continuation calls.
    max_output_tokens: int | None = None

    @property
    def allowed_tools(self) -> frozenset[str]:
        """Tool names the CLI may report. `StructuredOutput` is the CLI's own
        `--json-schema` enforcement, which runs nothing."""
        names = {CLAUDE_TOOL_PREFIX + tool["name"] for tool in self.tools}
        return frozenset(names | ({"StructuredOutput"} if self.output_schema else set()))

    @property
    def prompt(self) -> str:
        """The transcript text, for logs (media summarised)."""
        return transcript_text(list(self.content))


def _openai_model_record(model_id: str, catalog: dict[str, Any] | None = None) -> ProviderModel:
    levels = catalog.get("supported_reasoning_levels") if catalog else None
    modes = tuple(dict.fromkeys(
        item["effort"] for item in levels
        if isinstance(item, dict) and isinstance(item.get("effort"), str) and item["effort"]
    )) if isinstance(levels, list) else OPENAI_REASONING_MODES
    default = catalog.get("default_reasoning_level", "none") if catalog else "none"
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
        reasoning_modes=modes,
        reasoning_default=default if isinstance(default, str) else None,
        structured_output_types=("json_schema", "json_object"),
        discovery_source="upstream_catalog" if catalog is not None else "configured",
        display_name=catalog.get("display_name") if catalog else None,
        account_availability=catalog.get("account_availability") if catalog else None,
        catalog_visibility=catalog.get("visibility") if catalog else None,
        description=catalog.get("description") if catalog else None,
    )


def _openai_catalog_model_ids(payload: dict[str, Any]) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in models:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        normalized = slug.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ids.append(slug)
    return ids


def _openai_supported_model_ids(
    payload: dict[str, Any],
    extra_models: tuple[str, ...],
) -> list[str]:
    ids = _openai_catalog_model_ids(payload)
    seen = {model.lower() for model in ids}
    for model_id in extra_models:
        normalized = model_id.lower()
        if model_id and normalized not in seen:
            ids.append(model_id)
            seen.add(normalized)
    return ids


def _openai_model_prefixes(model_id: str) -> list[str]:
    parts = [part for part in model_id.lower().split("-") if part]
    return ["-".join(parts[:length]) for length in range(len(parts) - 1, 1, -1)]


def _suggest_openai_models(
    requested_model: str,
    supported_models: list[str],
    *,
    limit: int = 5,
) -> list[str]:
    if not supported_models:
        return []
    lower_to_original = {model.lower(): model for model in supported_models}
    suggestions: list[str] = []
    seen: set[str] = set()
    for prefix in _openai_model_prefixes(requested_model):
        for model in sorted(supported_models):
            normalized = model.lower()
            if normalized in seen or not normalized.startswith(prefix):
                continue
            suggestions.append(model)
            seen.add(normalized)
            if len(suggestions) >= limit:
                return suggestions
    close_matches = difflib.get_close_matches(
        requested_model.lower(),
        list(lower_to_original),
        n=limit,
        cutoff=0.35,
    )
    for normalized in close_matches:
        if normalized in seen:
            continue
        suggestions.append(lower_to_original[normalized])
        seen.add(normalized)
        if len(suggestions) >= limit:
            break
    return suggestions


def _claude_routes() -> dict[str, bool]:
    return {
        "responses": True,
        "chat_completions": True,
        "completions": True,
        "files": True,
        "conversations": True,
        "subscription_status": False,
    }


def _finish_reason(stop_reason: str | None) -> str:
    # Anthropic stop reasons: a token or context limit reads as "length",
    # a refusal as "content_filter".
    if stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return "length"
    if stop_reason == "refusal":
        return "content_filter"
    return "stop"


def _claude_effort(
    body: dict[str, Any], route: str, modes: tuple[str, ...] = CLAUDE_REASONING_MODES,
) -> str | None:
    """Validated reasoning effort for the claude CLI. The CLI silently
    ignores unknown --effort values and falls back to its default, which
    would be silent degradation — so unsupported values are rejected here
    with the supported list."""
    effort = body.get("reasoning_effort")
    if effort is None:
        return None
    if isinstance(effort, str) and effort.lower() in modes:
        return effort.lower()
    supported = ", ".join(modes) or "none (omit reasoning_effort)"
    raise ProviderError(
        422,
        f"Unsupported `reasoning_effort` {effort!r} for Claude models on `{route}`. "
        f"Supported values: {supported}.",
        code="unsupported_for_provider",
    )


def _claude_output_schema(body: dict[str, Any], route: str) -> str | None:
    """Serialized JSON Schema for the CLI's --json-schema flag, built from the
    OpenAI-style `response_format`. The CLI enforces the schema natively and
    reports the conforming object in its result envelope (`structured_output`,
    with `result` carrying the same JSON as text) — verified against claude
    CLI 2.1.209. Unsupported shapes are rejected loudly instead of degrading
    to unenforced text."""
    response_format = body.get("response_format")
    if response_format is None:
        return None
    if not isinstance(response_format, dict):
        raise InvalidRequestError(f"`response_format` must be an object on `{route}`.", param="response_format")
    if not response_format:
        return None  # {} carries no request, like an absent parameter
    format_type = response_format.get("type")
    if format_type == "text":
        return None
    if format_type == "json_object":
        # Not a CLI schema: with a schema that names no fields, haiku models
        # often fill in an empty `{}` (6 of 10 live runs). JSON mode is an
        # instruction plus a local check instead (`json_mode`).
        return None
    if format_type == "json_schema":
        schema_payload = response_format.get("json_schema")
        if not isinstance(schema_payload, dict):
            raise InvalidRequestError(f"`response_format.type=json_schema` requires a `json_schema` object on "
                                      f"`{route}`.", param="response_format")
        schema = schema_payload.get("schema")
        if not isinstance(schema, dict):
            raise InvalidRequestError(f"`response_format.json_schema.schema` must be an object on `{route}`.",
                                      param="response_format")
        try:
            serialized = json.dumps(schema, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except ValueError as exc:
            raise InvalidRequestError(f"`response_format.json_schema.schema` must be valid JSON on `{route}` "
                                      "(NaN and Infinity are not representable).", param="response_format") from exc
        if len(serialized) > CLAUDE_MAX_SCHEMA_BYTES:
            # The schema travels on the CLI's argv; past the platform's
            # argument-size limit the spawn fails with a raw OS error, so
            # oversized schemas are rejected while a status code is still
            # possible.
            raise InvalidRequestError(f"`response_format` schema is too large for the local Claude CLI "
                                      f"(serialized {len(serialized)} bytes, maximum {CLAUDE_MAX_SCHEMA_BYTES}).",
                                      param="response_format")
        return serialized
    # A missing `type` on a non-empty response_format lands here too
    # (format_type None): OpenAI rejects that shape, and treating it as
    # "text" would silently drop the enforcement the client asked for.
    raise ProviderError(
        422,
        f"Unsupported `response_format.type` {format_type!r} for Claude models on `{route}`. "
        "Supported types: text, json_object, json_schema.",
        code="unsupported_for_provider",
    )


# Claude Code's built-in system prompt (an agent persona, about 3,000
# tokens) applies whenever the CLI gets none; an OpenAI-compatible request
# without a system message must not carry it, so one is always passed.
CLAUDE_MINIMAL_SYSTEM_PROMPT = "."
CLAUDE_JSON_MODE_INSTRUCTION = (
    "Respond with a single valid JSON object only: no code fences, no text before or after it."
)


def _claude_system_prompt(parts: list[str], *, json_mode: bool = False) -> str:
    text = "\n\n".join(part for part in parts if part.strip())
    if json_mode:
        text = f"{text}\n\n{CLAUDE_JSON_MODE_INSTRUCTION}" if text else CLAUDE_JSON_MODE_INSTRUCTION
    return text or CLAUDE_MINIMAL_SYSTEM_PROMPT


def _json_object_text(text: str) -> str | None:
    """The answer of a JSON-mode run if it is one JSON object, without a
    surrounding code fence; None otherwise."""
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text, parse_constant=_reject_non_json_number)
    except ValueError:
        return None
    return text if isinstance(value, dict) else None


def _reject_non_json_number(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")  # NaN, Infinity


def _json_mode_answer(text: str, finish_reason: str) -> str:
    if finish_reason in ("length", "content_filter"):
        # Cut short or refused: returned as is, with that finish_reason, as
        # OpenAI does; retrying would not produce JSON.
        return text
    answer = _json_object_text(text)
    if answer is None:
        raise ProviderError(
            502, "Claude did not return a valid JSON object for `response_format: {\"type\": \"json_object\"}` "
            f"(it answered: {text.strip()[:200]!r}). Retry, or use `json_schema` with a schema for strict "
            "enforcement.", code="invalid_json_output",
        )
    return answer


def _claude_structured_content(payload: dict[str, Any]) -> str | None:
    """The JSON text a schema-enforced run produced. `structured_output` is
    the CLI's typed field for it; `result` carries the same JSON as a string
    and doubles as the fallback if the envelope shape ever changes — but the
    fallback is trusted only when it actually parses as JSON, because on
    refusal/truncation `result` can carry prose, and serving prose as the
    "enforced JSON" would be silent degradation."""
    structured = payload.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        try:
            json.loads(result)
        except json.JSONDecodeError:
            return None
        return result
    return None


def _claude_error_detail(payload: dict[str, Any]) -> str | None:
    """Error text from a CLI result envelope, when it is one. The CLI can
    exit 0 while reporting failure in-band (`is_error`, error subtypes), so
    envelope inspection — not the exit code — is the reliable signal."""
    subtype = payload.get("subtype")
    if payload.get("is_error") is True or (isinstance(subtype, str) and subtype.startswith("error")):
        return str(payload.get("result") or "Claude CLI returned an error.")
    return None


def _claude_failure(detail: str, payload: dict[str, Any] | None = None) -> ProviderError:
    """Classify CLI failures without treating ordinary model output as errors.

    The status the CLI reports ("API Error: 400 ...") decides; a number
    quoted inside the message (an image URL that returned 403, a stack
    trace line) never does. Keywords classify only status-less failures."""
    payload = payload or {}
    error = payload.get("error")
    marker = str(error.get("type") if isinstance(error, dict) else error or "")
    text = (marker + " " + detail).lower()
    if any(word in text for word in ("context_length_exceeded", "prompt is too long", "invalid model")):
        return ProviderError(422, detail, code="invalid_request_error")
    explicit = re.search(r"api error:?\s*(\d{3})", text)
    status = int(explicit.group(1)) if explicit else None
    if status is None:
        if any(word in text for word in ("rate_limit", "usage_limit", "usage limit", "rate limit",
                                         "you've hit your limit", "you have hit your limit")):
            status = 429
        elif any(word in text for word in ("authentication_error", "oauth token", "not logged in",
                                           "please run /login", "invalid access token")):
            status = 401
        elif "permission_error" in text:
            status = 403
        elif "not_found_error" in text:
            status = 404
        elif "request_too_large" in text:
            status = 413
        elif "invalid_request_error" in text:
            status = 400
        elif "overloaded" in text:
            status = 529
    if status == 429:
        info = payload.get("rate_limit_info") or payload.get("rateLimitInfo") or {}
        reset = info.get("resetsAt") if isinstance(info, dict) else None
        delay = max(1.0, reset - time.time()) if isinstance(reset, (int, float)) and math.isfinite(reset) else None
        return ProviderError(429, detail, code="provider_quota_exhausted", retry_after_seconds=delay)
    if status in (401, 403):
        return ProviderError(status, detail, code="provider_auth_error")
    if status == 404:
        # The API does not serve this model to this account.
        return ProviderError(404, detail, code="model_unavailable")
    if status == 413:
        return ProviderError(413, detail, code="request_too_large")
    if status is not None and 400 <= status < 500:
        # The request itself (bad image, unreachable URL, invalid field).
        return ProviderError(422, detail, code="invalid_request_error")
    if status is not None and status >= 500:
        return ProviderError(503, detail, code="provider_overloaded")
    return ProviderError(502, detail, code="provider_failure")


def _usage_from_claude_result(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """CompletionUsage for a whole CLI run. Only the `result` event covers
    every API call the CLI made (including schema enforcement turns), so it
    is the single source; per-message usage would double count."""
    return completion_usage_from_anthropic(payload.get("usage") if isinstance(payload, dict) else None)


CLAUDE_STREAM_LINE_LIMIT = 64 * 1024 * 1024
# The CLI names a tool from the `airelays` MCP server `mcp__airelays__<name>`;
# the API caps tool names at 64 characters, prefix included.
CLAUDE_TOOL_PREFIX = "mcp__airelays__"
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,%d}" % (64 - len(CLAUDE_TOOL_PREFIX)))


def _claude_tools(body: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """OpenAI function tools as MCP definitions for the tool stub. Options
    the CLI cannot honour are refused rather than ignored."""
    tools = body.get("tools")
    if not is_requested("tools", tools):
        return ()
    if not isinstance(tools, list):
        raise InvalidRequestError("`tools` must be an array.", param="tools")
    choice = body.get("tool_choice")
    allowed: set[str] | None = None
    if choice == "none":
        return ()
    if isinstance(choice, dict) and choice.get("type") == "allowed_tools" \
            and (choice.get("allowed_tools") or {}).get("mode") == "auto":
        allowed = {((t.get("function") or t).get("name")) for t in (choice["allowed_tools"].get("tools") or [])
                   if isinstance(t, dict)}
    elif choice not in (None, "auto"):
        raise ProviderError(422, "Claude models cannot be forced to call a tool; use `tool_choice` `auto` or "
                            "`none`.", code="unsupported_for_provider")
    if body.get("parallel_tool_calls") is False:
        raise ProviderError(422, "Claude models may call several tools at once; `parallel_tool_calls: false` "
                            "cannot be honoured.", code="unsupported_for_provider")
    definitions: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ProviderError(422, "Claude models support only `function` tools.", code="unsupported_for_provider")
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise InvalidRequestError(f"Tool names on Claude models must be 1-{64 - len(CLAUDE_TOOL_PREFIX)} "
                                      "letters, digits, `_` or `-`.", param="tools")
        if name in definitions:
            raise InvalidRequestError(f"Duplicate tool name `{name}`.", param="tools")
        parameters = function.get("parameters")
        if parameters is not None and (not isinstance(parameters, dict) or parameters.get("type", "object") != "object"):
            # The CLI silently drops every tool when one schema is not an object.
            raise InvalidRequestError(f"Tool `{name}`: `parameters` must be a JSON Schema of type `object`.",
                                      param="tools")
        definitions[name] = {"name": name, "description": function.get("description") or "",
                             # `type` made explicit: the CLI drops every tool when it is missing.
                             "inputSchema": {"type": "object", "properties": {}, **(parameters or {})}}
    return tuple(d for n, d in definitions.items() if allowed is None or n in allowed)


def _tool_arguments(cwd: Path, tools: tuple[dict[str, Any], ...]) -> list[str]:
    """CLI arguments that make the client's tools visible, never runnable:
    the tool stub serves their definitions, and a `PreToolUse` check
    denies every call (defence in depth; see airelay.claude_tool_guard)."""
    stub, guard = (str(Path(module.__file__).resolve()) for module in (claude_tool_stub, claude_tool_guard))
    write_private(cwd / "tools.json", json.dumps(list(tools)))
    write_private(cwd / "mcp.json", json.dumps({"mcpServers": {"airelays": {
        "command": sys.executable, "args": ["-I", stub, str(cwd / "tools.json")]}}}))
    write_private(cwd / "settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
        {"type": "command", "command": _command_line([sys.executable, "-I", guard])}]}]}}))
    return ["--mcp-config", str(cwd / "mcp.json"), "--settings", str(cwd / "settings.json")]


def claude_ignored_tool_options(body: dict[str, Any]) -> list[str]:
    """Tool options Claude runs without, for disclosure: it has no strict
    tool mode, so `strict: true` tools run non-strict."""
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    strict = any(isinstance(t, dict) and ((t.get("function") if isinstance(t.get("function"), dict) else t).get("strict")
                                          is True) for t in tools)
    return ["tools.function.strict"] if strict else []


def _command_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


INTERRUPT_LINE = (json.dumps({"type": "control_request", "request_id": "airelays-stop",
                              "request": {"subtype": "interrupt"}}) + "\n").encode("utf-8")


class _EarlyStop:
    """Where AIRelays stops a Claude run, read from the stream: Claude's calls
    to the client's tools (each tool_use block: start, argument fragments,
    stop) at `stop_reason: "tool_use"`, or the output limit at
    `stop_reason: "max_tokens"`; with the text and usage of that model call."""

    def __init__(self, allowed: frozenset[str], *, limited: bool = False) -> None:
        self.allowed = allowed
        self.limited = limited
        self.stop_reason = ""
        self.calls: list[dict[str, Any]] = []
        self.text: list[str] = []
        self.usage: dict[str, Any] = {}
        self._open: dict[int, dict[str, Any]] = {}

    def observe(self, event: dict[str, Any]) -> bool:
        if event.get("type") != "stream_event":
            return False
        inner = event.get("event") or {}
        kind = inner.get("type")
        if kind == "message_start":
            self.usage = dict(((inner.get("message") or {}).get("usage")) or {})
        elif kind == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                if block.get("name") not in self.allowed:
                    raise ProviderError(502, f"Claude called a tool AIRelays did not define (`{block.get('name')}`); "
                                        "the request was stopped.", code="cli_unexpected_tools")
                if str(block["name"]).startswith(CLAUDE_TOOL_PREFIX):
                    self._open[inner.get("index")] = {"id": block.get("id"), "name": block["name"], "arguments": ""}
        elif kind == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "input_json_delta" and inner.get("index") in self._open:
                self._open[inner["index"]]["arguments"] += delta.get("partial_json") or ""
            elif delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                self.text.append(delta["text"])
        elif kind == "content_block_stop" and inner.get("index") in self._open:
            self.calls.append(self._open.pop(inner["index"]))
        elif kind == "message_delta":
            self.usage.update(inner.get("usage") or {})
            self.stop_reason = (inner.get("delta") or {}).get("stop_reason") or ""
            return (self.stop_reason == "tool_use" and bool(self.calls)) or \
                (self.stop_reason == "max_tokens" and self.limited)
        return False

    def result(self) -> dict[str, Any]:
        """A `result` event for the stopped run."""
        tool_calls = [{"id": call["id"], "type": "function", "function": {
            "name": call["name"].removeprefix(CLAUDE_TOOL_PREFIX), "arguments": call["arguments"] or "{}"}}
            for call in self.calls]
        return {"type": "result", "subtype": "success", "is_error": False, "result": "".join(self.text),
                "stop_reason": self.stop_reason, "usage": self.usage, "tool_calls": tool_calls}


def _check_cli_event(event: dict[str, Any], request: ClaudeTextRequest) -> None:
    """Guards on what the CLI reports about itself, for every request.

    The startup report must list no tool AIRelays did not define: with
    built-in tools off there are none, so anything else means the CLI
    changed and the request stops before a tool could run. The CLI's echo of
    our message must still hold every image and document: it silently
    replaces media it cannot read with a text notice."""
    if event.get("type") == "system" and event.get("subtype") == "init":
        offered = set(event.get("tools") or [])
        missing = {CLAUDE_TOOL_PREFIX + tool["name"] for tool in request.tools} - offered
        if missing:
            # The stub did not start or a definition was dropped: answering
            # without the client's tools would be silent degradation.
            raise ProviderError(
                502, "The Claude CLI could not load the request's tools "
                f"({', '.join(sorted(name.removeprefix(CLAUDE_TOOL_PREFIX) for name in missing))}); the request was "
                "stopped. Check that the relay's Python can run AIRelays' tool helper.", code="cli_tools_unavailable",
            )
        unexpected = [tool for tool in offered if tool not in request.allowed_tools]
        if unexpected:
            raise ProviderError(
                502, f"The Claude CLI offered tools AIRelays did not define ({', '.join(map(str, unexpected))}); "
                "the request was stopped before any tool could run.", code="cli_unexpected_tools",
            )
    if event.get("type") == "user" and event.get("isReplay") and request.media_count:
        content = (event.get("message") or {}).get("content") or []
        kept = sum(1 for block in content if isinstance(block, dict) and block.get("type") in ("image", "document"))
        if kept < request.media_count:
            raise ProviderError(
                422, f"Claude could not read {request.media_count - kept} of the {request.media_count} images or "
                "documents in this request (for example a CMYK JPEG or an animated image). Convert them to a "
                "standard PNG, JPEG or PDF and retry.", code="invalid_request_error",
            )


def _stderr_message(stderr: bytes, returncode: int) -> str:
    text = stderr.decode("utf-8", errors="replace").strip()
    return text or f"claude exited with code {returncode}"


# Usage comes from Claude Code's own `get_usage` control request (ADR 0007):
# the CLI refreshes the profile's OAuth token while answering, so the
# background refresh also keeps idle profiles signed in.
CLAUDE_USAGE_CACHE_SECONDS = 300.0
# Failed refreshes back off (persisted): a short-lived CLI forgets an
# upstream 429 when it exits, so the relay must remember it instead.
CLAUDE_USAGE_BACKOFF_SECONDS = (300, 600, 1200, 2400, 3600)
# A reading without `limits` is fresh only if its breakdown is this recent;
# the CLI's seeded fallback carries a snapshot up to an hour old.
CLAUDE_USAGE_FRESH_SECONDS = 120.0
CLAUDE_CONTROL_TIMEOUT_SECONDS = 30.0
CLAUDE_EXIT_GRACE_SECONDS = 10.0
CLAUDE_TOKEN_USAGE_UNSUPPORTED = (
    "Claude doesn't report usage for setup tokens; requests are unaffected. "
    "Add a browser sign-in with `airelays claude login` to see usage."
)
CLAUDE_FIVE_HOUR_SECONDS = 5 * 3600
CLAUDE_SEVEN_DAY_SECONDS = 7 * 86400


class ClaudeCliRuntime:
    def __init__(self, settings: Settings, traffic: TrafficLogger | None = None,
                 *, profile: ClaudeProfile | None = None) -> None:
        self._settings = settings
        self._traffic = traffic
        # Without a profile, the runtime serves the stored setup token.
        self.profile = profile or token_profile(settings)
        if not self.profile.managed:
            self.profile.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Resolved now and again after a launch finds nothing (ADR 0008).
        self._launcher = ClaudeLauncher(settings)
        self._semaphore = asyncio.Semaphore(settings.claude_max_concurrent_requests)
        self._models = self._build_models(settings.claude_models)
        self._models_fetched_at: float | None = None
        self._models_lock = asyncio.Lock()
        self._models_error: str | None = None
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
        # Last live reading, kept (and persisted) so a failed refresh shows
        # honest, dated numbers instead of nothing.
        self._usage_last_good: dict[str, Any] | None = None
        self._usage_last_good_epoch: float = 0.0
        # Wall-clock backoff after failed refreshes; survives restarts.
        self._usage_failures = 0
        self._usage_retry_at = 0.0
        self._usage_stale_reason: str | None = None
        self._usage_error: str | None = None
        # Single flight, shielded from callers: a disconnecting client must
        # not cancel a CLI that may be rotating the profile's token.
        self._usage_task: asyncio.Task[None] | None = None
        self._usage_state_path = self.profile.root / "usage-state.json"
        if self.profile.managed:
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
                stateful_conversations=True,
                reasoning_modes=CLAUDE_REASONING_MODES,
                reasoning_default=None,
                structured_output_types=("json_schema", "json_object"),
            )
            records[model_id] = record
        return records

    def list_models(self) -> list[dict[str, Any]]:
        return [record.as_wire() for record in self._models.values()]

    async def refresh_models(self, *, force: bool = False) -> None:
        ttl = max(0.0, self._settings.models_cache_ttl_seconds)
        async with self._models_lock:
            if not force and self._models_fetched_at is not None:
                if time.monotonic() - self._models_fetched_at < ttl:
                    return
            try:
                catalog = await self._discover_models()
                records = self._build_models(self._settings.claude_models)
                for item in catalog:
                    if not isinstance(item, dict):
                        continue
                    selector = item.get("value")
                    if not isinstance(selector, str) or not selector:
                        continue
                    public_id = selector if selector.startswith(("claude-", "claude:")) else f"claude:{selector}"
                    record = self._build_models((public_id,))[public_id]
                    resolved = item.get("resolvedModel")
                    resolved = resolved if isinstance(resolved, str) and resolved.startswith("claude-") else None
                    efforts = item.get("supportedEffortLevels")
                    modes = tuple(x for x in efforts if isinstance(x, str) and x) if isinstance(efforts, list) else ()
                    record = replace(
                        record, resolved_model=resolved, discovery_source="claude_cli",
                        display_name=item.get("displayName"), reasoning_modes=modes,
                    )
                    records[public_id] = record
                    if resolved:
                        records[resolved] = replace(record, id=resolved, upstream_id=resolved)
                self._models = records
                self._models_error = None
            except (OSError, ValueError, TimeoutError, ProviderError) as exc:
                # Older/missing CLIs retain configured ids and the last good
                # catalog. A failed probe is cached too to avoid spawn storms.
                self._models_error = str(exc)
            self._models_fetched_at = time.monotonic()

    async def _discover_models(self) -> list[dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        try:
            async with asyncio.timeout(min(10.0, self._settings.claude_timeout_seconds)):
                # The picker may omit an alias until selected (e.g. it lists
                # opus[1m] but only returns opus's resolution with --model opus).
                selectors = ["default", *(m.upstream_id for m in self._build_models(self._settings.claude_models).values())]
                for selector in selectors:
                    if selector in catalog:
                        continue
                    try:
                        models = await self._initialize_models(selector)
                    except (OSError, ValueError, ProviderError):
                        if not catalog:
                            raise
                        continue
                    for item in models:
                        if isinstance(item, dict) and isinstance(item.get("value"), str):
                            catalog[item["value"]] = item
        except TimeoutError:
            if not catalog:
                raise
        return list(catalog.values())

    async def _initialize_models(self, selector: str) -> list[dict[str, Any]]:
        # The SDK initialize control request returns the CLI's own picker
        # catalog and alias resolutions without submitting a user prompt.
        payload = await self._control_request(
            {"subtype": "initialize"}, model=selector,
            timeout=min(10.0, self._settings.claude_timeout_seconds),
        )
        if isinstance(payload.get("models"), list):
            return payload["models"]
        raise ProviderError(503, "Claude CLI did not return a model catalog.")

    async def _control_request(self, request: dict[str, Any], *, model: str | None = None,
                               timeout: float) -> dict[str, Any]:
        """One SDK control request in a short-lived CLI session: no prompt,
        no inference. Raises ProviderError with code `cli_unsupported` when
        the installed CLI does not know the request."""
        command = ["-p"]
        if model:
            command.extend(["--model", model])
        command.extend([
            "--tools", "", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        ])
        with workdir("airelays-claude-control-") as cwd:
            process = await self._spawn(
                command, cwd=cwd, env=self._subprocess_env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024,
            )
            try:
                async with asyncio.timeout(timeout):
                    assert process.stdin is not None and process.stdout is not None
                    message = {"type": "control_request", "request_id": "airelays", "request": request}
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await process.stdin.drain()
                    while line := await process.stdout.readline():
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ProviderError(502, "Claude CLI returned malformed output.",
                                                code="provider_failure") from exc
                        if not isinstance(item, dict) or item.get("type") != "control_response":
                            continue
                        response = item.get("response")
                        if not isinstance(response, dict) or response.get("request_id") != "airelays":
                            continue
                        if response.get("subtype") == "error":
                            detail = str(response.get("error") or "Claude CLI rejected the request.")
                            unsupported = "unsupported control request" in detail.lower()
                            raise ProviderError(502, detail, code="cli_unsupported" if unsupported else "provider_failure")
                        payload = response.get("response")
                        return payload if isinstance(payload, dict) else {}
                raise ProviderError(502, "Claude CLI exited without answering.", code="provider_failure")
            except TimeoutError as exc:
                raise ProviderError(504, "Claude CLI did not answer in time.", code="provider_timeout") from exc
            finally:
                await _end_process(process, close_stdin=True)

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
        expected = account_identity(self.profile.identity)
        # A signed-out profile has no identity to compare. It needs a new
        # sign-in; reporting it as holding another account is false.
        actual = account_identity(probe) if probe.get("logged_in") else None
        mismatch = bool(expected and actual and actual != expected)
        verified = not expected or actual == expected
        return {
            "slug": self.profile.slug,
            "managed_profile": self.profile.managed,
            "enabled": True,
            "local_only": True,
            "requires_relay_bearer_auth": self._settings.require_bearer_auth,
            "stateless_only": True,
            "ready_for_requests": ready and verified and not self.profile.identity.get("reauth_required"),
            "reauth_required": bool(self.profile.identity.get("reauth_required")),
            "identity_mismatch": mismatch,
            "cli_installed": probe.get("installed", False),
            "cli_version": probe.get("version"),
            "cli_path": self._launcher.describe(),
            "cli_error": probe.get("cli_error"),
            "auth_method": probe.get("auth_method"),
            "api_provider": probe.get("api_provider"),
            "logged_in": probe.get("logged_in", False),
            "email": self.profile.identity.get("email") or probe.get("email"),
            "account_id": self.profile.identity.get("account_id") or probe.get("account_id"),
            "organization_id": self.profile.identity.get("organization_id") or probe.get("organization_id"),
            "subscription_type": probe.get("subscription_type") or self.profile.identity.get("subscription_type"),
            "models": [record.id for record in self._models.values()],
            "models_discovery_error": self._models_error,
            "oauth_token_source": "profile" if self.profile.managed else "file",
            "notes": [
                "Use `airelays claude login` to add an isolated subscription account.",
                "For headless environments, run `claude setup-token` on a machine with a "
                "browser, then store the token with `airelays claude set-token`.",
            ] if self.profile.managed else [
                "Setup token: inference-only; Claude reports no usage or email for it.",
            ],
        }

    async def create_chat_completion(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = self._prepare_chat_request(body)
        result = await self._run_json(request, request_id)
        content = result.get("result", "")
        tool_calls = result.get("tool_calls")
        if request.json_mode and not tool_calls:
            content = _json_mode_answer(content, _finish_reason(result.get("stop_reason")))
        if request.output_schema and not tool_calls and result.get("stop_reason") != "max_tokens":
            # Schema-enforced runs must return the enforced JSON only; the
            # model's surrounding prose (if any) is not the answer. A run
            # that produced no conforming JSON is a provider failure, not an
            # empty success.
            structured_content = _claude_structured_content(result)
            if structured_content is None:
                raise ProviderError(
                    502,
                    "The Claude CLI returned no schema-conforming output for a `response_format` request.",
                    code="invalid_structured_output",
                )
            content = structured_content
        payload: dict[str, Any] = {
            "id": f"chatcmpl_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.public_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None if tool_calls and not content else content,
                        "refusal": None,
                        **({"tool_calls": tool_calls} if tool_calls else {}),
                    },
                    "logprobs": None,
                    "finish_reason": "tool_calls" if tool_calls else _finish_reason(result.get("stop_reason")),
                }
            ],
        }
        usage = _usage_from_claude_result(result)
        if usage is not None:
            payload["usage"] = usage
        return payload

    def stream_chat_completion(
        self,
        body: dict[str, Any],
        request_id: str,
    ) -> AsyncIterator[bytes]:
        # Validation must happen before the app layer commits SSE headers:
        # a 422 raised inside the generator can only surface as an empty 200
        # once streaming has started. Prepare eagerly, then stream.
        request = self._prepare_chat_request(body)
        return self._stream_chat_response(request, request_id)

    async def _stream_chat_response(
        self,
        request: ClaudeTextRequest,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        response_id = f"chatcmpl_{uuid.uuid4().hex}"
        created_at = int(time.time())
        sent_role = False
        saw_text = False
        assistant_fallback = ""
        last_usage: dict[str, Any] | None = None
        finish_reason = "stop"
        # Schema-enforced runs: the enforced JSON streams as the CLI's
        # StructuredOutput tool-use block (`input_json_delta` fragments); any
        # text deltas around it are the model's prose, not the answer, and
        # must not reach a client that asked for JSON.
        structured = bool(request.output_schema)
        # JSON mode: the answer is validated before it is sent, so text is
        # buffered and delivered as one chunk.
        json_buffer: list[str] | None = [] if request.json_mode else None
        # Under CLI 2.1.209 exactly one tool-use block exists per schema run;
        # if a future CLI ever streamed a second (e.g. an enforcement retry),
        # concatenating both would hand the client unparseable JSON, so only
        # the first block's fragments are forwarded.
        tool_use_blocks = 0
        schema_block: Any = None  # the stream index of the first StructuredOutput block
        async with aclosing(self._run_stream(request, request_id)) as events:
            async for event in events:
                event_type = event.get("type")
                if event_type == "stream_event":
                    inner = event.get("event") or {}
                    if inner.get("type") == "content_block_start":
                        block = inner.get("content_block") or {}
                        if block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                            tool_use_blocks += 1
                            if tool_use_blocks == 1:
                                schema_block = inner.get("index")
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta") or {}
                        text: Any = None
                        if delta.get("type") == "text_delta" and not structured:
                            text = delta.get("text")
                        elif delta.get("type") == "input_json_delta" and structured and inner.get("index") == schema_block:
                            text = delta.get("partial_json")
                        if not isinstance(text, str) or not text:
                            continue
                        if json_buffer is not None:
                            json_buffer.append(text)
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
                            include_usage=request.include_usage,
                        )
                        yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                    elif inner.get("type") == "message_delta":
                        finish_reason = _finish_reason((inner.get("delta") or {}).get("stop_reason"))
                elif event_type == "assistant" and not saw_text and not structured:
                    message = event.get("message") or {}
                    content = message.get("content") or []
                    assistant_fallback = "".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                elif event_type == "result":
                    # The CLI can exit 0 while reporting failure in-band; that
                    # text is an error, never the answer (the non-stream path has
                    # always guarded this — the stream must too).
                    error_detail = _claude_error_detail(event)
                    if error_detail is not None:
                        raise _claude_failure(error_detail, event)
                    last_usage = _usage_from_claude_result(event)
                    finish_reason = _finish_reason(event.get("stop_reason"))
                    tool_calls = event.get("tool_calls") or []
                    if tool_calls:
                        finish_reason = "tool_calls"
                        structured, json_buffer = False, None  # a tool turn carries calls, not the answer
                        for index, call in enumerate(tool_calls):
                            delta_payload = {"tool_calls": [{"index": index, **call}]}
                            if not sent_role:
                                delta_payload["role"] = "assistant"
                                sent_role = True
                            chunk = chat_completion_chunk(response_id, created_at, request.public_model,
                                                          delta_payload, include_usage=request.include_usage)
                            yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
                        saw_text = True
                    elif structured and event.get("stop_reason") != "max_tokens":
                        if not saw_text:
                            structured_fallback = _claude_structured_content(event)
                            if structured_fallback is None:
                                raise ProviderError(
                                    502,
                                    "The Claude CLI returned no schema-conforming output for a `response_format` request.",
                                    code="invalid_structured_output",
                                )
                            assistant_fallback = structured_fallback
                    elif not assistant_fallback:
                        result_text = event.get("result")
                        if isinstance(result_text, str):
                            assistant_fallback = result_text
        if json_buffer is not None:
            assistant_fallback = _json_mode_answer("".join(json_buffer) or assistant_fallback, finish_reason)
        # Logged before any yield: a client that disconnects after the last
        # chunk must not cost the record of a fully billed run.
        self._log_outbound_usage(request_id, request.public_model, last_usage)
        if assistant_fallback and not saw_text:
            delta_payload = {"role": "assistant", "content": assistant_fallback}
            chunk = chat_completion_chunk(
                response_id,
                created_at,
                request.public_model,
                delta_payload,
                include_usage=request.include_usage,
            )
            yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        final_chunk = chat_completion_chunk(
            response_id,
            created_at,
            request.public_model,
            {},
            finish_reason=finish_reason,
            include_usage=request.include_usage,
        )
        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        if request.include_usage and last_usage is not None:
            closing = usage_chunk("chat.completion.chunk", response_id, created_at, request.public_model, last_usage)
            yield f"data: {json.dumps(closing, ensure_ascii=True)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    async def create_completion(self, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        request = self._prepare_completion_request(body)
        result = await self._run_json(request, request_id)
        payload: dict[str, Any] = {
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
        }
        usage = _usage_from_claude_result(result)
        if usage is not None:
            payload["usage"] = usage
        return payload

    def stream_completion(
        self,
        body: dict[str, Any],
        request_id: str,
    ) -> AsyncIterator[bytes]:
        # Same eager-validation contract as stream_chat_completion.
        request = self._prepare_completion_request(body)
        return self._stream_completion_response(request, request_id)

    async def _stream_completion_response(
        self,
        request: ClaudeTextRequest,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        response_id = f"cmpl_{uuid.uuid4().hex}"
        created_at = int(time.time())
        saw_text = False
        assistant_fallback = ""
        last_usage: dict[str, Any] | None = None
        finish_reason = "stop"
        async with aclosing(self._run_stream(request, request_id)) as events:
            async for event in events:
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
                                include_usage=request.include_usage,
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
                    error_detail = _claude_error_detail(event)
                    if error_detail is not None:
                        raise _claude_failure(error_detail, event)
                    last_usage = _usage_from_claude_result(event)
                    finish_reason = _finish_reason(event.get("stop_reason"))
                    if not assistant_fallback:
                        result_text = event.get("result")
                        if isinstance(result_text, str):
                            assistant_fallback = result_text
        # Logged before any yield: a client that disconnects after the last
        # chunk must not cost the record of a fully billed run.
        self._log_outbound_usage(request_id, request.public_model, last_usage)
        if assistant_fallback and not saw_text:
            chunk = completion_chunk(
                response_id,
                created_at,
                request.public_model,
                assistant_fallback,
                finish_reason=None,
                include_usage=request.include_usage,
            )
            yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        final_chunk = completion_chunk(
            response_id,
            created_at,
            request.public_model,
            "",
            finish_reason=finish_reason,
            include_usage=request.include_usage,
        )
        yield f"data: {json.dumps(final_chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        if request.include_usage and last_usage is not None:
            closing = usage_chunk("text_completion", response_id, created_at, request.public_model, last_usage)
            yield f"data: {json.dumps(closing, ensure_ascii=True)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    def _prepare_chat_request(self, body: dict[str, Any]) -> ClaudeTextRequest:
        resolved = self._resolved_model_from_body(body)
        if body.get("n") not in {None, 1}:
            raise ProviderError(422, "The Claude runtime supports only `n=1`.", code="unsupported_for_provider")
        for field in CLAUDE_CHAT_REJECTED:
            if is_requested(field, body.get(field)):
                raise ProviderError(
                    422,
                    f"The Claude runtime does not support `{field}` on `/v1/chat/completions`.",
                    code="unsupported_for_provider",
                )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise InvalidRequestError("Chat completions requests must include a non-empty `messages` array.",
                                      param="messages")
        conversation = conversation_from_chat(messages)  # InvalidRequestError -> 422 in the app
        system_parts = conversation.system_parts
        if sum(len(json.dumps(block)) for block in conversation.blocks if block["type"] != "text") > MAX_REQUEST_MEDIA_BYTES:
            raise RequestTooLargeError(f"Images and documents in one Claude request are limited to "
                                       f"{MAX_REQUEST_MEDIA_BYTES // (1024 * 1024)} MiB (encoded).")
        output_schema = _claude_output_schema(body, "/v1/chat/completions")
        response_format = body.get("response_format")
        json_mode = isinstance(response_format, dict) and response_format.get("type") == "json_object"
        return ClaudeTextRequest(
            public_model=resolved.public_id,
            upstream_model=resolved.upstream_id,
            system_prompt=_claude_system_prompt(system_parts, json_mode=json_mode),
            content=tuple(conversation.blocks),
            media_count=conversation.media_count,
            include_usage=stream_include_usage(body),
            effort=_claude_effort(body, "/v1/chat/completions", self._models[resolved.public_id].reasoning_modes),
            output_schema=output_schema,
            json_mode=json_mode,
            tools=_claude_tools(body),
            max_output_tokens=output_token_limit(body),
        )

    def _prepare_completion_request(self, body: dict[str, Any]) -> ClaudeTextRequest:
        resolved = self._resolved_model_from_body(body)
        if body.get("n") not in {None, 1}:
            raise ProviderError(422, "The Claude runtime supports only `n=1`.", code="unsupported_for_provider")
        for field in CLAUDE_COMPLETION_REJECTED:
            if is_requested(field, body.get(field)):
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
            raise InvalidRequestError("Completions requests must include a non-empty `prompt` string.", param="prompt")
        return ClaudeTextRequest(
            public_model=resolved.public_id,
            upstream_model=resolved.upstream_id,
            system_prompt=_claude_system_prompt([]),
            content=tuple(conversation_from_prompt(prompt).blocks),
            include_usage=stream_include_usage(body),
            effort=_claude_effort(body, "/v1/completions", self._models[resolved.public_id].reasoning_modes),
            max_output_tokens=output_token_limit(body),
        )

    def _resolved_model_from_body(self, body: dict[str, Any]) -> ResolvedModel:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise InvalidRequestError("Requests must include a non-empty `model` string.", param="model")
        resolved = self.resolve_model(model)
        if resolved is None:
            raise ProviderError(
                404,
                f"Unknown Claude model `{model}`. Configure it under `[providers.claude].models` first.",
                code="model_not_found",
            )
        return resolved

    async def _run_json(self, request: ClaudeTextRequest, request_id: str) -> dict[str, Any]:
        """A whole run's `result` event, read from the same stream as
        streaming requests (one runner, one parser)."""
        result: dict[str, Any] | None = None
        async with aclosing(self._run_stream(request, request_id)) as events:
            async for event in events:
                if event.get("type") == "result":
                    result = event
        if result is None:  # pragma: no cover - _run_stream raises first
            raise ProviderError(502, "Claude stream ended without a result.", code="provider_failure")
        self._log_result(request_id, json.dumps(result, ensure_ascii=False).encode("utf-8"))
        error_detail = _claude_error_detail(result)
        if error_detail is not None:
            raise _claude_failure(error_detail, result)
        return result

    async def _run_stream(
        self,
        request: ClaudeTextRequest,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._semaphore:
            with workdir() as cwd:
                env = self._subprocess_env()
                if request.max_output_tokens is not None:
                    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request.max_output_tokens)
                command = await self._inference_command(request, stream=True, cwd=cwd, env=env)
                self._log_command(request_id, request.public_model, command, request.prompt)
                process = await self._spawn(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # A final `assistant`/`result` event carries the whole
                    # answer on one line; images echoed back can be MBs.
                    limit=CLAUDE_STREAM_LINE_LIMIT,
                )

                stderr_task = asyncio.create_task(process.stderr.read())
                saw_result = saw_init = False
                try:
                    assert process.stdin is not None
                    process.stdin.write(user_line(list(request.content)))
                    await process.stdin.drain()
                    stoppable = bool(request.tools) or request.max_output_tokens is not None
                    if not stoppable:
                        process.stdin.close()  # stoppable runs keep it open to interrupt
                    early_stop = _EarlyStop(request.allowed_tools, limited=request.max_output_tokens is not None) \
                        if stoppable else None
                    assert process.stdout is not None
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
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        if request.tools and not saw_init and event.get("type") != "system":
                            raise ProviderError(502, "The Claude CLI did not report its tools; the request was stopped.",
                                                code="cli_tools_unavailable")
                        saw_init = saw_init or (event.get("type") == "system" and event.get("subtype") == "init")
                        _check_cli_event(event, request)
                        saw_result = saw_result or event.get("type") == "result"
                        info = event.get("rate_limit_info") or event.get("rateLimitInfo") or {}
                        if event.get("type") == "rate_limit_event" and info.get("status") == "rejected":
                            raise _claude_failure("Claude usage limit reached.", event)
                        if event.get("type") == "assistant" and event.get("error"):
                            content = (event.get("message") or {}).get("content") or []
                            detail = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
                            raise _claude_failure(detail or str(event["error"]), event)
                        if event.get("type") == "stream_event" and (event.get("event") or {}).get("type") == "error":
                            inner = event["event"]
                            raise _claude_failure(str((inner.get("error") or {}).get("message") or "Claude stream failed."), inner)
                        if event.get("type") == "user":
                            continue  # the CLI's own echo of our message
                        if early_stop is not None and early_stop.observe(event):
                            # Claude asked for tools, or reached the output
                            # limit: stop the run before the CLI acts on the
                            # calls or starts a continuation call.
                            try:
                                process.stdin.write(INTERRUPT_LINE)
                                await process.stdin.drain()
                                process.stdin.close()
                            except (BrokenPipeError, ConnectionResetError):
                                pass  # the CLI already ended
                            saw_result = True
                            yield event
                            yield early_stop.result()
                            # The calls are in hand: give the CLI a moment to
                            # end on its own, then `finally` stops it (gracefully).
                            with contextlib.suppress(asyncio.TimeoutError, ValueError):
                                await asyncio.wait_for(process.stdout.read(), timeout=CLAUDE_EXIT_GRACE_SECONDS)
                            return
                        yield event
                        if saw_result:
                            if not process.stdin.is_closing():
                                process.stdin.close()
                            break
                    async with asyncio.timeout(self._settings.claude_timeout_seconds):
                        returncode = await process.wait()
                        stderr = await stderr_task
                    if returncode == 127:
                        self._launcher.refresh()
                    if returncode != 0 and not saw_result:
                        raise _claude_failure(_stderr_message(stderr, returncode))
                    if not saw_result:
                        raise ProviderError(502, "Claude stream ended without a result.", code="provider_failure")
                except asyncio.TimeoutError as exc:
                    raise ProviderError(
                        504,
                        "Claude CLI timed out while streaming a response.",
                        code="provider_timeout",
                    ) from exc
                except ValueError as exc:  # a stream line over CLAUDE_STREAM_LINE_LIMIT
                    raise ProviderError(502, "The Claude CLI produced an output line too large to read.",
                                        code="cli_output_too_large") from exc
                finally:
                    await _end_process(process)
                    await stderr_task

    def _build_command(self, request: ClaudeTextRequest, *, stream: bool,
                       system_prompt_file: str | None = None) -> list[str]:
        """CLI arguments after the executable (the launcher adds it)."""
        command = [
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
        if system_prompt_file:
            command.extend(["--system-prompt-file", system_prompt_file])
        elif request.system_prompt:
            command.extend(["--system-prompt", request.system_prompt])
        if request.effort:
            command.extend(["--effort", request.effort])
        if request.output_schema:
            command.extend(["--json-schema", request.output_schema])
        # One structured input path for every request (text, images,
        # documents); stream-json input requires stream-json output.
        command.extend(["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"])
        if stream or request.tools:
            # Tool runs stop at the call boundary (`message_delta`).
            command.append("--include-partial-messages")
        if request.media_count:
            command.append("--replay-user-messages")
        return command

    async def _inference_command(self, request: ClaudeTextRequest, *, stream: bool, cwd: str,
                                 env: dict[str, str]) -> list[str]:
        """A system prompt leaves argv (process list, OS size limits) for a
        private file in the per-request directory when the CLI supports it."""
        prompt_file = None
        if request.system_prompt and await self._launcher.supports_system_prompt_file(env=env, cwd=self.profile.root):
            prompt_file = os.path.join(cwd, "system-prompt.txt")
            write_private(Path(prompt_file), request.system_prompt)
        command = self._build_command(request, stream=stream, system_prompt_file=prompt_file)
        if request.tools:
            command += _tool_arguments(Path(cwd), request.tools)
        return command

    async def _spawn(self, args: list[str], *, cwd: str, env: dict[str, str],
                     **kwargs: Any) -> asyncio.subprocess.Process:
        try:
            return await self._launcher.spawn(args, cwd=cwd, env=env, **kwargs)
        except ArgumentsTooLong as exc:
            raise ProviderError(
                413,
                "The request is too large for the local Claude CLI "
                "(its arguments exceed this platform's command-line limit).",
                code="invalid_request_error",
            ) from exc
        except FileNotFoundError as exc:
            raise ProviderError(
                503,
                f"Claude CLI not found at `{self._launcher.describe()}`.",
                code="provider_unavailable",
            ) from exc
        except OSError as exc:
            raise ProviderError(503, f"Cannot launch the Claude CLI: {exc}", code="provider_unavailable") from exc

    def _subprocess_env(self, *, interactive: bool = False) -> dict[str, str]:
        # Every process names its relay-owned profile; only the setup-token
        # account receives the stored token (ADR 0007).
        token = None if self.profile.managed else self._settings.resolve_claude_oauth_token()
        return self._launcher.env(config_dir=self.profile.config_dir, oauth_token=token, interactive=interactive)

    def run_cli(self, args: list[str], *, interactive: bool = False, timeout: float | None = None,
                capture: bool = True) -> subprocess.CompletedProcess[Any]:
        """One synchronous `claude` command in this profile (sign-in,
        sign-out, probes). Raises OSError when it cannot be launched."""
        return self._launcher.run(
            args, env=self._subprocess_env(interactive=interactive), cwd=self.profile.root,
            timeout=timeout, capture=capture, interactive=interactive,
        )

    @property
    def needs_first_probe(self) -> bool:
        return self._last_probe is None

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
        code path. Source: Claude Code's `get_usage` control request, run in
        this profile; AIRelays never reads Claude credentials (ADR 0007)."""
        del request_id
        if not self.profile.managed:
            raise ProviderError(422, CLAUDE_TOKEN_USAGE_UNSUPPORTED, code="unsupported_for_provider")
        now = time.time()
        if self._usage_last_good is not None and now - self._usage_last_good_epoch < CLAUDE_USAGE_CACHE_SECONDS:
            return self._usage_snapshot()
        if now >= self._usage_retry_at:
            if self._usage_task is None or self._usage_task.done():
                self._usage_task = asyncio.create_task(self._refresh_usage())
            await asyncio.shield(self._usage_task)
            if self._usage_stale_reason is None and self._usage_last_good is not None:
                return self._usage_snapshot()
        return self._stale_usage()

    async def check_sign_in(self) -> bool | None:
        """Ask Anthropic, through the CLI's usage query (no inference),
        whether this account's sign-in still works: True, False, or None
        when it cannot tell (setup token, CLI failure). Decides whether a
        login-like failure was about the account or about one model."""
        if not self.profile.managed:
            return None
        try:
            answer = await self._control_request(
                {"subtype": "get_usage", "skip_behaviors": True}, timeout=CLAUDE_CONTROL_TIMEOUT_SECONDS,
            )
        except ProviderError as error:
            # Only an explicit rejection: suspending an account cannot be
            # undone without a new sign-in, so ambiguity means "unknown".
            text = error.detail.lower()
            return False if re.search(r"api error:?\s*401", text) or "authentication_error" in text else None
        # The CLI may answer from a saved snapshot when the usage endpoint
        # fails (as with a revoked token); only live usage proves the sign-in.
        live = answer.get("rate_limits_available") is True and _fresh_usage(answer.get("rate_limits"))
        return True if live else None

    async def _refresh_usage(self) -> None:
        try:
            answer = await self._control_request(
                {"subtype": "get_usage", "skip_behaviors": True}, timeout=CLAUDE_CONTROL_TIMEOUT_SECONDS,
            )
        except ProviderError as error:
            if error.code == "cli_unsupported":
                self._usage_failed("cli_unsupported", "This Claude Code version can't report usage; update Claude Code.")
            else:
                self._usage_failed("cli_failed", f"Claude Code could not report usage: {error.detail}")
            return
        limits = answer.get("rate_limits")
        if answer.get("rate_limits_available") is True and _fresh_usage(limits):
            self._usage_last_good = self._normalize_usage(limits)
            self._usage_last_good_epoch = time.time()
            self._usage_failures = 0
            self._usage_retry_at = 0.0
            self._usage_stale_reason = None
            self._usage_error = None
            self._save_usage_state()
        elif os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
            self._usage_failed("nonessential_traffic_disabled",
                               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC is set, so Claude Code doesn't fetch usage.")
        elif answer.get("rate_limits_available") is False:
            self._usage_failed("usage_unavailable", "Claude doesn't report usage for this sign-in.")
        else:
            self._usage_failed("usage_unavailable", "Claude did not return live usage.")

    def _usage_failed(self, reason: str, message: str) -> None:
        self._usage_failures += 1
        delay = CLAUDE_USAGE_BACKOFF_SECONDS[min(self._usage_failures, len(CLAUDE_USAGE_BACKOFF_SECONDS)) - 1]
        self._usage_retry_at = time.time() + delay
        self._usage_stale_reason = reason
        self._usage_error = message
        self._save_usage_state()

    def _usage_snapshot(self) -> dict[str, Any]:
        snapshot = json.loads(json.dumps(self._usage_last_good))
        _refresh_stale_windows(snapshot)
        return snapshot

    def _stale_usage(self) -> dict[str, Any]:
        """The last live reading, dated and explained; an error when there
        has never been one. Callers rely on `stale`/`stale_reason`/
        `as_of_epoch`/`retry_after_seconds` to present it honestly."""
        retry_after = max(0, math.ceil(self._usage_retry_at - time.time()))
        message = self._usage_error or "Claude did not return live usage."
        if self._usage_last_good is None:
            minutes = max(1, math.ceil(retry_after / 60))
            raise ProviderError(
                503, f"{message} Retrying in ~{minutes}m; requests are unaffected.",
                code="provider_rate_limited", retry_after_seconds=retry_after or None,
            )
        stale = self._usage_snapshot()
        stale["stale"] = True
        stale["stale_reason"] = self._usage_stale_reason or "usage_unavailable"
        stale["stale_message"] = message
        stale["as_of_epoch"] = int(self._usage_last_good_epoch)
        if retry_after:
            stale["retry_after_seconds"] = retry_after
        return stale

    # ----- usage state persistence -----
    # The last reading and the backoff survive restarts, so a restart loop
    # can never turn into a burst of usage requests.

    def _load_usage_state(self) -> None:
        try:
            state = json.loads(self._usage_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(state, dict):
            return
        now = time.time()
        retry_at = state.get("retry_at_epoch")
        failures = state.get("failures")
        if isinstance(retry_at, (int, float)) and retry_at > now and isinstance(failures, int) and failures > 0:
            # Clamped: a clock jump must not silence the meter for days.
            self._usage_retry_at = min(float(retry_at), now + CLAUDE_USAGE_BACKOFF_SECONDS[-1])
            self._usage_failures = failures
            reason, error = state.get("stale_reason"), state.get("error")
            self._usage_stale_reason = reason if isinstance(reason, str) else None
            self._usage_error = error if isinstance(error, str) else None
        last_good = state.get("last_good")
        last_good_epoch = state.get("last_good_epoch")
        if isinstance(last_good, dict) and isinstance(last_good_epoch, (int, float)):
            # Snapshots older than a day describe windows that have long
            # since rolled over; showing them helps nobody.
            if now - last_good_epoch < 86400:
                self._usage_last_good = last_good
                self._usage_last_good_epoch = float(last_good_epoch)

    def _save_usage_state(self) -> None:
        state = {
            "failures": self._usage_failures,
            "retry_at_epoch": self._usage_retry_at,
            "stale_reason": self._usage_stale_reason,
            "error": self._usage_error,
            "last_good": self._usage_last_good,
            "last_good_epoch": self._usage_last_good_epoch,
        }
        try:
            self._usage_state_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a second relay sharing data_dir must never
            # read a half-written state file.
            tmp = self._usage_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
            os.replace(tmp, self._usage_state_path)
        except OSError:
            pass  # best-effort: the in-memory backoff still holds

    def _normalize_usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        probe = self._last_probe or {}
        primary = _claude_usage_window(payload.get("five_hour"), CLAUDE_FIVE_HOUR_SECONDS)
        secondary = _claude_usage_window(payload.get("seven_day"), CLAUDE_SEVEN_DAY_SECONDS)
        additional = []
        for key, label in (("seven_day_sonnet", "Sonnet"), ("seven_day_opus", "Opus"), ("seven_day_fable", "Fable")):
            window = _claude_usage_window(payload.get(key), CLAUDE_SEVEN_DAY_SECONDS)
            if window is not None:
                additional.append(
                    {
                        "limit_name": label,
                        "metered_feature": None,
                        "rate_limit": {
                            "allowed": None,
                            "limit_reached": _window_at_limit(window),
                            "primary_window": window,
                            "secondary_window": None,
                        },
                    }
                )
        # The modern endpoint explicitly names scopes. Opaque legacy keys
        # must not be guessed: e.g. a Fable cap arrives as weekly_scoped.
        scoped = []
        modern_limits = payload.get("limits")
        for limit in modern_limits if isinstance(modern_limits, list) else []:
            if not isinstance(limit, dict):
                continue
            kind = limit.get("kind")
            seconds = (CLAUDE_FIVE_HOUR_SECONDS if kind == "session" else
                       CLAUDE_SEVEN_DAY_SECONDS if limit.get("group") == "weekly" or kind == "weekly_all" else None)
            window = _claude_usage_window(
                {"utilization": limit.get("percent"), "resets_at": limit.get("resets_at")}, seconds
            )
            if kind == "session":
                primary = window
            elif kind == "weekly_all":
                secondary = window
            else:
                scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
                labels = []
                for value in scope.values():
                    label = value.get("display_name") or value.get("id") if isinstance(value, dict) else value
                    if isinstance(label, str) and label:
                        labels.append(label)
                label = " / ".join(labels) or str(kind or "Additional limit")
                scoped.append({
                    "limit_name": label,
                    "metered_feature": kind,
                    "scope": scope,
                    "severity": limit.get("severity"),
                    "is_active": limit.get("is_active"),
                    "rate_limit": {"allowed": None, "limit_reached": _window_at_limit(window),
                                   "primary_window": window, "secondary_window": None},
                })
        modern_labels = {extra["limit_name"].lower() for extra in scoped}
        additional = [extra for extra in additional if extra["limit_name"].lower() not in modern_labels] + scoped
        reached_type = None
        if _window_at_limit(primary):
            reached_type = "five_hour"
        elif _window_at_limit(secondary):
            reached_type = "seven_day"
        return {
            "object": "subscription_status",
            "provider": "claude",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": {"kind": "claude_subscription", "upstream_path": "/api/oauth/usage"},
            "spend": payload.get("spend") if isinstance(payload.get("spend"), dict) else None,
            "extra_usage": payload.get("extra_usage") if isinstance(payload.get("extra_usage"), dict) else None,
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
        """`installed` comes from the resolver. A probe that times out (a
        busy machine) is unknown: the last known sign-in state is kept, and
        the CLI is never reported as missing because it was slow."""
        if not self._launcher.command.found:
            self._launcher.refresh()
        installed = self._launcher.command.found
        version = None
        status_payload: dict[str, Any] | None = None
        timed_out = False
        if installed:
            try:
                version = self._status_version_probe()
                status_payload = self._status_auth_probe()
            except subprocess.TimeoutExpired:
                timed_out = True
            except OSError:
                installed = False
        previous = self._last_probe or {}
        if timed_out and status_payload is None:
            identity = {key: previous.get(key) for key in (
                "logged_in", "auth_method", "api_provider", "email", "account_id",
                "organization_id", "subscription_type",
            )}
        else:
            identity = {
                "logged_in": bool(status_payload and status_payload.get("loggedIn")),
                "auth_method": status_payload.get("authMethod") if status_payload else None,
                "api_provider": status_payload.get("apiProvider") if status_payload else None,
                "email": status_payload.get("email") if status_payload else None,
                "account_id": status_payload.get("accountId") if status_payload else None,
                "organization_id": status_payload.get("orgId") if status_payload else None,
                "subscription_type": status_payload.get("subscriptionType") if status_payload else None,
            }
        probe = {
            "installed": installed,
            "version": version or (previous.get("version") if timed_out else None),
            "probe_timed_out": timed_out,
            "cli_error": self._launcher.command.error,
            **identity,
        }
        self._last_probe = probe
        return probe

    def _status_version_probe(self) -> str | None:
        process = self.run_cli(["--version"], timeout=5)
        if process.returncode != 0:
            return None
        text = process.stdout.strip()
        return text or None

    def _status_auth_probe(self) -> dict[str, Any] | None:
        process = self.run_cli(["auth", "status", "--json"], timeout=5)
        if process.returncode != 0:
            return None
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _log_outbound_usage(self, request_id: str, model: str, usage: dict[str, Any] | None) -> None:
        # Streams return no payload to the app layer, which logs this record
        # for non-streaming requests.
        if self._traffic is not None and usage is not None:
            self._traffic.write({"request_id": request_id, "phase": "outbound_usage", "provider": "claude",
                                 "model": model, "usage": usage})

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
    limits = snapshot.get("rate_limits") or {}
    default = limits.get("default") or {}
    groups = [default, *((extra or {}).get("rate_limit") or {} for extra in limits.get("additional") or [])]
    for rate in groups:
        windows = [rate[key] for key in ("primary_window", "secondary_window") if isinstance(rate.get(key), dict)]
        for window in windows:
            reset_at = window.get("reset_at")
            if isinstance(reset_at, (int, float)) and reset_at > 0:
                remaining = max(0, int(reset_at) - wall_now)
                window["reset_after_seconds"] = remaining
                if remaining == 0:
                    window["used_percent"] = None
                    window["remaining_percent"] = None
        if rate:
            rate["limit_reached"] = any(_window_at_limit(window) for window in windows)
    snapshot["rate_limit_reached_type"] = (
        "five_hour" if _window_at_limit(default.get("primary_window")) else
        "seven_day" if _window_at_limit(default.get("secondary_window")) else None
    )


def _fresh_usage(limits: Any) -> bool:
    """Whether a `get_usage` reading is live. When the usage endpoint fails,
    the CLI answers from a persisted snapshot up to an hour old, without
    `limits` and without saying so; only live readings carry `limits`. A
    recent breakdown covers plans whose payload has no `limits`."""
    if not isinstance(limits, dict):
        return False
    if isinstance(limits.get("limits"), list) and limits["limits"]:
        return True
    breakdown = limits.get("seven_day_breakdown")
    as_of = breakdown.get("as_of") if isinstance(breakdown, dict) else None
    try:
        age = time.time() - datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return abs(age) < CLAUDE_USAGE_FRESH_SECONDS


async def _end_process(process: asyncio.subprocess.Process, *, close_stdin: bool = False) -> None:
    """End a Claude CLI process without SIGKILL first. A CLI killed between
    refreshing its OAuth token and persisting it leaves the profile with a
    dead refresh token. Control sessions exit cleanly once stdin closes;
    inference runs get SIGTERM, which lets the CLI finish writing."""
    if close_stdin and process.returncode is None and process.stdin is not None:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), CLAUDE_EXIT_GRACE_SECONDS)
        except TimeoutError:
            pass
    if process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), CLAUDE_EXIT_GRACE_SECONDS)
        except ProcessLookupError:
            pass
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    await process.wait()


def _window_at_limit(window: dict[str, Any] | None) -> bool:
    used = (window or {}).get("used_percent")
    return isinstance(used, (int, float)) and used >= 100


def _claude_usage_window(bucket: Any, window_seconds: int | None) -> dict[str, Any] | None:
    """One usage bucket → the same window shape transforms.py produces for
    OpenAI, so the UI's single renderer covers both providers."""
    if not isinstance(bucket, dict):
        return None
    used_raw = bucket.get("utilization")
    used = float(used_raw) if isinstance(used_raw, (int, float)) and not isinstance(used_raw, bool) and math.isfinite(used_raw) else None
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
        "remaining_percent": round(max(0, 100.0 - used), 2) if used is not None else None,
        "window_seconds": window_seconds,
        "window_minutes": window_seconds // 60 if window_seconds is not None else None,
        "window_label": "weekly" if window_seconds == CLAUDE_SEVEN_DAY_SECONDS else "5h" if window_seconds == CLAUDE_FIVE_HOUR_SECONDS else None,
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
        from airelay.claude_accounts import ClaudeAccountPool

        self._claude = (
            ClaudeAccountPool(settings, traffic)
            if settings.enable_claude
            else None
        )

    @property
    def claude_runtime(self) -> Any:
        return self._claude

    def resolve_model(self, model_id: str) -> ResolvedModel:
        if self._claude is not None:
            resolved = self._claude.resolve_model(model_id)
            if resolved is not None:
                return resolved
            if model_id.startswith("claude:") or model_id.startswith("claude-"):
                raise ProviderError(
                    404,
                    f"Unknown Claude model `{model_id}`. Configure it under `[providers.claude].models` first.",
                    code="model_not_found",
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

    async def ensure_openai_model_supported(self, model_id: str, request_id: str) -> None:
        if self._openai_backend is None:
            return
        extra_models = {
            configured.lower(): configured
            for configured in self._settings.openai_extra_models
            if configured
        }
        normalized = model_id.lower()
        if normalized in extra_models:
            return
        try:
            payload = await self._openai_models_payload(request_id)
        except Exception:  # noqa: BLE001 - lack of catalog evidence must not break serving
            return
        supported_models = _openai_supported_model_ids(payload, self._settings.openai_extra_models)
        supported = {supported_model.lower(): supported_model for supported_model in supported_models}
        if normalized in supported:
            return
        detail = (
            f"Unsupported OpenAI model `{model_id}` for the authenticated ChatGPT/Codex account. "
            "Use a model id from `/v1/models`."
        )
        suggestions = _suggest_openai_models(model_id, supported_models)
        if suggestions:
            detail += f" Closest supported ids: {', '.join(suggestions)}."
        detail += (
            " If the upstream really serves this id but omits it from `/models`, "
            "add it to `[providers.openai].extra_models`."
        )
        raise ProviderError(404, detail, code="model_not_found")

    async def retrieve_model(self, model_id: str, request_id: str) -> dict[str, Any]:
        """`GET /v1/models/{model}`: one listed model, matched
        case-insensitively like admission. Unlike admission, it does not
        fail open: an OpenAI catalog failure is raised, not answered with a
        model nobody listed."""
        if self._claude is not None:
            await self._claude.refresh_models()
        try:
            resolved = self.resolve_model(model_id) if model_id else None
        except ProviderError as error:
            raise ProviderError(404, error.detail, code="model_not_found") from error
        if resolved is not None and resolved.provider == "openai" and self._openai_backend is not None:
            await self._openai_models_payload(request_id)
        for model in (await self.list_models(request_id))["data"]:
            if (resolved is not None and model["id"].lower() == resolved.public_id.lower()
                    and model.get("airelays", {}).get("provider") == resolved.provider):
                return model
        raise ProviderError(404, f"The model `{model_id}` does not exist.", code="model_not_found")

    async def list_models(self, request_id: str, *, refresh: bool = False) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        openai_error: Exception | None = None
        if self._settings.enable_openai_provider and self._openai_backend is not None:
            try:
                payload = await self._openai_models_payload(request_id, force=refresh)
                catalog = {
                    item["slug"]: item for item in payload.get("models", [])
                    if isinstance(item, dict) and isinstance(item.get("slug"), str)
                } if isinstance(payload.get("models"), list) else {}
                for slug in _openai_catalog_model_ids(payload):
                    data.append(_openai_model_record(slug, catalog[slug]).as_wire())
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
            await self._claude.refresh_models(force=refresh)
            data.extend(self._claude.list_models())
        if data:
            return {"object": "list", "data": data}
        if openai_error is not None:
            raise openai_error
        return {"object": "list", "data": []}

    def _models_cache_ttl_seconds(self) -> float:
        return max(0.0, float(self._settings.models_cache_ttl_seconds))

    @staticmethod
    def _pool_slot_cache_fragment(slot: Any) -> str:
        if isinstance(slot, dict):
            slug = slot.get("slug")
            account_id = slot.get("account_id")
            authenticated = slot.get("authenticated")
        else:
            slug = getattr(slot, "slug", None)
            account_id = getattr(slot, "account_id", None)
            authenticated = getattr(slot, "authenticated", None)
        return f"{slug or ''}:{account_id or ''}:{int(bool(authenticated))}"

    def _current_openai_account_pool_cache_key(self) -> tuple[str, ...]:
        if self._account_pool is None:
            return ()
        refresh = getattr(self._account_pool, "refresh_if_changed", None)
        if callable(refresh):
            refresh()
        slots = getattr(self._account_pool, "slots", None)
        if not callable(slots):
            return ()
        try:
            current_slots = slots()
        except Exception:  # noqa: BLE001 - cache-key refresh must not break serving
            return ()
        return tuple(self._pool_slot_cache_fragment(slot) for slot in current_slots)

    def _current_openai_models_cache_key(self) -> tuple[str | None, ...] | None:
        record = self._openai_auth.load()
        if record is None or not record.authenticated or not record.account_matches_binding():
            return None
        return (
            self._settings.upstream_base_url,
            self._settings.client_version,
            record.account_id,
            record.bound_account_id,
            *self._current_openai_account_pool_cache_key(),
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

    async def _openai_models_payload(self, request_id: str, *, force: bool = False) -> dict[str, Any]:
        if self._openai_backend is None:
            return {"models": []}
        now = time.monotonic()
        cache_key = self._current_openai_models_cache_key()
        cached = self._cached_openai_models_payload(now, cache_key)
        if cached is not None and not force:
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
            if cached is not None and not force:
                self._log_openai_models_cache(request_id, "hit", now)
                return cached
            if force:
                invalidate = getattr(self._openai_backend, "invalidate_models_cache", None)
                if callable(invalidate):
                    invalidate()
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

    async def warm_status_probes(self) -> None:
        """Call before provider_statuses() on the event loop."""
        if self._claude is not None:
            await self._claude.warm_status_probes()

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
        if self._settings.enable_openai_provider:
            keyring_era = keyring_era_sign_ins(self._settings)
            if keyring_era:
                openai_status["keyring_era_sign_ins"] = keyring_era
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
    def claude(self) -> Any:
        return self._claude
