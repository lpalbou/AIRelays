from __future__ import annotations

import asyncio

import pytest

from airelay.providers import ClaudeCliRuntime, ProviderError, ProviderRegistry
from airelay.config import Settings


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
async def test_claude_runtime_maps_response_format_json_object_to_permissive_schema(tmp_path, monkeypatch) -> None:
    """`json_object` promises "a valid JSON object" without a caller schema;
    the equivalent native enforcement is the permissive object schema."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run_json(request, request_id):
        captured["request"] = request
        return {"result": "{\"ok\": true}", "structured_output": {"ok": True}, "stop_reason": "tool_use"}

    monkeypatch.setattr(runtime, "_run_json", fake_run_json)
    await runtime.create_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Reply in JSON."}],
            "response_format": {"type": "json_object"},
        },
        "req_1",
    )
    import json as _json

    assert _json.loads(captured["request"].output_schema) == {"type": "object"}


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
            "response_format": {"type": "json_object"},
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
            "response_format": {"type": "json_object"},
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
                "response_format": {"type": "json_object"},
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
                "response_format": {"type": "json_object"},
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


def _install_fake_claude(tmp_path) -> str:
    import os
    import sys as _sys

    script = tmp_path / "fake-claude"
    script.write_text(FAKE_CLAUDE_CLI.replace("#!/usr/bin/env python3", f"#!{_sys.executable}"), encoding="utf-8")
    script.chmod(script.stat().st_mode | 0o111)
    return str(script)


@pytest.mark.asyncio
async def test_claude_end_to_end_json_schema_through_a_real_subprocess(tmp_path) -> None:
    """Full path over the actual subprocess boundary with a fake `claude`
    binary speaking the probed CLI envelope: response_format travels as
    --json-schema argv, the result envelope's structured_output becomes the
    message content, and --effort rides along."""
    settings = make_settings(tmp_path, claude_bin=_install_fake_claude(tmp_path))
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
    settings = make_settings(tmp_path, claude_bin=_install_fake_claude(tmp_path))
    runtime = ClaudeCliRuntime(settings)

    collected = []
    async for chunk in runtime.stream_chat_completion(
        {
            "model": "claude:sonnet",
            "messages": [{"role": "user", "content": "Rate it."}],
            "stream": True,
            "response_format": {"type": "json_object"},
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
    assert openai_wire["airelays"]["structured_output"]["types"] == ["json_schema"]


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
                "response_format": {"type": "json_object"},
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


def test_subprocess_env_injects_stored_claude_token(tmp_path, monkeypatch) -> None:
    """A token stored via `airelays claude set-token` must reach every
    spawned claude child, and must beat any ambient environment value —
    explicit configuration over invisible shell state."""
    settings = make_settings(tmp_path)
    settings.claude_oauth_token_file = tmp_path / "data" / "claude-token"
    settings.write_claude_oauth_token("file-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")

    runtime = ClaudeCliRuntime(settings)
    env = runtime._subprocess_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "file-token"

    token_mode = settings.claude_oauth_token_file.stat().st_mode & 0o777
    assert token_mode == 0o600
    assert settings.claude_oauth_token_source() == "file"


def test_subprocess_env_falls_back_to_ambient_token(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings.claude_oauth_token_file = tmp_path / "data" / "claude-token"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")

    runtime = ClaudeCliRuntime(settings)
    env = runtime._subprocess_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient-token"
    assert settings.claude_oauth_token_source() == "env"


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


def test_claude_usage_serves_stale_snapshot_during_rate_limit(tmp_path) -> None:
    """A 429 from the undocumented usage endpoint must not blank the UI:
    the last good snapshot is served, annotated as stale with the retry
    horizon, and no further upstream request is made inside the window."""
    import time as _time

    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    runtime._usage_last_good = {"account": {"email": "a@b.c"}, "rate_limits": {"default": None, "additional": []}}
    runtime._usage_last_good_epoch = _time.time() - 120
    runtime._usage_blocked_until = _time.monotonic() + 1800

    stale = runtime._stale_or_usage_error(_time.monotonic(), "rate_limited")

    assert stale["stale"] is True
    assert stale["stale_reason"] == "rate_limited"
    assert 0 < stale["retry_after_seconds"] <= 1800
    assert stale["account"]["email"] == "a@b.c"
    # The stored snapshot itself must stay unannotated (deep-copied).
    assert "stale" not in runtime._usage_last_good


def test_claude_usage_raises_actionable_error_without_snapshot(tmp_path) -> None:
    import time as _time

    import pytest as _pytest

    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    runtime._usage_blocked_until = _time.monotonic() + 3600

    with _pytest.raises(ProviderError) as excinfo:
        runtime._stale_or_usage_error(_time.monotonic(), "rate_limited")

    assert excinfo.value.status_code == 503
    assert "rate-limit" in str(excinfo.value).lower()
    assert excinfo.value.code == "provider_rate_limited"


def test_retry_after_seconds_clamps_and_defaults() -> None:
    from airelay.providers import _retry_after_seconds

    assert _retry_after_seconds(None) == 3600
    assert _retry_after_seconds("garbage") == 3600
    assert _retry_after_seconds("120") == 120
    assert _retry_after_seconds("5") == 60          # floor: no hammering
    assert _retry_after_seconds("999999") == 7200   # ceiling: no multi-day wedge


@pytest.mark.asyncio
async def test_claude_usage_429_blocks_upstream_until_window_passes(tmp_path, monkeypatch) -> None:
    """End-to-end orchestration: a 429 sets the block from retry-after, no
    request is sent inside the window, and the first call after the window
    succeeds and clears the block."""
    import time as _time

    import httpx as _httpx

    calls = {"count": 0}
    responses = [
        _httpx.Response(429, headers={"retry-after": "120"}, json={"error": "rate_limit"}),
        _httpx.Response(200, json={"five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00+00:00"}}),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            response = responses[min(calls["count"], len(responses) - 1)]
            calls["count"] += 1
            return response

    monkeypatch.setattr("airelay.providers.httpx.AsyncClient", FakeClient)
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    monkeypatch.setattr(runtime, "_resolve_usage_token", lambda: ("tok", "keychain"))

    # 1) First call hits upstream, gets 429, no snapshot yet → clear error.
    with pytest.raises(ProviderError) as excinfo:
        await runtime.get_subscription_status("req-1")
    assert excinfo.value.code == "provider_rate_limited"
    assert calls["count"] == 1
    assert runtime._usage_blocked_until > _time.monotonic()

    # 2) Inside the window: no upstream request at all.
    with pytest.raises(ProviderError):
        await runtime.get_subscription_status("req-2")
    assert calls["count"] == 1

    # 3) Window passed (and attempt spacing elapsed): fetch succeeds,
    # block clears, payload is fresh.
    runtime._usage_blocked_until = _time.monotonic() - 1
    runtime._usage_last_attempt_at = _time.monotonic() - 3600
    payload = await runtime.get_subscription_status("req-3")
    assert calls["count"] == 2
    assert "stale" not in payload
    assert payload["rate_limits"]["default"]["primary_window"]["used_percent"] == 10.0
    assert runtime._usage_blocked_until == 0.0

    # 4) Fresh cache: still no extra upstream call.
    await runtime.get_subscription_status("req-4")
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_claude_usage_never_retries_a_rejected_token(tmp_path, monkeypatch) -> None:
    """A 401 marks the token dead: no upstream retry with the same token
    (even after the attempt-spacing interval), automatic recovery when the
    resolved token changes, and the guardrail survives a restart."""
    import time as _time

    import httpx as _httpx

    calls = {"count": 0}
    responses = [
        _httpx.Response(401, json={"error": "auth"}),
        _httpx.Response(200, json={"five_hour": {"utilization": 5.0, "resets_at": "2099-01-01T00:00:00+00:00"}}),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            response = responses[min(calls["count"], len(responses) - 1)]
            calls["count"] += 1
            return response

    monkeypatch.setattr("airelay.providers.httpx.AsyncClient", FakeClient)
    settings = make_settings(tmp_path)
    runtime = ClaudeCliRuntime(settings)
    token = {"value": "dead-token"}
    monkeypatch.setattr(runtime, "_resolve_usage_token", lambda: (token["value"], "keychain"))

    # 1) 401 → actionable error, token fingerprinted as rejected.
    with pytest.raises(ProviderError) as excinfo:
        await runtime.get_subscription_status("req-1")
    assert "renews automatically" in str(excinfo.value)
    assert calls["count"] == 1

    # 2) Same token, attempt spacing elapsed → still no upstream call.
    runtime._usage_last_attempt_at = _time.monotonic() - 3600
    with pytest.raises(ProviderError):
        await runtime.get_subscription_status("req-2")
    assert calls["count"] == 1

    # 3) Restart: guardrail persists via the state file.
    restarted = ClaudeCliRuntime(settings)
    monkeypatch.setattr(restarted, "_resolve_usage_token", lambda: (token["value"], "keychain"))
    with pytest.raises(ProviderError):
        await restarted.get_subscription_status("req-3")
    assert calls["count"] == 1

    # 4) Token rotates → fetch allowed again, succeeds, block cleared.
    token["value"] = "fresh-token"
    restarted._usage_last_attempt_at = _time.monotonic() - 3600
    payload = await restarted.get_subscription_status("req-4")
    assert calls["count"] == 2
    assert payload["rate_limits"]["default"]["primary_window"]["used_percent"] == 5.0
    assert restarted._usage_rejected_fingerprint is None


def test_resolve_usage_token_reports_expired_vs_absent(tmp_path, monkeypatch) -> None:
    """A lapsed-but-present CLI credential is 'expired' (renews itself), not
    'none' (needs sign-in) — the distinction the incident hinged on."""
    runtime = ClaudeCliRuntime(make_settings(tmp_path))
    monkeypatch.setattr(
        type(runtime._settings), "resolve_claude_oauth_token", lambda self: None
    )
    import json as _json
    import sys as _sys

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(_sys, "platform", "linux")  # skip keychain probe

    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("airelay.providers.Path.home", staticmethod(lambda: tmp_path))

    creds.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 1}}), encoding="utf-8")
    token, source = runtime._resolve_usage_token()
    assert token is None and source == "expired"

    creds.write_text(_json.dumps({}), encoding="utf-8")
    token, source = runtime._resolve_usage_token()
    assert token is None and source == "none"

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    token, source = runtime._resolve_usage_token()
    assert token == "env-tok" and source == "env"


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
