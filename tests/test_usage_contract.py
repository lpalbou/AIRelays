"""The `usage` contract of the Chat Completions and Completions routes.

OpenAI's CompletionUsage (openai-python `types/completion_usage.py`;
`prompt_tokens_details.cache_write_tokens` is typed in recent releases):
`prompt_tokens`, `completion_tokens`, `total_tokens` plus optional
`prompt_tokens_details` / `completion_tokens_details`. With
`stream_options.include_usage`, every chunk carries `"usage": null` and one
extra chunk with `"choices": []` follows the finish_reason chunk, before
`[DONE]`; without it, no chunk has a `usage` key. `/v1/responses` keeps the
Responses API usage verbatim.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from airelay.app import create_app
from airelay.backend import SSEEvent
from airelay.providers import ClaudeCliRuntime
from airelay.transforms import (
    chat_completion_chunk,
    completion_chunk,
    completion_usage_from_anthropic,
    completion_usage_from_responses,
    responses_to_chat_completion,
    stream_include_usage,
    usage_chunk,
)
from test_backend_and_app import _created_event, _failure_events, make_settings

UPSTREAM_USAGE = {
    "input_tokens": 18,
    "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 1},
    "total_tokens": 23,
    "attribution": {"items": {"msg_1": {"input_tokens": 11}}, "request_fields": {}},
}
EXPECTED = {
    "prompt_tokens": 18,
    "completion_tokens": 5,
    "total_tokens": 23,
    "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
    "completion_tokens_details": {"reasoning_tokens": 1},
}


# ----- translators -----

def test_responses_usage_becomes_completion_usage() -> None:
    assert completion_usage_from_responses(UPSTREAM_USAGE) == EXPECTED
    # The input is the token tally's object too: never mutated.
    assert UPSTREAM_USAGE["input_tokens"] == 18 and "attribution" in UPSTREAM_USAGE


@pytest.mark.parametrize("usage", [
    None, {}, [], "x", {"total_tokens": 6}, {"input_tokens": 3},
    {"input_tokens": "3", "output_tokens": 1}, {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": 1.5, "output_tokens": 1},
])
def test_unreadable_usage_is_omitted_never_zeroed(usage) -> None:
    assert completion_usage_from_responses(usage) is None
    assert completion_usage_from_anthropic(usage) is None


def test_details_keep_only_readable_members() -> None:
    usage = completion_usage_from_responses({
        "input_tokens": 3, "output_tokens": 1,
        "input_tokens_details": {"cached_tokens": "2", "cache_write_tokens": 0},
        "output_tokens_details": "n/a",
    })
    assert usage == {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
                     "prompt_tokens_details": {"cache_write_tokens": 0}}
    assert completion_usage_from_responses({"input_tokens": 3, "output_tokens": 1,
                                            "input_tokens_details": {}}) == {
        "prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}


def test_anthropic_usage_with_an_unreadable_cache_count_is_omitted() -> None:
    assert completion_usage_from_anthropic({"input_tokens": 2, "output_tokens": 4,
                                            "cache_read_input_tokens": "535"}) is None
    # Anthropic types the cache counts as nullable: null means none.
    assert completion_usage_from_anthropic({"input_tokens": 2, "output_tokens": 4,
                                            "cache_read_input_tokens": None})["prompt_tokens"] == 2


def test_anthropic_usage_counts_cache_reads_and_writes_as_prompt_tokens() -> None:
    # Observed live from the Claude CLI: 2 uncached + 3005 written + 535 read.
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 3005,
             "cache_read_input_tokens": 535, "output_tokens": 4, "server_tool_use": {}}
    assert completion_usage_from_anthropic(usage) == {
        "prompt_tokens": 3542, "completion_tokens": 4, "total_tokens": 3546,
        "prompt_tokens_details": {"cached_tokens": 535, "cache_write_tokens": 3005},
    }
    assert completion_usage_from_anthropic({"input_tokens": 11, "output_tokens": 4}) == {
        "prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}


def test_chunks_carry_null_usage_only_when_requested() -> None:
    assert "usage" not in chat_completion_chunk("id", 1, "m", {"content": "x"})
    assert chat_completion_chunk("id", 1, "m", {}, include_usage=True)["usage"] is None
    assert "usage" not in completion_chunk("id", 1, "m", "x")
    assert completion_chunk("id", 1, "m", "x", include_usage=True)["usage"] is None
    assert usage_chunk("text_completion", "id", 1, "m", EXPECTED) == {
        "id": "id", "object": "text_completion", "created": 1, "model": "m",
        "choices": [], "usage": EXPECTED}


@pytest.mark.parametrize("body, expected", [
    ({"stream_options": {"include_usage": True}}, True),
    ({"stream_options": {"include_usage": False}}, False),
    ({"stream_options": {"include_usage": "yes"}}, False),
    ({"stream_options": None}, False),
    ({"stream_options": ["include_usage"]}, False),
    ({}, False),
])
def test_stream_include_usage_reads_only_a_true_flag(body, expected) -> None:
    assert stream_include_usage(body) is expected


def test_truncated_response_finishes_with_length() -> None:
    payload = responses_to_chat_completion({"id": "r", "status": "incomplete", "output": [], "usage": None})
    assert payload["choices"][0]["finish_reason"] == "length"
    assert "usage" not in payload


# ----- OpenAI runtime routes -----

def _response(usage=UPSTREAM_USAGE, status="completed") -> dict:
    return {
        "id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-5.4-mini", "status": status,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": usage,
    }


def _stream(usage=UPSTREAM_USAGE, terminal="response.completed"):
    async def events(payload, request_id, session_id):
        del payload, request_id, session_id
        yield _created_event("resp_1")
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "ok"}))
        yield SSEEvent(terminal, json.dumps({"response": {"id": "resp_1", "model": "gpt-5.4-mini",
                                                          "status": "completed", "usage": usage}}))
    return events


def _data_lines(text: str) -> list:
    lines = [line[len("data: "):] for line in text.split("\n") if line.startswith("data: ")]
    return [line if line == "[DONE]" else json.loads(line) for line in lines]


def _client_post(tmp_path, path, body, *, collect=None, stream=None):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        if collect is not None:
            async def collect_response(payload, request_id, session_id):
                return collect
            client.app.state.backend.collect_response = collect_response
        if stream is not None:
            client.app.state.backend.stream_response_events = stream
        return client.post(path, json=body)


def _body(path: str, **extra) -> dict:
    base = {"model": "gpt-5.4-mini", **extra}
    if path.endswith("chat/completions"):
        return {**base, "messages": [{"role": "user", "content": "hello"}]}
    return {**base, "prompt": "hello"}


ROUTES = ["/v1/chat/completions", "/v1/completions", "/no-tools/v1/chat/completions", "/no-tools/v1/completions"]


@pytest.mark.parametrize("path", ROUTES)
def test_routes_return_completion_usage(tmp_path, path) -> None:
    response = _client_post(tmp_path, path, _body(path, stream=False), collect=_response())
    assert response.status_code == 200
    assert response.json()["usage"] == EXPECTED
    assert "attribution" not in response.text and "input_tokens" not in response.text


@pytest.mark.parametrize("path", ROUTES[:2])
def test_unreadable_upstream_usage_is_omitted(tmp_path, path) -> None:
    response = _client_post(tmp_path, path, _body(path, stream=False), collect=_response({"total_tokens": 6}))
    assert response.status_code == 200 and "usage" not in response.json()


@pytest.mark.parametrize("path, chunk_object", [
    ("/v1/chat/completions", "chat.completion.chunk"),
    ("/v1/completions", "text_completion"),
    ("/no-tools/v1/chat/completions", "chat.completion.chunk"),
    ("/no-tools/v1/completions", "text_completion"),
])
def test_stream_usage_chunk_follows_the_spec(tmp_path, path, chunk_object) -> None:
    response = _client_post(tmp_path, path, _body(path, stream=True, stream_options={"include_usage": True}),
                            stream=_stream())
    assert response.status_code == 200
    events = _data_lines(response.text)
    assert events[-1] == "[DONE]"
    chunks = events[:-1]
    *content, closing = chunks
    assert all(chunk["usage"] is None for chunk in content)
    assert content[-1]["choices"][0]["finish_reason"] == "stop"
    assert closing["object"] == chunk_object and closing["choices"] == [] and closing["usage"] == EXPECTED
    assert sum(1 for chunk in chunks if chunk.get("usage")) == 1


@pytest.mark.parametrize("path", ROUTES[:2])
def test_stream_without_include_usage_has_no_usage_key(tmp_path, path) -> None:
    response = _client_post(tmp_path, path, _body(path, stream=True), stream=_stream())
    chunks = [event for event in _data_lines(response.text) if event != "[DONE]"]
    assert chunks and all("usage" not in chunk for chunk in chunks)


def test_truncated_stream_without_usage_still_finishes(tmp_path) -> None:
    path = "/v1/chat/completions"
    response = _client_post(tmp_path, path, _body(path, stream=True, stream_options={"include_usage": True}),
                            stream=_stream(usage=None, terminal="response.incomplete"))
    events = _data_lines(response.text)
    assert events[-1] == "[DONE]"
    assert events[-2]["choices"][0]["finish_reason"] == "length"
    assert all("usage" in chunk and chunk["usage"] is None for chunk in events[:-1])


def test_responses_route_keeps_responses_usage(tmp_path) -> None:
    response = _client_post(tmp_path, "/v1/responses",
                            {"model": "gpt-5.4-mini", "input": "hello", "stream": False}, collect=_response())
    assert response.status_code == 200
    assert response.json()["usage"] == UPSTREAM_USAGE


# ----- Claude runtime -----

CLAUDE_USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 3005, "cache_read_input_tokens": 535, "output_tokens": 4}
CLAUDE_EXPECTED = completion_usage_from_anthropic(CLAUDE_USAGE)


def _claude(tmp_path, monkeypatch, *, result_usage=CLAUDE_USAGE, delta_usage=None):
    runtime = ClaudeCliRuntime(make_settings(tmp_path, claude_models=("claude:sonnet",)))
    result = {"type": "result", "result": "ok", "stop_reason": "end_turn"}
    if result_usage is not None:
        result["usage"] = result_usage

    async def run_json(request, request_id):
        return dict(result)

    async def run_stream(request, request_id):
        yield {"type": "stream_event", "event": {"type": "content_block_delta",
                                                 "delta": {"type": "text_delta", "text": "ok"}}}
        yield {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                                 "usage": delta_usage or {"input_tokens": 1, "output_tokens": 1}}}
        yield dict(result)

    monkeypatch.setattr(runtime, "_run_json", run_json)
    monkeypatch.setattr(runtime, "_run_stream", run_stream)
    return runtime


@pytest.mark.asyncio
async def test_claude_reports_every_prompt_token(tmp_path, monkeypatch) -> None:
    runtime = _claude(tmp_path, monkeypatch)
    chat = await runtime.create_chat_completion(
        {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}, "r")
    completion = await runtime.create_completion({"model": "claude:sonnet", "prompt": "hi"}, "r")
    assert chat["usage"] == completion["usage"] == CLAUDE_EXPECTED
    assert CLAUDE_EXPECTED["prompt_tokens"] == 3542


@pytest.mark.asyncio
async def test_claude_without_usage_omits_it(tmp_path, monkeypatch) -> None:
    runtime = _claude(tmp_path, monkeypatch, result_usage=None)
    chat = await runtime.create_chat_completion(
        {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}, "r")
    assert "usage" not in chat


async def _claude_stream(runtime, body) -> list:
    stream = (runtime.stream_chat_completion(body, "r") if "messages" in body
              else runtime.stream_completion(body, "r"))
    return _data_lines(b"".join([chunk async for chunk in stream]).decode())


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt, chunk_object", [
    ({"messages": [{"role": "user", "content": "hi"}]}, "chat.completion.chunk"),
    ({"prompt": "hi"}, "text_completion"),
])
async def test_claude_stream_usage_comes_from_the_result_and_follows_the_spec(
        tmp_path, monkeypatch, prompt, chunk_object) -> None:
    runtime = _claude(tmp_path, monkeypatch, delta_usage={"input_tokens": 999, "output_tokens": 999})
    events = await _claude_stream(runtime, {"model": "claude:sonnet", "stream": True,
                                            "stream_options": {"include_usage": True}, **prompt})
    assert events[-1] == "[DONE]"
    *content, closing = events[:-1]
    assert all(chunk["usage"] is None for chunk in content)
    assert content[-1]["choices"][0]["finish_reason"] == "stop"
    assert closing["object"] == chunk_object and closing["choices"] == []
    assert closing["usage"] == CLAUDE_EXPECTED


@pytest.mark.asyncio
async def test_claude_stream_with_unreadable_usage_still_finishes(tmp_path, monkeypatch) -> None:
    runtime = _claude(tmp_path, monkeypatch, result_usage={"input_tokens": "x", "output_tokens": 4})
    events = await _claude_stream(runtime, {"model": "claude:sonnet", "stream": True,
                                            "stream_options": {"include_usage": True},
                                            "messages": [{"role": "user", "content": "hi"}]})
    assert events[-1] == "[DONE]"
    assert all("usage" in chunk and chunk["usage"] is None for chunk in events[:-1])


def test_tool_call_stream_puts_usage_after_the_tool_calls_finish(tmp_path) -> None:
    async def events(payload, request_id, session_id):
        yield _created_event("resp_1")
        yield SSEEvent("response.output_item.done", json.dumps({"item": {
            "type": "custom_tool_call", "call_id": "call_1", "name": "ApplyPatch", "input": "x"}}))
        yield SSEEvent("response.completed", json.dumps({"response": {"id": "resp_1", "usage": UPSTREAM_USAGE}}))

    path = "/v1/chat/completions"
    response = _client_post(tmp_path, path, _body(path, stream=True, stream_options={"include_usage": True}),
                            stream=events)
    events_out = _data_lines(response.text)
    *content, closing, done = events_out
    assert done == "[DONE]" and closing["choices"] == [] and closing["usage"] == EXPECTED
    assert content[0]["choices"][0]["delta"]["tool_calls"]
    assert content[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all("usage" in chunk and chunk["usage"] is None for chunk in content)


def test_failed_stream_sends_no_usage_chunk(tmp_path) -> None:
    async def events(payload, request_id, session_id):
        yield _created_event("resp_1")
        yield SSEEvent("response.output_text.delta", json.dumps({"delta": "Hel"}))
        for event in _failure_events():
            yield event

    path = "/v1/chat/completions"
    response = _client_post(tmp_path, path, _body(path, stream=True, stream_options={"include_usage": True}),
                            stream=events)
    assert "[DONE]" not in response.text and '"error"' in response.text
    assert not any(isinstance(e, dict) and e.get("usage") for e in _data_lines(response.text))


def test_stream_options_without_streaming_is_dropped_and_disclosed(tmp_path) -> None:
    path = "/v1/chat/completions"
    response = _client_post(tmp_path, path, _body(path, stream=False, stream_options={"include_usage": True}),
                            collect=_response())
    assert response.status_code == 200
    assert "stream_options" in response.headers["x-airelays-ignored-parameters"].split(",")
    assert response.json()["usage"] == EXPECTED


def test_traffic_logs_keep_upstream_usage_and_log_the_translated_response(tmp_path) -> None:
    path = "/v1/chat/completions"
    _client_post(tmp_path, path, _body(path, stream=True, stream_options={"include_usage": True}), stream=_stream())
    records = [json.loads(line) for log in (tmp_path / "logs").rglob("*.log")
               for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    outbound_usage = [r for r in records if r.get("phase") == "outbound_usage"]
    assert outbound_usage and outbound_usage[-1]["usage"]["input_tokens"] == 18

    _client_post(tmp_path, path, _body(path, stream=False), collect=_response())
    records = [json.loads(line) for log in (tmp_path / "logs").rglob("*.log")
               for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    responses = [r for r in records if r.get("phase") == "outbound_response" and "usage" in json.dumps(r)]
    assert responses and '"prompt_tokens": 18' in json.dumps(responses[-1])


class _Traffic:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


@pytest.mark.asyncio
async def test_claude_stream_logs_usage_and_without_usage_still_finishes(tmp_path, monkeypatch) -> None:
    runtime = _claude(tmp_path, monkeypatch)
    runtime._traffic = traffic = _Traffic()
    await _claude_stream(runtime, {"model": "claude:sonnet", "stream": True,
                                   "messages": [{"role": "user", "content": "hi"}]})
    assert [r["usage"] for r in traffic.records if r.get("phase") == "outbound_usage"] == [CLAUDE_EXPECTED]

    runtime = _claude(tmp_path, monkeypatch, result_usage=None)
    events = await _claude_stream(runtime, {"model": "claude:sonnet", "stream": True,
                                            "stream_options": {"include_usage": True},
                                            "messages": [{"role": "user", "content": "hi"}]})
    assert events[-1] == "[DONE]"
    assert all("usage" in chunk and chunk["usage"] is None for chunk in events[:-1])


@pytest.mark.asyncio
async def test_claude_fallback_chunk_carries_null_usage(tmp_path, monkeypatch) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path, claude_models=("claude:sonnet",)))

    async def run_stream(request, request_id):
        # No text deltas: the answer arrives only in the final result.
        yield {"type": "result", "result": "ok", "stop_reason": "end_turn", "usage": CLAUDE_USAGE}

    monkeypatch.setattr(runtime, "_run_stream", run_stream)
    events = await _claude_stream(runtime, {"model": "claude:sonnet", "stream": True,
                                            "stream_options": {"include_usage": True},
                                            "messages": [{"role": "user", "content": "hi"}]})
    *content, closing, done = events
    assert content[0]["choices"][0]["delta"]["content"] == "ok" and content[0]["usage"] is None
    assert closing["usage"] == CLAUDE_EXPECTED and done == "[DONE]"


def test_truthy_stream_flag_keeps_stream_options(tmp_path) -> None:
    """Routes stream on a truthy `stream`; `stream_options` must survive it."""
    path = "/v1/chat/completions"
    response = _client_post(tmp_path, path, _body(path, stream=1, stream_options={"include_usage": True}),
                            stream=_stream())
    assert "stream_options" not in response.headers.get("x-airelays-ignored-parameters", "")
    assert _data_lines(response.text)[-2]["usage"] == EXPECTED
