from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from airelay.claude_auth import new_profile
from claude_fakes import use_fake_cli
from airelay.providers import ClaudeCliRuntime, ProviderError, ProviderRegistry
from airelay.config import Settings

# Captured before conftest stubs it out for every other test.
REAL_CONTROL_REQUEST = ClaudeCliRuntime._control_request


def make_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        enable_claude=True,
        claude_models=("claude:sonnet",),
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class _FakeAuthManager:
    @staticmethod
    def load():
        return _FakeAuthRecord()

    @staticmethod
    def status() -> dict[str, object]:
        return {
            "ready_for_requests": True,
            "authenticated": True,
            "account_bound": True,
            "email": "user@example.com",
        }


class _FakeAuthRecord:
    authenticated = True
    account_id = "acct_123"
    bound_account_id = "acct_123"

    @staticmethod
    def account_matches_binding() -> bool:
        return True


class _FakeOpenAIBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self, request_id: str) -> dict[str, object]:
        del request_id
        self.calls += 1
        await asyncio.sleep(0.01)
        return {"models": [{"slug": "gpt-concurrent-cache"}]}


class _DynamicPoolOpenAIBackend:
    def __init__(self, pool: "_FakeAccountPool") -> None:
        self.calls = 0
        self._pool = pool

    async def list_models(self, request_id: str) -> dict[str, object]:
        del request_id
        self.calls += 1
        if len(self._pool.slots()) > 1:
            return {"models": [{"slug": "gpt-shared"}]}
        return {"models": [{"slug": "gpt-shared"}, {"slug": "gpt-pool-a-only"}]}


class _FakeAccountPool:
    def __init__(self) -> None:
        self._slots = [SimpleNamespace(slug="default", account_id="acct_123", authenticated=True)]
        self.refresh_calls = 0

    def refresh_if_changed(self) -> bool:
        self.refresh_calls += 1
        return False

    def slots(self) -> list[SimpleNamespace]:
        return list(self._slots)

    def add_account(self, slug: str, account_id: str, *, authenticated: bool = True) -> None:
        self._slots.append(
            SimpleNamespace(slug=slug, account_id=account_id, authenticated=authenticated)
        )


@pytest.mark.asyncio
async def test_claude_runtime_creates_chat_completion_from_text_messages(tmp_path, monkeypatch) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        captured["request_id"] = request_id
        return {
            "result": "Claude says hi",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 4},
        }

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)

    payload = await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Say hi"},
            ],
        },
        "req_123",
    )

    request = captured["request"]
    assert request.public_model == "claude:sonnet"
    assert request.upstream_model == "sonnet"
    assert request.system_prompt == "Be terse."
    assert request.prompt == "Say hi"
    assert payload["choices"][0]["message"]["content"] == "Claude says hi"
    assert payload["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_claude_runtime_rejects_tools_on_chat_route(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    with pytest.raises(ProviderError, match="does not support `tools`"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "name": "lookup"}],
            },
            "req_123",
        )


@pytest.mark.asyncio
async def test_claude_runtime_forwards_reasoning_effort_to_the_cli(tmp_path, monkeypatch) -> None:
    """`reasoning_effort` on claude:* requests becomes the CLI's --effort
    flag — the mechanism verified against the real claude binary."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {"result": "ok", "stop_reason": "end_turn"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "XHigh",
        },
        "req_1",
    )
    request = captured["request"]
    assert request.effort == "xhigh"  # normalized
    command = runtime._build_command(request, stream=False)
    assert command[command.index("--effort") + 1] == "xhigh"

    # Without the parameter, no flag: the model's adaptive default rules.
    await runtime.create_chat_completion(
        {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]},
        "req_2",
    )
    assert "--effort" not in runtime._build_command(captured["request"], stream=False)


@pytest.mark.asyncio
async def test_claude_runtime_rejects_unsupported_reasoning_effort(tmp_path) -> None:
    """The claude CLI silently ignores unknown --effort values, which would
    be silent degradation — the relay rejects them with the supported list."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    with pytest.raises(ProviderError, match="low, medium, high, xhigh, max"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": "ultrathink",
            },
            "req_1",
        )


@pytest.mark.asyncio
async def test_claude_runtime_maps_response_format_json_schema_to_the_cli(tmp_path, monkeypatch) -> None:
    """OpenAI-style `response_format.type=json_schema` becomes the CLI's
    --json-schema flag (native enforcement), and the response content is the
    enforced JSON — never the model's surrounding prose."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {
            # With --json-schema the CLI reports the conforming object in
            # `structured_output`; `result` carries the same JSON as text.
            "result": "{\n  \"response\": 4\n}",
            "structured_output": {"response": 4},
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 9, "output_tokens": 21},
        }

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    payload = await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Rate it."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "rating",
                    "schema": {
                        "type": "object",
                        "properties": {"response": {"type": "integer"}},
                        "required": ["response"],
                        "additionalProperties": False,
                    },
                },
            },
        },
        "req_1",
    )

    request = captured["request"]
    assert request.output_schema is not None
    import json as _json

    assert _json.loads(request.output_schema) == {
        "type": "object",
        "properties": {"response": {"type": "integer"}},
        "required": ["response"],
        "additionalProperties": False,
    }
    command = runtime._build_command(request, stream=False)
    assert command[command.index("--json-schema") + 1] == request.output_schema
    # The typed structured_output field wins; content is the JSON only.
    assert _json.loads(payload["choices"][0]["message"]["content"]) == {"response": 4}
    assert payload["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_claude_json_mode_is_an_instruction_plus_a_local_check(tmp_path, monkeypatch) -> None:
    """`json_object` is not a CLI schema (a schema naming no fields makes
    haiku models answer `{}`): the system prompt asks for one JSON object and
    the answer is checked, with a surrounding code fence removed."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}
    answers = iter(['```json\n{"ok": true}\n```', "Sure! Here it is."])

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {"result": next(answers), "stop_reason": "end_turn"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    body = {"model": "claude:sonnet", "messages": [{"role": "system", "content": "Be brief."},
                                                     {"role": "user", "content": "Reply in JSON."}],
            "response_format": {"type": "json_object"}}
    payload = await runtime.create_chat_completion(body, "req_1")
    request = captured["request"]
    assert request.output_schema is None and request.json_mode
    assert request.system_prompt.startswith("Be brief.") and "single valid JSON object" in request.system_prompt
    assert payload["choices"][0]["message"]["content"] == '{"ok": true}'
    with pytest.raises(ProviderError) as error:
        await runtime.create_chat_completion(body, "req_2")
    assert error.value.code == "invalid_json_output" and "Sure! Here it is." in error.value.detail


def test_claude_requests_always_carry_a_system_prompt(tmp_path) -> None:
    """Without one the CLI applies Claude Code's own agent prompt (about
    3,000 tokens), which an OpenAI-compatible request must not carry."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    chat = runtime._prepare_chat_request({"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]})
    completion = runtime._prepare_completion_request({"model": "claude:sonnet", "prompt": "hi"})
    for request in (chat, completion):
        command = runtime._build_command(request, stream=False)
        assert command[command.index("--system-prompt") + 1] == "."


@pytest.mark.asyncio
async def test_claude_runtime_response_format_text_is_a_noop(tmp_path, monkeypatch) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {"result": "plain text", "stop_reason": "end_turn"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    payload = await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "text"},
        },
        "req_1",
    )
    assert captured["request"].output_schema is None
    assert "--json-schema" not in runtime._build_command(captured["request"], stream=False)
    assert payload["choices"][0]["message"]["content"] == "plain text"


@pytest.mark.asyncio
async def test_claude_runtime_rejects_unsupported_response_format_shapes(tmp_path) -> None:
    """Unknown response_format types and malformed json_schema payloads are
    rejected loudly instead of degrading to unenforced text."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    with pytest.raises(ProviderError, match="Supported types: text, json_object, json_schema"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": "xml"},
            },
            "req_1",
        )
    with pytest.raises(ProviderError, match="requires a `json_schema` object"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": "json_schema"},
            },
            "req_2",
        )
    with pytest.raises(ProviderError, match="`response_format.json_schema.schema` must be an object"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "x"}},
            },
            "req_3",
        )


def _collect_stream_chunks(sse_bytes: list[bytes]) -> list[dict]:
    import json as _json

    chunks = []
    for raw in sse_bytes:
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunks.append(_json.loads(line[len("data: "):]))
    return chunks


@pytest.mark.asyncio
async def test_claude_stream_with_schema_streams_only_the_enforced_json(tmp_path, monkeypatch) -> None:
    """Streaming a schema-enforced request must deliver the JSON text (the
    CLI's StructuredOutput input_json_delta fragments) as the content deltas,
    and suppress the model's surrounding prose — a client that asked for
    JSON must never receive non-JSON content."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    async def fake_stream(request, request_id):
        events = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "let me think"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Here is your JSON: "}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": ""}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{\"response\": 4"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "}"}}},
            {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 9, "output_tokens": 30}}},
            {"type": "result", "result": "{\"response\": 4}", "structured_output": {"response": 4}, "stop_reason": "tool_use", "usage": {"input_tokens": 9, "output_tokens": 30}},
        ]
        for event in events:
            yield event

    monkeypatch.setattr(runtime, "_run_stream", fake_stream)
    collected = []
    async for chunk in runtime.stream_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Rate it."}],
            "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
        },
        "req_1",
    ):
        collected.append(chunk)

    chunks = _collect_stream_chunks(collected)
    content = "".join(
        (chunk["choices"][0].get("delta") or {}).get("content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    import json as _json

    assert _json.loads(content) == {"response": 4}
    assert "Here is your JSON" not in content
    finish_reasons = [chunk["choices"][0].get("finish_reason") for chunk in chunks if chunk.get("choices")]
    assert "stop" in finish_reasons


@pytest.mark.asyncio
async def test_claude_stream_with_schema_falls_back_to_the_result_envelope(tmp_path, monkeypatch) -> None:
    """If no StructuredOutput fragments streamed (CLI shape drift), the
    result envelope still carries the enforced JSON; it is emitted as one
    content chunk rather than dropped."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    async def fake_stream(request, request_id):
        events = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "prose"}}},
            {"type": "result", "result": "{\"response\":2}", "structured_output": {"response": 2}, "stop_reason": "tool_use", "usage": {"input_tokens": 4, "output_tokens": 8}},
        ]
        for event in events:
            yield event

    monkeypatch.setattr(runtime, "_run_stream", fake_stream)
    collected = []
    async for chunk in runtime.stream_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Rate it."}],
            "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
        },
        "req_1",
    ):
        collected.append(chunk)

    chunks = _collect_stream_chunks(collected)
    content = "".join(
        (chunk["choices"][0].get("delta") or {}).get("content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    import json as _json

    assert _json.loads(content) == {"response": 2}
    assert "prose" not in content


@pytest.mark.asyncio
async def test_claude_runtime_rejects_response_format_without_a_type(tmp_path) -> None:
    """A non-empty response_format with no `type` must not silently degrade
    to unenforced text (OpenAI rejects the same shape)."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    with pytest.raises(ProviderError, match="Supported types: text, json_object, json_schema"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"json_schema": {"name": "x", "schema": {"type": "object"}}},
            },
            "req_1",
        )
    # Non-string type values (unhashable included) get the same clean 422.
    with pytest.raises(ProviderError, match="Supported types"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": {}},
            },
            "req_2",
        )


@pytest.mark.asyncio
async def test_claude_runtime_rejects_oversized_and_nonfinite_schemas(tmp_path) -> None:
    """The schema rides the CLI argv: oversized schemas must 422 before the
    spawn can fail with a raw E2BIG, and NaN/Infinity are not JSON."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    huge = {"type": "object", "properties": {f"k{i}": {"type": "string"} for i in range(9000)}}
    with pytest.raises(ProviderError, match="too large"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": huge}},
            },
            "req_1",
        )
    with pytest.raises(ProviderError, match="NaN and Infinity"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "x", "schema": {"type": "object", "x": float("nan")}},
                },
            },
            "req_2",
        )


@pytest.mark.asyncio
async def test_claude_runtime_never_serves_prose_as_enforced_json(tmp_path, monkeypatch) -> None:
    """If a schema-enforced run yields no structured_output and a `result`
    that does not parse as JSON (refusal, truncation), the request must fail
    loudly — prose served under a JSON contract is silent degradation."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    async def fake_run_json(request, request_id):
        return {"result": "I'm sorry, I can't produce that.", "stop_reason": "end_turn"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    with pytest.raises(ProviderError, match="no schema-conforming output"):
        await runtime.create_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "Rate it."}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
            },
            "req_1",
        )


@pytest.mark.asyncio
async def test_claude_stream_surfaces_cli_error_envelopes_instead_of_content(tmp_path, monkeypatch) -> None:
    """The CLI can exit 0 while reporting failure in-band (`is_error`,
    error subtypes). The stream must raise — never narrate the error text as
    assistant content (the non-stream path has always guarded this)."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    async def fake_stream(request, request_id):
        events = [
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "API Error: 401 invalid bearer token",
            },
        ]
        for event in events:
            yield event

    monkeypatch.setattr(runtime, "_run_stream", fake_stream)
    with pytest.raises(ProviderError, match="401 invalid bearer token"):
        async for _ in runtime.stream_chat_completion(
            {
                "model": "claude:sonnet",
                "messages": [{"role": "user", "content": "Rate it."}],
                "stream": True,
                "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
            },
            "req_1",
        ):
            pass


FAKE_CLAUDE_CLI = '''#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
prompt = sys.stdin.read()


def flag(name):
    return args[args.index(name) + 1] if name in args else None


schema_arg = flag("--json-schema")
out_format = flag("--output-format")
if schema_arg is None:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "no schema", "stop_reason": "end_turn", "argv": args}))
    sys.exit(0)
schema = json.loads(schema_arg)  # must be valid JSON on the argv boundary
if out_format == "json":
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "{\\"response\\":5}", "structured_output": {"response": 5},
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 3, "output_tokens": 9},
        "argv": args,
    }))
else:
    events = [
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "text_delta", "text": "prose to suppress"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "input_json_delta", "partial_json": "{\\"response\\""}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "input_json_delta", "partial_json": ":5}"}}},
        {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                           "usage": {"input_tokens": 3, "output_tokens": 9}}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "{\\"response\\":5}", "structured_output": {"response": 5},
         "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 9}},
    ]
    for event in events:
        print(json.dumps(event))
'''


def _fake_claude_settings(tmp_path) -> Settings:
    script = tmp_path / "fake-claude.py"
    script.write_text(FAKE_CLAUDE_CLI, encoding="utf-8")
    settings = make_settings(tmp_path)
    use_fake_cli(settings, script)
    return settings


@pytest.mark.asyncio
async def test_claude_end_to_end_json_schema_through_a_real_subprocess(tmp_path) -> None:
    """Full path over the actual subprocess boundary with a fake `claude`
    binary speaking the probed CLI envelope: response_format travels as
    --json-schema argv, the result envelope's structured_output becomes the
    message content, and --effort rides along."""
    settings = _fake_claude_settings(tmp_path)
    runtime = ClaudeCliRuntime(settings)

    payload = await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Rate it."}],
            "reasoning_effort": "low",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "rating", "schema": {"type": "object"}},
            },
        },
        "req_e2e",
    )

    import json as _json

    assert _json.loads(payload["choices"][0]["message"]["content"]) == {"response": 5}
    assert payload["usage"] == {"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12}


@pytest.mark.asyncio
async def test_claude_end_to_end_streaming_json_schema_through_a_real_subprocess(tmp_path) -> None:
    settings = _fake_claude_settings(tmp_path)
    runtime = ClaudeCliRuntime(settings)

    collected = []
    async for chunk in runtime.stream_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Rate it."}],
            "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
            "stream_options": {"include_usage": True},
        },
        "req_e2e_stream",
    ):
        collected.append(chunk)

    chunks = _collect_stream_chunks(collected)
    content = "".join(
        (chunk["choices"][0].get("delta") or {}).get("content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    import json as _json

    assert _json.loads(content) == {"response": 5}
    assert "prose" not in content
    usage_chunks = [chunk for chunk in chunks if chunk.get("usage")]
    assert usage_chunks and usage_chunks[-1]["usage"]["total_tokens"] == 12


def test_model_records_expose_structured_output_types(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    claude_wire = runtime.list_models()[0]
    assert claude_wire["airelays"]["structured_output"]["parameter"] == "response_format"
    assert claude_wire["airelays"]["structured_output"]["types"] == ["json_schema", "json_object"]

    from airelay.providers import _openai_model_record

    openai_wire = _openai_model_record("gpt-5.5").as_wire()
    assert openai_wire["airelays"]["structured_output"]["types"] == ["json_schema", "json_object"]


def test_model_records_expose_reasoning_modes(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    claude_wire = runtime.list_models()[0]
    assert claude_wire["airelays"]["reasoning"]["modes"] == [
        "low", "medium", "high", "xhigh", "max",
    ]
    assert claude_wire["airelays"]["reasoning"]["parameter"] == "reasoning_effort"

    from airelay.providers import _openai_model_record

    openai_wire = _openai_model_record("gpt-5.5").as_wire()
    assert openai_wire["airelays"]["reasoning"]["modes"] == [
        "none", "low", "medium", "high", "xhigh",
    ]
    assert openai_wire["airelays"]["reasoning"]["default"] == "none"


@pytest.mark.asyncio
async def test_claude_runtime_rejects_stop_on_completions_route(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    with pytest.raises(ProviderError, match="does not support `stop`"):
        await runtime.create_completion(
            {
                "model": "claude:sonnet",
                "prompt": "hello",
                "stop": ["END"],
            },
            "req_123",
        )


@pytest.mark.asyncio
async def test_claude_runtime_forwards_reasoning_effort_on_completions_route(tmp_path, monkeypatch) -> None:
    """`reasoning_effort` is honored on `/v1/completions` too — same
    validation and the same --effort argv as the chat route."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {"result": "ok", "stop_reason": "end_turn"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    await runtime.create_completion(
        {"model": "claude:sonnet", "prompt": "hello", "reasoning_effort": "max"},
        "req_1",
    )
    request = captured["request"]
    assert request.effort == "max"
    for stream in (False, True):
        command = runtime._build_command(request, stream=stream)
        assert command[command.index("--effort") + 1] == "max"


@pytest.mark.asyncio
async def test_claude_runtime_rejects_response_format_on_completions_route(tmp_path) -> None:
    """`response_format` is not part of the completions API; ignoring it
    would silently hand unenforced text to a client that asked for JSON."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    with pytest.raises(ProviderError, match="does not support `response_format`"):
        await runtime.create_completion(
            {
                "model": "claude:sonnet",
                "prompt": "hello",
                "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}},
            },
            "req_123",
        )


@pytest.mark.asyncio
async def test_claude_runtime_accepts_sampling_parameters_without_forwarding_them(tmp_path, monkeypatch) -> None:
    """Sampling parameters must not fail the request at the runtime boundary:
    the app layer strips and discloses them (the claude CLI has no sampling
    controls), so a body that still carries them is simply ignored here."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {"result": "ok", "stop_reason": "end_turn"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)

    payload = await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.5,
            "top_p": 0.9,
        },
        "req_123",
    )

    assert payload["choices"][0]["message"]["content"] == "ok"
    assert captured["request"].prompt == "hello"


def test_provider_registry_marks_disabled_openai_runtime_not_ready(tmp_path) -> None:
    settings = make_settings(tmp_path, enable_openai_provider=False)
    registry = ProviderRegistry(settings, openai_auth=_FakeAuthManager())

    statuses = registry.provider_statuses()

    assert statuses["openai"]["enabled"] is False
    assert statuses["openai"]["ready_for_requests"] is False


@pytest.mark.asyncio
async def test_provider_registry_collapses_concurrent_openai_model_cache_misses(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        enable_claude=False,
        models_cache_ttl_seconds=300.0,
    )
    backend = _FakeOpenAIBackend()
    registry = ProviderRegistry(
        settings,
        openai_auth=_FakeAuthManager(),  # type: ignore[arg-type]
        openai_backend=backend,  # type: ignore[arg-type]
    )

    responses = await asyncio.gather(
        *(registry.list_models(f"req_{index}") for index in range(10))
    )

    assert backend.calls == 1
    assert {response["data"][0]["id"] for response in responses} == {"gpt-concurrent-cache"}


@pytest.mark.asyncio
async def test_openai_model_admission_invalidates_cache_when_account_pool_changes(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        enable_claude=False,
        models_cache_ttl_seconds=300.0,
    )
    pool = _FakeAccountPool()
    backend = _DynamicPoolOpenAIBackend(pool)
    registry = ProviderRegistry(
        settings,
        openai_auth=_FakeAuthManager(),  # type: ignore[arg-type]
        openai_backend=backend,  # type: ignore[arg-type]
        account_pool=pool,  # type: ignore[arg-type]
    )

    await registry.ensure_openai_model_supported("gpt-pool-a-only", "req_1")
    assert backend.calls == 1

    pool.add_account("second", "acct_456")

    with pytest.raises(ProviderError, match="Unsupported OpenAI model `gpt-pool-a-only`"):
        await registry.ensure_openai_model_supported("gpt-pool-a-only", "req_2")

    assert backend.calls == 2
    assert pool.refresh_calls >= 2


def test_subprocess_env_names_relay_profile_and_ignores_ambient_claude_state(tmp_path, monkeypatch) -> None:
    """ADR 0007: every spawned claude child runs in a relay-owned profile.
    The user's own CLAUDE_CONFIG_DIR / CLAUDE_CODE_OAUTH_TOKEN never leak in;
    only the stored setup token reaches the setup-token account."""
    settings = make_settings(tmp_path)
    settings.claude_oauth_token_file = tmp_path / "data" / "claude-token"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "personal-claude"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")

    token_runtime = ClaudeCliRuntime(settings)
    env = token_runtime._subprocess_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "data" / "claude" / "token" / "config")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert settings.claude_oauth_token_source() == "none"

    settings.write_claude_oauth_token("file-token")
    assert token_runtime._subprocess_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "file-token"
    if os.name != "nt":  # Windows protects it with the profile ACL instead
        assert settings.claude_oauth_token_file.stat().st_mode & 0o777 == 0o600
    assert settings.claude_oauth_token_source() == "file"

    managed = ClaudeCliRuntime(settings, profile=new_profile(settings))
    env = managed._subprocess_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(managed.profile.config_dir)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_claude_usage_normalizes_to_openai_shape(tmp_path) -> None:
    """The Claude usage payload must produce the exact window shape the
    OpenAI runtime produces, so one renderer covers both providers."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    runtime._last_probe = {"email": "me@example.com", "subscription_type": "pro", "version": "2.1.94"}

    normalized = runtime._normalize_usage(
        {
            "five_hour": {"utilization": 35.0, "resets_at": "2099-01-01T05:00:00+00:00"},
            "seven_day": {"utilization": 100.0, "resets_at": "2099-01-03T00:00:00+00:00"},
            "seven_day_sonnet": {"utilization": 12.0, "resets_at": "2099-01-03T00:00:00+00:00"},
            "seven_day_opus": None,
        }
    )

    assert normalized["account"] == {"email": "me@example.com", "plan_type": "pro"}
    primary = normalized["rate_limits"]["default"]["primary_window"]
    secondary = normalized["rate_limits"]["default"]["secondary_window"]
    assert primary["used_percent"] == 35.0
    assert primary["window_label"] == "5h"
    assert primary["reset_after_seconds"] > 0
    assert secondary["window_label"] == "weekly"
    # A 100% window marks the account as at its limit, like OpenAI.
    assert normalized["rate_limit_reached_type"] == "seven_day"
    additional = normalized["rate_limits"]["additional"]
    assert len(additional) == 1 and additional[0]["limit_name"] == "Sonnet"


def test_claude_usage_tolerates_missing_buckets(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    normalized = runtime._normalize_usage({})
    assert normalized["rate_limits"]["default"]["primary_window"] is None
    assert normalized["rate_limit_reached_type"] is None


def test_claude_modern_scoped_limits_and_credit_units(tmp_path):
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    spend = {"enabled": False, "disabled_reason": "out_of_credits",
             "used": {"amount_minor": 125, "currency": "USD", "exponent": 2}, "balance": None}
    payload = {
        "five_hour": {"utilization": 1},
        "seven_day_fable": {"utilization": 1},
        "nimbus_quill": {"utilization": 0},
        "limits": [
            {"kind": "session", "group": "session", "percent": 8},
            {"kind": "weekly_all", "group": "weekly", "percent": 55},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 100,
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
             "resets_at": "2099-01-03T00:00:00+00:00", "is_active": True},
        ],
        "spend": spend,
        "extra_usage": {"is_enabled": False, "used_credits": 125, "currency": "USD", "decimal_places": 2},
    }
    status = runtime._normalize_usage(payload)
    default = status["rate_limits"]["default"]
    assert default["primary_window"]["used_percent"] == 8
    assert default["secondary_window"]["used_percent"] == 55
    assert default["limit_reached"] is False
    assert status["rate_limit_reached_type"] is None
    extra, = status["rate_limits"]["additional"]
    assert extra["limit_name"] == "Fable"
    assert extra["rate_limit"]["limit_reached"] is True
    assert extra["rate_limit"]["primary_window"]["window_seconds"] == 604800
    assert status["spend"] == spend
    assert status["extra_usage"] == payload["extra_usage"]
    assert status["captured_at"]


@pytest.mark.parametrize("percent", [None, True, "unknown", float("nan"), float("inf")])
def test_claude_unknown_percent_is_not_zero(tmp_path, percent):
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    window = runtime._normalize_usage({"five_hour": {"utilization": percent}})["rate_limits"]["default"]["primary_window"]
    assert window["used_percent"] is None
    assert window["remaining_percent"] is None


def usage_answer(percent=10.0, *, live=True, as_of=None, available=True):
    """A `get_usage` control response. Live readings carry `limits`; the
    CLI's seeded fallback strips them and keeps an old breakdown."""
    limits = {"five_hour": {"utilization": percent, "resets_at": "2099-01-01T00:00:00+00:00"},
              "seven_day": {"utilization": 40, "resets_at": "2099-01-03T00:00:00+00:00"}}
    if live:
        limits["limits"] = [{"kind": "session", "group": "session", "percent": percent},
                            {"kind": "weekly_all", "group": "weekly", "percent": 40}]
    if as_of is not None:
        limits["seven_day_breakdown"] = {"as_of": as_of}
    return {"rate_limits_available": available, "rate_limits": limits if available else None}


def managed_runtime(tmp_path, answers, calls=None):
    settings = make_settings(tmp_path)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))

    async def control(request, **kwargs):
        assert request == {"subtype": "get_usage", "skip_behaviors": True}
        if calls is not None:
            calls.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    runtime._control_request = control
    return runtime


def expire_cache(runtime) -> None:
    import time as _time

    runtime._usage_last_good_epoch = _time.time() - 301
    runtime._usage_retry_at = 0.0


@pytest.mark.asyncio
async def test_claude_fresh_cache_rederives_resets_and_keeps_scopes_independent(tmp_path):
    import time
    runtime = managed_runtime(tmp_path, [])
    runtime._usage_last_good = runtime._normalize_usage({
        "five_hour": {"utilization": 100, "resets_at": "2000-01-01T00:00:00Z"},
        "seven_day_fable": {"utilization": 100, "resets_at": "2099-01-01T00:00:00Z"},
    })
    runtime._usage_last_good_epoch = time.time()
    snapshot = await runtime.get_subscription_status("req")
    assert snapshot["rate_limit_reached_type"] is None
    assert snapshot["rate_limits"]["default"]["primary_window"]["used_percent"] is None
    assert snapshot["rate_limits"]["additional"][0]["rate_limit"]["limit_reached"] is True
    assert runtime._usage_last_good["rate_limit_reached_type"] == "five_hour"
    assert snapshot["captured_at"] == runtime._usage_last_good["captured_at"]


@pytest.mark.asyncio
async def test_claude_usage_reads_live_get_usage_and_caches(tmp_path) -> None:
    calls = []
    runtime = managed_runtime(tmp_path, [usage_answer(10.0)], calls)
    payload = await runtime.get_subscription_status("req-1")
    assert "stale" not in payload
    assert payload["rate_limits"]["default"]["primary_window"]["used_percent"] == 10
    assert payload["rate_limits"]["default"]["secondary_window"]["used_percent"] == 40
    await runtime.get_subscription_status("req-2")
    assert len(calls) == 1  # fresh reading served from cache


@pytest.mark.asyncio
async def test_claude_usage_seeded_snapshot_is_stale_and_backs_off(tmp_path) -> None:
    """The CLI's fallback (no `limits`, hour-old breakdown) must never pass
    for live data: the last live reading is served, dated and explained,
    and the CLI is not asked again until the backoff passes."""
    import time as _time

    calls = []
    runtime = managed_runtime(tmp_path, [
        usage_answer(10.0),
        usage_answer(99.0, live=False, as_of="2026-01-01T00:00:00+00:00"),
        usage_answer(20.0),
    ], calls)
    await runtime.get_subscription_status("live")
    good_epoch = runtime._usage_last_good_epoch
    expire_cache(runtime)
    runtime._usage_last_good_epoch = good_epoch - 301

    stale = await runtime.get_subscription_status("seeded")
    assert stale["stale"] is True and stale["stale_reason"] == "usage_unavailable"
    assert stale["rate_limits"]["default"]["primary_window"]["used_percent"] == 10  # never the seeded 99
    assert stale["as_of_epoch"] == int(good_epoch - 301)
    assert 290 <= stale["retry_after_seconds"] <= 300
    assert "stale" not in runtime._usage_last_good

    await runtime.get_subscription_status("inside-backoff")
    assert len(calls) == 2

    runtime._usage_retry_at = _time.time() - 1
    payload = await runtime.get_subscription_status("recovered")
    assert "stale" not in payload and payload["rate_limits"]["default"]["primary_window"]["used_percent"] == 20
    assert runtime._usage_failures == 0 and len(calls) == 3


@pytest.mark.asyncio
async def test_claude_usage_without_limits_is_fresh_only_with_recent_breakdown(tmp_path) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    runtime = managed_runtime(tmp_path, [usage_answer(15.0, live=False, as_of=now)])
    payload = await runtime.get_subscription_status("req")
    assert "stale" not in payload
    assert payload["rate_limits"]["default"]["primary_window"]["used_percent"] == 15


@pytest.mark.asyncio
async def test_claude_usage_failures_back_off_exponentially_and_persist(tmp_path) -> None:
    import time as _time

    calls = []
    runtime = managed_runtime(tmp_path, [usage_answer(available=False)] * 3, calls)
    delays = []
    for attempt in range(3):
        with pytest.raises(ProviderError) as excinfo:
            await runtime.get_subscription_status(f"req-{attempt}")
        assert excinfo.value.code == "provider_rate_limited"
        assert "doesn't report usage" in excinfo.value.detail
        delays.append(round(runtime._usage_retry_at - _time.time(), -1))
        runtime._usage_retry_at = _time.time() - 1 if attempt < 2 else runtime._usage_retry_at
    assert delays == [300, 600, 1200]
    assert len(calls) == 3

    # A restart keeps the backoff: no CLI session inside the window.
    restarted = ClaudeCliRuntime(runtime._settings, profile=runtime.profile)
    restarted._control_request = runtime._control_request
    with pytest.raises(ProviderError):
        await restarted.get_subscription_status("after-restart")
    assert len(calls) == 3 and restarted._usage_failures == 3


@pytest.mark.asyncio
async def test_claude_usage_explains_unsupported_cli_and_disabled_traffic(tmp_path, monkeypatch) -> None:
    runtime = managed_runtime(tmp_path, [
        ProviderError(502, "Unsupported control request subtype: get_usage", code="cli_unsupported"),
        usage_answer(live=False, as_of="2026-01-01T00:00:00+00:00"),
    ])
    with pytest.raises(ProviderError) as excinfo:
        await runtime.get_subscription_status("old-cli")
    assert "update Claude Code" in excinfo.value.detail
    assert runtime._usage_stale_reason == "cli_unsupported"

    runtime._usage_retry_at = 0.0
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    with pytest.raises(ProviderError) as excinfo:
        await runtime.get_subscription_status("no-traffic")
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in excinfo.value.detail
    assert runtime._usage_stale_reason == "nonessential_traffic_disabled"


@pytest.mark.asyncio
async def test_claude_usage_refresh_survives_caller_cancellation(tmp_path) -> None:
    """A disconnecting client must not cancel a CLI that may be rotating
    the profile's OAuth token."""
    import asyncio

    release = asyncio.Event()
    runtime = managed_runtime(tmp_path, [])

    async def slow_control(request, **kwargs):
        await release.wait()
        return usage_answer(30.0)

    runtime._control_request = slow_control
    caller = asyncio.create_task(runtime.get_subscription_status("client"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await runtime._usage_task
    assert runtime._usage_last_good["rate_limits"]["default"]["primary_window"]["used_percent"] == 30


@pytest.mark.asyncio
async def test_setup_token_account_reports_no_usage(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    with pytest.raises(ProviderError) as excinfo:
        await runtime.get_subscription_status("req")
    assert excinfo.value.status_code == 422
    assert "setup tokens" in excinfo.value.detail


@pytest.mark.asyncio
async def test_control_request_reads_answer_and_ends_cli_gracefully(tmp_path) -> None:
    """A real child process: the request goes in on stdin, the matching
    control_response comes back, and the CLI is allowed to exit on its own
    (stdin closed), never killed while it may be persisting a token."""
    marker = tmp_path / "clean-exit"
    script = tmp_path / "fake-claude.py"
    script.write_text(f'''
import json, os, sys
request = json.loads(sys.stdin.readline())
assert os.path.basename(os.environ["CLAUDE_CONFIG_DIR"]) == "config"
print(json.dumps({{"type": "system"}}), flush=True)
print(json.dumps({{"type": "control_response", "response": {{"subtype": "success",
      "request_id": request["request_id"], "response": {{"echo": request["request"]}}}}}}), flush=True)
sys.stdin.read()
open({str(marker)!r}, "w").write("ok")
''')
    settings = make_settings(tmp_path)
    use_fake_cli(settings, script)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    answer = await REAL_CONTROL_REQUEST(runtime, {"subtype": "get_usage"}, timeout=10)
    assert answer == {"echo": {"subtype": "get_usage"}}
    assert marker.read_text() == "ok"


@pytest.mark.asyncio
async def test_control_request_maps_unknown_subtype_to_cli_unsupported(tmp_path) -> None:
    script = tmp_path / "old-claude.py"
    script.write_text('''
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"type": "control_response", "response": {"subtype": "error",
      "request_id": request["request_id"], "error": "Unsupported control request subtype: get_usage"}}), flush=True)
''')
    settings = make_settings(tmp_path)
    use_fake_cli(settings, script)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    with pytest.raises(ProviderError) as excinfo:
        await REAL_CONTROL_REQUEST(runtime, {"subtype": "get_usage"}, timeout=10)
    assert excinfo.value.code == "cli_unsupported"


def test_refresh_stale_windows_clears_rolled_over_state() -> None:
    from airelay.providers import _refresh_stale_windows

    snapshot = {
        "rate_limit_reached_type": "five_hour",
        "rate_limits": {
            "default": {
                "limit_reached": True,
                "primary_window": {"used_percent": 100, "reset_at": 1},  # long past
                "secondary_window": {"used_percent": 40, "reset_at": 4102444800},  # far future
            },
            "additional": [],
        },
    }
    _refresh_stale_windows(snapshot)
    primary = snapshot["rate_limits"]["default"]["primary_window"]
    assert primary["used_percent"] is None  # rolled over → unknown
    assert primary["reset_after_seconds"] == 0
    # A future window that is not maxed clears the account-level limit flag.
    assert snapshot["rate_limit_reached_type"] is None
    assert snapshot["rate_limits"]["default"]["limit_reached"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("pieces, expected", [
    (["```json\n", '{"a": ', "1}", "\n```"], '{"a": 1}'),
    (["I can't do that."], None),
])
async def test_claude_stream_json_mode_sends_one_checked_answer(tmp_path, monkeypatch, pieces, expected) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path))

    async def fake_stream(request, request_id):
        for piece in pieces:
            yield {"type": "stream_event", "event": {"type": "content_block_delta",
                                                     "delta": {"type": "text_delta", "text": piece}}}
        yield {"type": "result", "result": "".join(pieces), "stop_reason": "end_turn",
               "usage": {"input_tokens": 3, "output_tokens": 5}}

    monkeypatch.setattr(runtime, "_run_stream", fake_stream)
    body = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "JSON please"}], "stream": True,
            "response_format": {"type": "json_object"}}
    if expected is None:
        with pytest.raises(ProviderError, match="valid JSON object"):
            [chunk async for chunk in runtime.stream_chat_completion(body, "req_1")]
        return
    chunks = _collect_stream_chunks([chunk async for chunk in runtime.stream_chat_completion(body, "req_1")])
    contents = [c["choices"][0]["delta"]["content"] for c in chunks if c["choices"] and c["choices"][0]["delta"].get("content")]
    assert contents == [expected]


@pytest.mark.parametrize("text, expected", [
    ('{"a": NaN}', None), ('{"a": Infinity}', None), ("```JSON\n{\"a\": 1}\n```", '{"a": 1}'), ("[1, 2]", None),
])
def test_json_mode_accepts_only_one_strict_json_object(text, expected) -> None:
    from airelay.providers import _json_object_text

    assert _json_object_text(text) == expected


def test_json_mode_returns_refusals_and_cut_answers_as_is() -> None:
    from airelay.providers import _json_mode_answer

    assert _json_mode_answer("I can't help with that.", "content_filter") == "I can't help with that."
    assert _json_mode_answer('{"a": ', "length") == '{"a": '
    with pytest.raises(ProviderError, match="valid JSON object"):
        _json_mode_answer("I can't help with that.", "stop")
