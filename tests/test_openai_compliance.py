"""OpenAI API conformance of the chat and completions routes, beyond usage
(see test_usage_contract.py): error envelopes, status codes, finish
reasons, refusals, required fields, and parameters that cannot be honored.
Authority: openai-openapi `openapi.yml` and the openai-python SDK types."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from airelay.app import create_app
from airelay.backend import BackendError, SSEEvent
from airelay.providers import ClaudeCliRuntime, ProviderError
from airelay.transforms import responses_to_chat_completion
from test_backend_and_app import _created_event, make_settings
from test_usage_contract import _data_lines


def _assert_error(response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status
    body = response.json()
    error = body["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert isinstance(error["message"], str) and error["message"] == body["detail"]
    if code is not None:
        assert error["code"] == code
    return error


def _post(tmp_path, path, body, *, settings=None, setup=None):
    app = create_app(settings or make_settings(tmp_path, enable_claude=True))
    with TestClient(app) as client:
        if setup is not None:
            setup(client.app.state)
        return client.post(path, json=body)


CHAT = {"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "hi"}]}
CLAUDE_CHAT = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "hi"}]}


# ----- errors -----

def test_relay_errors_use_the_openai_envelope(tmp_path) -> None:
    error = _assert_error(_post(tmp_path, "/v1/chat/completions", {"model": "gpt-5.4-mini"}), 422)
    assert error["type"] == "invalid_request_error" and error["param"] is None
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        _assert_error(client.get("/v1/does-not-exist"), 404)


def test_unknown_claude_model_is_model_not_found(tmp_path) -> None:
    body = {"model": "claude:no-such-model", "messages": [{"role": "user", "content": "hi"}]}
    _assert_error(_post(tmp_path, "/v1/chat/completions", body), 404, "model_not_found")


def test_upstream_errors_are_complete_and_unwrapped(tmp_path) -> None:
    async def collect(payload, request_id, session_id):
        raise BackendError(400, '{"detail":"Unsupported parameter: stop"}')

    response = _post(tmp_path, "/v1/chat/completions", CHAT,
                     setup=lambda state: setattr(state.backend, "collect_response", collect))
    error = _assert_error(response, 400)
    assert error["message"] == "Unsupported parameter: stop"


def test_relay_auth_rejection_has_param(tmp_path) -> None:
    settings = make_settings(tmp_path, require_bearer_auth=True)
    settings.write_bearer_token("secret")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=CHAT, headers={"authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.json()["error"]["param"] is None


@pytest.mark.parametrize("error, status, retry_after", [
    (ProviderError(429, "All accounts are at their limit.", code="provider_quota_exhausted",
                   retry_after_seconds=29.2), 429, "30"),
    (ProviderError(401, "Claude CLI is not signed in.", code="provider_auth_error"), 503, None),
])
def test_claude_stream_failures_before_output_are_real_http_errors(tmp_path, error, status, retry_after) -> None:
    async def failing(body, request_id):
        raise error
        yield b""  # pragma: no cover - makes this an async generator

    response = _post(tmp_path, "/v1/chat/completions", {**CLAUDE_CHAT, "stream": True},
                     setup=lambda state: setattr(state.providers.claude, "stream_chat_completion", failing))
    _assert_error(response, status, error.code)
    assert response.headers.get("retry-after") == retry_after


# ----- parameters that cannot be honored -----

@pytest.mark.parametrize("path, base", [
    ("/v1/chat/completions", CHAT),
    ("/v1/completions", {"model": "gpt-5.4-mini", "prompt": "hi"}),
    ("/v1/chat/completions", CLAUDE_CHAT),
    ("/v1/completions", {"model": "claude:sonnet", "prompt": "hi"}),
])
@pytest.mark.parametrize("extra", [{"stop": ["3"]}, {"logprobs": True}, {"top_logprobs": 2},
                                   {"logit_bias": {"50256": -100}}])
def test_unhonored_parameters_are_rejected(tmp_path, path, base, extra) -> None:
    error = _assert_error(_post(tmp_path, path, {**base, **extra}), 422)
    assert f"`{next(iter(extra))}`" in error["message"]


def test_empty_unhonored_parameters_are_accepted_and_seed_is_disclosed(tmp_path) -> None:
    async def collect(payload, request_id, session_id):
        assert "seed" not in payload and "stop" not in payload
        return {"id": "resp_1", "created_at": 1.5, "model": "gpt-5.4-mini", "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}

    response = _post(tmp_path, "/v1/chat/completions",
                     {**CHAT, "stop": None, "logprobs": False, "logit_bias": {}, "seed": 7},
                     setup=lambda state: setattr(state.backend, "collect_response", collect))
    assert response.status_code == 200
    assert "seed" in response.headers["x-airelays-ignored-parameters"].split(",")
    body = response.json()
    assert body["created"] == 1 and isinstance(body["created"], int)
    choice = body["choices"][0]
    assert choice["logprobs"] is None and choice["message"]["refusal"] is None


# ----- finish reasons and refusals -----

def _incomplete(reason: str) -> dict:
    return {"id": "r", "created_at": 1, "status": "incomplete", "incomplete_details": {"reason": reason},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "part"}]}]}


@pytest.mark.parametrize("reason, expected", [("max_output_tokens", "length"), ("content_filter", "content_filter")])
def test_incomplete_responses_map_to_the_right_finish_reason(reason, expected) -> None:
    assert responses_to_chat_completion(_incomplete(reason))["choices"][0]["finish_reason"] == expected


def test_refusal_is_reported_not_dropped() -> None:
    payload = responses_to_chat_completion({"id": "r", "created_at": 1, "status": "completed", "output": [
        {"type": "message", "content": [{"type": "refusal", "refusal": "I can't help with that."}]}]})
    message = payload["choices"][0]["message"]
    assert message["refusal"] == "I can't help with that." and message["content"] is None


def test_streamed_refusal_and_content_filter(tmp_path) -> None:
    async def events(payload, request_id, session_id):
        yield _created_event("resp_1")
        yield SSEEvent("response.refusal.delta", json.dumps({"delta": "I can't"}))
        yield SSEEvent("response.incomplete", json.dumps({"response": {
            "id": "resp_1", "status": "incomplete", "incomplete_details": {"reason": "content_filter"}}}))

    response = _post(tmp_path, "/v1/chat/completions", {**CHAT, "stream": True},
                     setup=lambda state: setattr(state.backend, "stream_response_events", events))
    chunks = [event for event in _data_lines(response.text) if event != "[DONE]"]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "refusal": "I can't"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "content_filter"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason, expected", [
    ("end_turn", "stop"), ("max_tokens", "length"), ("model_context_window_exceeded", "length"),
    ("refusal", "content_filter"),
])
async def test_claude_stop_reasons_map_to_openai_finish_reasons(tmp_path, monkeypatch, stop_reason, expected) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path, claude_models=("claude:sonnet",)))

    async def run_json(request, request_id):
        return {"result": "x", "stop_reason": stop_reason}

    monkeypatch.setattr(runtime, "_run_json", run_json)
    payload = await runtime.create_chat_completion(CLAUDE_CHAT, "r")
    assert payload["choices"][0]["finish_reason"] == expected
    assert payload["choices"][0]["logprobs"] is None and payload["choices"][0]["message"]["refusal"] is None


def test_claude_permission_failures_are_the_relays_and_not_retried(tmp_path) -> None:
    async def failing(body, request_id):
        raise ProviderError(403, "Claude CLI lacks permission.", code="provider_auth_error")
        yield b""  # pragma: no cover

    response = _post(tmp_path, "/v1/chat/completions", {**CLAUDE_CHAT, "stream": True},
                     setup=lambda state: setattr(state.providers.claude, "stream_chat_completion", failing))
    _assert_error(response, 503, "provider_auth_error")
    assert response.headers["x-should-retry"] == "false"


@pytest.mark.parametrize("path, body", [
    ("/v1/chat/completions", {**CHAT, "modalities": ["text"], "audio": None, "prediction": None}),
    ("/v1/completions", {"model": "gpt-5.4-mini", "prompt": "hi", "echo": False, "best_of": 1,
                         "logprobs": None, "suffix": None}),
])
def test_default_values_are_not_rejected(tmp_path, path, body) -> None:
    async def collect(payload, request_id, session_id):
        return {"id": "r", "created_at": 1, "model": "gpt-5.4-mini", "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}

    response = _post(tmp_path, path, body, setup=lambda state: setattr(state.backend, "collect_response", collect))
    assert response.status_code == 200


def test_completions_refusal_reports_content_filter(tmp_path) -> None:
    from airelay.transforms import responses_to_completion

    refused = {"id": "r", "created_at": 1, "status": "completed", "output": [
        {"type": "message", "content": [{"type": "refusal", "refusal": "No."}]}]}
    assert responses_to_completion(refused)["choices"][0]["finish_reason"] == "content_filter"

    async def events(payload, request_id, session_id):
        yield _created_event("resp_1")
        yield SSEEvent("response.refusal.delta", json.dumps({"delta": "No."}))
        yield SSEEvent("response.completed", json.dumps({"response": {"id": "resp_1", "status": "completed"}}))

    response = _post(tmp_path, "/v1/completions", {"model": "gpt-5.4-mini", "prompt": "hi", "stream": True},
                     setup=lambda state: setattr(state.backend, "stream_response_events", events))
    chunks = [event for event in _data_lines(response.text) if event != "[DONE]"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "content_filter"


def test_typed_route_validation_errors_use_the_envelope(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post("/v1/files", data={"purpose": "assistants"})
    _assert_error(response, 422)


def test_completions_logprobs_zero_is_a_request(tmp_path) -> None:
    for model in ("gpt-5.4-mini", "claude:sonnet"):
        error = _assert_error(_post(tmp_path, "/v1/completions", {"model": model, "prompt": "hi", "logprobs": 0}), 422)
        assert "logprobs" in error["message"]


# ----- round 3: spec items found by the conformance audit -----

def _completed(response_id: str, output: list[dict]) -> dict:
    return {"id": response_id, "created_at": 1, "model": "gpt-5.4-mini", "status": "completed", "output": output}


def _text(text: str) -> dict:
    return {"type": "message", "id": "msg_up", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _call(call_id: str, name: str = "get_time") -> dict:
    return {"type": "function_call", "id": "fc_up", "status": "completed", "call_id": call_id,
            "name": name, "arguments": '{"tz":"UTC"}'}


TOOLS = [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object", "properties": {}}}}]


def _app_with(tmp_path, *, collect=None, events=None):
    app = create_app(make_settings(tmp_path))
    client = TestClient(app)
    client.__enter__()
    if collect is not None:
        app.state.backend.collect_response = collect
    if events is not None:
        app.state.backend.stream_response_events = events
    return client


def test_tool_call_loop_accepts_null_assistant_content(tmp_path) -> None:
    sent = []

    async def collect(payload, request_id, session_id):
        sent.append(payload)
        return _completed("r", [_text("It is noon.")])

    client = _app_with(tmp_path, collect=collect)
    messages = [
        {"role": "user", "content": "time?"},
        {"role": "assistant", "content": None, "refusal": None,
         "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "12:00"},
    ]
    response = client.post("/v1/chat/completions", json={**CHAT, "messages": messages, "tools": TOOLS})
    assert response.status_code == 200
    assert [item["type"] for item in sent[0]["input"]] == ["message", "function_call", "function_call_output"]
    error = _assert_error(client.post("/v1/chat/completions",
                                      json={**CHAT, "messages": [{"role": "user", "content": None}]}), 422)
    assert "require `content`" in error["message"]
    client.__exit__(None, None, None)


def test_files_list_paginates_like_openai(tmp_path) -> None:
    client = _app_with(tmp_path)
    ids = [client.post("/v1/files", files={"file": (f"f{n}.txt", b"x", "text/plain")},
                       data={"purpose": "user_data" if n == 2 else "assistants"}).json()["id"] for n in range(3)]
    page = client.get("/v1/files", params={"limit": 2}).json()
    assert [f["id"] for f in page["data"]] == ids[::-1][:2]
    assert page["has_more"] is True and page["first_id"] == ids[2] and page["last_id"] == ids[1]
    rest = client.get("/v1/files", params={"limit": 2, "after": page["last_id"]}).json()
    assert [f["id"] for f in rest["data"]] == [ids[0]] and rest["has_more"] is False
    assert [f["id"] for f in client.get("/v1/files", params={"order": "asc"}).json()["data"]] == ids
    assert [f["id"] for f in client.get("/v1/files", params={"purpose": "user_data"}).json()["data"]] == [ids[2]]
    empty = client.get("/v1/files", params={"purpose": "batch"}).json()
    assert empty == {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False}
    _assert_error(client.get("/v1/files", params={"after": "file_missing"}), 404)
    _assert_error(client.get("/v1/files", params={"order": "sideways"}), 422)
    client.__exit__(None, None, None)


def test_upload_purpose_is_the_form_field_and_validated(tmp_path) -> None:
    client = _app_with(tmp_path)
    upload = {"file": ("a.txt", b"x", "text/plain")}
    assert client.post("/v1/files", files=upload, data={"purpose": "user_data"}).json()["purpose"] == "user_data"
    assert client.post("/v1/files", files=upload).json()["purpose"] == "assistants"
    error = _assert_error(client.post("/v1/files", files=upload, data={"purpose": "bogus"}), 422)
    assert "`purpose`" in error["message"]
    client.__exit__(None, None, None)


def test_conversation_keeps_every_turn(tmp_path) -> None:
    sent = []
    replies = iter([_completed("resp_1", [{"type": "reasoning", "id": "rs_1", "summary": []}, _text("Paris.")]),
                    _completed("resp_2", [_call("call_9")]),
                    _completed("resp_3", [_text("Done.")])])

    async def collect(payload, request_id, session_id):
        sent.append(json.loads(json.dumps(payload)))
        return next(replies)

    client = _app_with(tmp_path, collect=collect)
    conversation = client.post("/v1/conversations", json={"items": [
        {"type": "message", "role": "user", "content": "Be brief."}]}).json()
    assert set(conversation) >= {"id", "object", "created_at", "metadata"}
    body = {**CHAT, "conversation": conversation["id"]}
    client.post("/v1/chat/completions", json={**body, "messages": [{"role": "user", "content": "Capital of France?"}]})
    client.post("/v1/responses", json={"model": "gpt-5.4-mini", "input": "Time?", "tools": [
        {"type": "function", "name": "get_time", "parameters": {"type": "object", "properties": {}}}],
        "conversation": conversation["id"]})
    # A tool result may answer a call made in an earlier turn.
    third = client.post("/v1/chat/completions", json={**body, "tools": TOOLS, "messages": [
        {"role": "tool", "tool_call_id": "call_9", "content": "12:00"}]})
    assert third.status_code == 200
    def text(item):
        if item["type"] != "message":
            return item["type"]
        content = item["content"]
        return content if isinstance(content, str) else content[0]["text"]

    assert [text(item) for item in sent[2]["input"]] == [
        "Be brief.", "Capital of France?", "Paris.", "Time?", "function_call", "function_call_output"]
    # Upstream ids and reasoning items are never replayed (store=false).
    assert all("id" not in item for item in sent[2]["input"])
    deleted = client.delete(f"/v1/conversations/{conversation['id']}").json()
    assert deleted == {"id": conversation["id"], "object": "conversation.deleted", "deleted": True}
    client.__exit__(None, None, None)


def test_streamed_turns_are_recorded_and_failed_turns_are_not(tmp_path) -> None:
    sent = []
    outcomes = iter(["ok", "fail", "check"])

    async def events(payload, request_id, session_id):
        sent.append(json.loads(json.dumps(payload)))
        outcome = next(outcomes)
        yield _created_event("resp_s")
        if outcome == "fail":
            yield SSEEvent("response.failed", json.dumps({"response": {"id": "resp_s", "status": "failed",
                                                                        "error": {"message": "boom"}}}))
            return
        yield SSEEvent("response.output_item.done", json.dumps({"output_index": 0, "item": _text("Hi!")}))
        yield SSEEvent("response.completed", json.dumps({"response": {"id": "resp_s", "status": "completed",
                                                                       "output": []}}))

    client = _app_with(tmp_path, events=events)
    conversation_id = client.post("/v1/conversations", json={}).json()["id"]
    for text in ("one", "two", "three"):
        client.post("/v1/completions", json={"model": "gpt-5.4-mini", "prompt": text, "stream": True,
                                             "conversation": conversation_id})
    texts = [item["content"][0]["text"] for item in sent[2]["input"]]
    assert texts == ["one", "Hi!", "three"]
    client.__exit__(None, None, None)


def test_unknown_parameters_are_disclosed_and_known_ones_honored(tmp_path) -> None:
    sent = []

    async def collect(payload, request_id, session_id):
        sent.append(payload)
        return _completed("r", [_text("ok")])

    client = _app_with(tmp_path, collect=collect)
    response = client.post("/v1/chat/completions", json={
        **CHAT, "verbosity": "low", "prompt_cache_key": "k1", "prompt_cache_retention": "24h",
        "brand_new_option": {"x": 1}, "unset_option": None})
    assert response.status_code == 200
    ignored = response.headers["x-airelays-ignored-parameters"].split(",")
    assert {"prompt_cache_retention", "brand_new_option"} <= set(ignored) and "unset_option" not in ignored
    assert sent[0]["text"] == {"verbosity": "low"} and sent[0]["prompt_cache_key"] == "k1"
    response = client.post("/v1/completions", json={"model": "gpt-5.4-mini", "prompt": "hi", "echo_v2": True})
    assert "echo_v2" in response.headers["x-airelays-ignored-parameters"]
    # `web_search_options: {}` means "search with defaults": still a request.
    for extra in ({"web_search_options": {}}, {"web_search_options": {"search_context_size": "low"}},
                  {"moderation": {"model": "omni-moderation-latest"}}):
        _assert_error(client.post("/v1/chat/completions", json={**CHAT, **extra}), 422)
    assert client.post("/v1/chat/completions", json={**CHAT, "web_search_options": None}).status_code == 200
    client.__exit__(None, None, None)


def test_claude_lane_discloses_unknown_and_rejects_unhonored_features(tmp_path) -> None:
    async def create(body, request_id):
        assert "verbosity" not in body
        return {"id": "c", "object": "chat.completion", "created": 1, "model": "claude:sonnet", "choices": []}

    def setup(state):
        state.providers.claude.create_chat_completion = create

    response = _post(tmp_path, "/v1/chat/completions", {**CLAUDE_CHAT, "verbosity": "low"}, setup=setup)
    assert response.headers["x-airelays-ignored-parameters"] == "verbosity"
    _assert_error(_post(tmp_path, "/v1/chat/completions", {**CLAUDE_CHAT, "web_search_options": {"x": 1}}), 422)


def test_claude_lane_accepts_default_values(tmp_path) -> None:
    runtime = ClaudeCliRuntime(make_settings(tmp_path, claude_models=("claude:sonnet",)))
    runtime._prepare_completion_request({"model": "claude:sonnet", "prompt": "hi", "best_of": 1, "suffix": "",
                                         "echo": False})
    runtime._prepare_chat_request({**CLAUDE_CHAT, "modalities": ["text"], "store": False, "audio": None})
    with pytest.raises(ProviderError, match="best_of"):
        runtime._prepare_completion_request({"model": "claude:sonnet", "prompt": "hi", "best_of": 2})


def test_legacy_functions_round_trip(tmp_path) -> None:
    sent = []

    async def collect(payload, request_id, session_id):
        sent.append(payload)
        return _completed("r", [_call("call_up", "lookup")])

    functions = [{"name": "lookup", "parameters": {"type": "object", "properties": {}}}]
    client = _app_with(tmp_path, collect=collect)
    reply = client.post("/v1/chat/completions", json={**CHAT, "functions": functions}).json()
    choice = reply["choices"][0]
    assert choice["finish_reason"] == "function_call" and "tool_calls" not in choice["message"]
    assert choice["message"]["function_call"] == {"name": "lookup", "arguments": '{"tz":"UTC"}'}
    assert sent[0]["parallel_tool_calls"] is False
    messages = [{"role": "user", "content": "hi"}, choice["message"],
                {"role": "function", "name": "lookup", "content": "42"}]
    assert client.post("/v1/chat/completions", json={**CHAT, "functions": functions,
                                                     "messages": messages}).status_code == 200
    call, output = sent[1]["input"][1:]
    assert call["type"] == "function_call" and output == {"type": "function_call_output",
                                                          "call_id": call["call_id"], "output": "42"}
    orphan = [{"role": "user", "content": "hi"}, {"role": "function", "name": "lookup", "content": "42"}]
    _assert_error(client.post("/v1/chat/completions", json={**CHAT, "functions": functions,
                                                            "messages": orphan}), 422)
    client.__exit__(None, None, None)


def test_legacy_functions_stream(tmp_path) -> None:
    async def events(payload, request_id, session_id):
        yield _created_event("resp_1")
        for index in (0, 1):
            yield SSEEvent("response.output_item.done",
                           json.dumps({"output_index": index, "item": _call(f"call_{index}", "lookup")}))
        yield SSEEvent("response.completed", json.dumps({"response": {"id": "resp_1", "status": "completed"}}))

    client = _app_with(tmp_path, events=events)
    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True, "functions": [{"name": "lookup"}]})
    chunks = [event for event in _data_lines(response.text) if event != "[DONE]"]
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert deltas[0] == {"role": "assistant", "function_call": {"name": "lookup", "arguments": '{"tz":"UTC"}'}}
    assert len([d for d in deltas if "function_call" in d]) == 1
    assert chunks[-1]["choices"][0]["finish_reason"] == "function_call"
    client.__exit__(None, None, None)


def test_file_content_parts_in_the_spec_shape(tmp_path) -> None:
    from airelay.store import AppStore
    from airelay.transforms import chat_completions_to_responses, expand_local_files

    store = AppStore(tmp_path)
    file_id = store.create_file(filename="notes.pdf", purpose="user_data", content_type="application/pdf",
                                data=b"%PDF-1.4", sha256="x")["id"]
    content = [{"type": "file", "file": {"file_id": file_id}},
               {"type": "file", "file": {"file_data": "JVBERi0xLjQ=", "filename": "inline.pdf"}}]
    payload, _, _ = chat_completions_to_responses({**CHAT, "messages": [{"role": "user", "content": content}]},
                                                  store, allow_tools=True)
    # Translation keeps the local reference; it is inlined just before the upstream call.
    assert payload["input"][0]["content"][0] == {"type": "input_file", "file_id": file_id}
    expand_local_files(payload, store)
    parts = payload["input"][0]["content"]
    assert [part["type"] for part in parts] == ["input_file", "input_file"]
    assert parts[0]["file_data"].startswith("data:application/pdf;base64,")
    assert parts[1] == {"type": "input_file", "filename": "inline.pdf",
                        "file_data": "data:application/pdf;base64,JVBERi0xLjQ="}


def test_allowed_tools_choice_is_translated(tmp_path) -> None:
    from airelay.store import AppStore
    from airelay.transforms import TranslationError, chat_completions_to_responses

    choice = {"type": "allowed_tools", "allowed_tools": {"mode": "required", "tools": [
        {"type": "function", "function": {"name": "get_time"}}]}}
    payload, _, _ = chat_completions_to_responses({**CHAT, "tools": TOOLS, "tool_choice": choice},
                                                  AppStore(tmp_path), allow_tools=True)
    assert payload["tool_choice"] == {"type": "allowed_tools", "mode": "required",
                                      "tools": [{"type": "function", "name": "get_time"}]}
    with pytest.raises(TranslationError, match="allowed_tools"):
        chat_completions_to_responses({**CHAT, "tools": TOOLS, "tool_choice": {"type": "allowed_tools"}},
                                      AppStore(tmp_path), allow_tools=True)


def test_non_strict_json_schema_is_forwarded_unchanged(tmp_path) -> None:
    from airelay.store import AppStore
    from airelay.transforms import chat_completions_to_responses

    schema = {"type": "object", "properties": {"nick": {"type": "string"}}}
    for strict in ({}, {"strict": False}):
        response_format = {"type": "json_schema", "json_schema": {"name": "s", "schema": schema, **strict}}
        payload, _, _ = chat_completions_to_responses({**CHAT, "response_format": response_format},
                                                      AppStore(tmp_path), allow_tools=True)
        # OpenAI's default, sent explicitly: the upstream assumes strict otherwise.
        assert payload["text"]["format"]["schema"] == schema and payload["text"]["format"]["strict"] is False


def test_model_retrieve_returns_listed_models_only(tmp_path) -> None:
    catalog = {"models": [{"slug": "gpt-5.4-mini"}]}

    async def models_payload(request_id, force=False):
        if catalog is None:
            raise BackendError(503, "catalog unavailable")
        return catalog

    app = create_app(make_settings(tmp_path, enable_claude=True))
    with TestClient(app) as client:
        app.state.providers._openai_models_payload = models_payload
        listed = client.get("/v1/models/GPT-5.4-mini").json()
        assert listed["id"] == "gpt-5.4-mini" and listed["owned_by"] == "airelays-openai-subscription"
        _assert_error(client.get("/v1/models/gpt-unlisted"), 404, "model_not_found")
        _assert_error(client.get("/v1/models/claude:no-such-model"), 404, "model_not_found")
        # Claude ids are case-sensitive for requests, so for retrieval too.
        claude_ids = [m["id"] for m in client.get("/v1/models").json()["data"] if m["id"].startswith("claude:")]
        for claude_id in claude_ids[:1]:
            assert client.get(f"/v1/models/{claude_id}").status_code == 200
            assert client.get(f"/v1/models/{claude_id.upper()}").status_code == 404
        _assert_error(client.get("/v1/models/"), 404, "model_not_found")
        catalog = None  # a catalog failure is reported, not answered with a guess
        assert client.get("/v1/models/gpt-5.4-mini").status_code == 503


def test_retry_signals_and_request_id(tmp_path) -> None:
    async def collect(payload, request_id, session_id):
        raise BackendError(429, json.dumps({"error": {"type": "usage_limit_reached", "message": "limit",
                                                      "resets_in_seconds": 3599.2}}))

    client = _app_with(tmp_path, collect=collect)
    response = client.post("/v1/chat/completions", json=CHAT)
    assert response.status_code == 429 and response.headers["retry-after"] == "3600"
    assert response.headers["x-request-id"].startswith("req_")
    unsupported = client.post("/v1/embeddings", json={"model": "x", "input": "y"})
    _assert_error(unsupported, 501)
    assert unsupported.headers["x-should-retry"] == "false"
    client.__exit__(None, None, None)
    disabled = TestClient(create_app(make_settings(tmp_path, enable_openai_provider=False)))
    with disabled:
        response = disabled.get("/v1/files")
    assert response.status_code == 501 and response.headers["x-should-retry"] == "false"


# ----- round 4: re-audit of the fixes above -----

def test_disclosed_parameter_names_are_header_safe(tmp_path) -> None:
    async def collect(payload, request_id, session_id):
        return _completed("r", [_text("ok")])

    client = _app_with(tmp_path, collect=collect)
    response = client.post("/v1/chat/completions", json={**CHAT, "提示": 1, "a\r\nb": 2, "max_tokens": None})
    assert response.status_code == 200
    assert response.headers["x-airelays-ignored-parameters"] == "a%0D%0Ab,%E6%8F%90%E7%A4%BA"
    response = client.post("/v1/chat/completions", json={**CHAT, **{f"opt_{n:04d}": n for n in range(400)}})
    header = response.headers["x-airelays-ignored-parameters"]
    assert len(header) <= 2048 + 4 and header.endswith(",...")
    client.__exit__(None, None, None)


def test_mixed_chat_shapes_are_rejected(tmp_path) -> None:
    body = {**CHAT, "input": "hi"}
    error = _assert_error(_post(tmp_path, "/v1/chat/completions", body), 422)
    assert "not both" in error["message"]


def test_list_and_delete_loop_follows_the_cursor(tmp_path) -> None:
    client = _app_with(tmp_path)
    for n in range(5):
        client.post("/v1/files", files={"file": (f"f{n}.txt", b"x", "text/plain")})
    deleted, after = 0, None
    while True:
        page = client.get("/v1/files", params={"limit": 2, **({"after": after} if after else {})}).json()
        for file in page["data"]:
            assert client.delete(f"/v1/files/{file['id']}").status_code == 200
            deleted += 1
        if not page["has_more"]:
            break
        after = page["last_id"]
    assert deleted == 5 and client.get("/v1/files").json()["data"] == []
    client.__exit__(None, None, None)


def test_replayable_items_keep_client_calls_and_encrypted_reasoning() -> None:
    from airelay.conversations import replayable_items

    output = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA"},
        {"type": "reasoning", "id": "rs_2", "summary": []},
        {"type": "web_search_call", "id": "ws_1", "status": "completed"},
        {"type": "local_shell_call", "id": "lsh_1", "call_id": "call_s", "status": "completed",
         "action": {"type": "exec", "command": ["ls"]}},
    ]
    assert replayable_items(output) == [
        {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"},
        {"type": "local_shell_call", "call_id": "call_s", "status": "completed",
         "action": {"type": "exec", "command": ["ls"]}},
    ]


def test_conversations_store_file_references_not_bytes(tmp_path) -> None:
    sent = []

    async def collect(payload, request_id, session_id):
        sent.append(payload)
        return _completed("r", [_text("ok")])

    client = _app_with(tmp_path, collect=collect)
    file_id = client.post("/v1/files", files={"file": ("doc.pdf", b"%PDF-1.4 secret", "application/pdf")}).json()["id"]
    conversation_id = client.post("/v1/conversations", json={}).json()["id"]
    content = [{"type": "text", "text": "read"}, {"type": "file", "file": {"file_id": file_id}}]
    for _ in range(2):
        assert client.post("/v1/chat/completions", json={
            **CHAT, "conversation": conversation_id, "messages": [{"role": "user", "content": content}]}).status_code == 200
    stored = client.app.state.store.conversation_items(conversation_id)
    assert "file_data" not in json.dumps(stored) and file_id in json.dumps(stored)
    assert json.dumps(sent[1]).count("data:application/pdf;base64,") == 2
    client.delete(f"/v1/files/{file_id}")
    error = _assert_error(client.post("/v1/chat/completions", json={
        **CHAT, "conversation": conversation_id, "messages": [{"role": "user", "content": "again"}]}), 422)
    assert f"deleted file `{file_id}`" in error["message"] and "new conversation" in error["message"]
    client.__exit__(None, None, None)


def test_legacy_functions_with_conversation_and_forced_function_call(tmp_path) -> None:
    from airelay.store import AppStore
    from airelay.transforms import TranslationError, chat_completions_to_responses

    store = AppStore(tmp_path)
    with pytest.raises(TranslationError, match="conversation"):
        chat_completions_to_responses({**CHAT, "functions": [{"name": "f"}], "conversation": "conv_1"},
                                      store, allow_tools=True)
    payload, _, _ = chat_completions_to_responses({**CHAT, "tools": TOOLS, "function_call": {"name": "get_time"}},
                                                  store, allow_tools=True)
    assert payload["tool_choice"] == {"type": "function", "name": "get_time"}
    with pytest.raises(TranslationError, match="entries"):
        chat_completions_to_responses({**CHAT, "tools": TOOLS, "tool_choice": {
            "type": "allowed_tools", "allowed_tools": {"mode": "auto", "tools": ["get_time"]}}},
            store, allow_tools=True)


def test_unexpected_errors_are_500_with_request_id(tmp_path) -> None:
    async def collect(payload, request_id, session_id):
        raise KeyError("boom")

    app = create_app(make_settings(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.backend.collect_response = collect
        conversation_id = client.post("/v1/conversations", json={}).json()["id"]
        response = client.post("/v1/chat/completions", json={**CHAT, "conversation": conversation_id})
        assert response.status_code == 500 and response.headers["x-request-id"].startswith("req_")
        _assert_error(client.post("/v1/chat/completions", json={**CHAT, "conversation": "conv_nope"}), 404)


def test_unsupported_file_and_conversation_features(tmp_path) -> None:
    client = _app_with(tmp_path)
    upload = {"file": ("a.txt", b"x", "text/plain")}
    error = _assert_error(client.post("/v1/files", files=upload, data={
        "purpose": "user_data", "expires_after[anchor]": "created_at", "expires_after[seconds]": "3600"}), 422)
    assert "expires_after" in error["message"]
    conversation_id = client.post("/v1/conversations", json={}).json()["id"]
    response = client.get(f"/v1/conversations/{conversation_id}/items")
    _assert_error(response, 501)
    assert response.headers["x-should-retry"] == "false"
    client.__exit__(None, None, None)


def test_conversation_items_are_checked_like_turns(tmp_path) -> None:
    client = _app_with(tmp_path)
    file_id = client.post("/v1/files", files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")}).json()["id"]
    item = {"type": "message", "role": "user", "content": [{"type": "input_image", "file_id": file_id}]}
    error = _assert_error(client.post("/v1/conversations", json={"items": [item]}), 422)
    assert "not an image" in error["message"]
    client.__exit__(None, None, None)


def test_chat_function_tools_default_to_non_strict(tmp_path) -> None:
    from airelay.store import AppStore
    from airelay.transforms import TranslationError, chat_completions_to_responses

    store = AppStore(tmp_path)
    tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}
    for strict, expected in ((None, False), (False, False), (True, True)):
        function = {**tool["function"], **({} if strict is None else {"strict": strict})}
        payload, _, _ = chat_completions_to_responses({**CHAT, "tools": [{**tool, "function": function}]},
                                                      store, allow_tools=True)
        assert payload["tools"][0]["strict"] is expected
    with pytest.raises(TranslationError, match="boolean"):
        chat_completions_to_responses({**CHAT, "tools": [{**tool, "function": {**tool["function"], "strict": "yes"}}]},
                                      store, allow_tools=True)
    response_format = {"type": "json_schema", "json_schema": {"name": "s", "schema": {"type": "object"}, "strict": 1}}
    with pytest.raises(TranslationError, match="boolean"):
        chat_completions_to_responses({**CHAT, "response_format": response_format}, store, allow_tools=True)


def test_json_mode_sends_system_prompts_as_developer_input(tmp_path) -> None:
    """The upstream checks only input messages for the word "json" (verified
    live), while OpenAI's chat API also counts the system prompt."""
    from airelay.store import AppStore
    from airelay.transforms import chat_completions_to_responses

    body = {**CHAT, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "Answer in JSON."}, {"role": "user", "content": "one under a"}]}
    payload, _, _ = chat_completions_to_responses(body, AppStore(tmp_path), allow_tools=True)
    assert payload["text"]["format"] == {"type": "json_object"}
    assert payload["input"][0] == {"type": "message", "role": "developer",
                                   "content": [{"type": "input_text", "text": "Answer in JSON."}]}
    plain, _, _ = chat_completions_to_responses({**body, "response_format": None}, AppStore(tmp_path), allow_tools=True)
    assert plain["instructions"] == "Answer in JSON." and plain["input"][0]["role"] == "user"


def test_long_waits_tell_sdks_not_to_retry(tmp_path) -> None:
    async def failing(body, request_id):
        raise ProviderError(503, "No Claude account can serve it right now.", code="provider_unavailable",
                            retry_after_seconds=280)
        yield b""  # pragma: no cover

    response = _post(tmp_path, "/v1/chat/completions", {**CLAUDE_CHAT, "stream": True},
                     setup=lambda state: setattr(state.providers.claude, "stream_chat_completion", failing))
    assert response.headers["retry-after"] == "280" and response.headers["x-should-retry"] == "false"


# ----- 0.17.0: Claude models on /v1/responses, with tools, files and conversations -----

def _claude_app(tmp_path, results):
    """An app whose Claude runtime answers from `results` and records what it was sent."""
    app = create_app(make_settings(tmp_path, enable_claude=True, claude_models=("claude:sonnet",)))
    client = TestClient(app)
    client.__enter__()
    sent = []
    runtime = app.state.providers.claude

    async def create(body, request_id):
        sent.append(body)
        return next(results)

    async def stream(body, request_id):
        sent.append(body)
        for chunk in next(results):
            yield f"data: {json.dumps(chunk)}\n\n".encode() if chunk != "[DONE]" else b"data: [DONE]\n\n"

    runtime.create_chat_completion = create
    runtime.stream_chat_completion = stream
    return client, sent


def _chat(content=None, tool_calls=None, finish="stop"):
    message = {"role": "assistant", "content": content, "refusal": None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "c", "object": "chat.completion", "created": 1, "model": "claude:sonnet",
            "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                      "prompt_tokens_details": {"cached_tokens": 4}}}


CALL = {"id": "toolu_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}


def test_responses_on_claude_translate_both_ways(tmp_path) -> None:
    client, sent = _claude_app(tmp_path, iter([_chat(tool_calls=[CALL], finish="tool_calls"), _chat("21C, sunny")]))
    body = {"model": "claude:sonnet", "instructions": "Be brief.", "input": "Weather in Paris?",
            "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
            "text": {"format": {"type": "json_object"}}, "reasoning": {"effort": "low"}}
    first = client.post("/v1/responses", json=body).json()
    assert sent[0]["messages"] == [{"role": "system", "content": "Be brief."},
                                   {"role": "user", "content": "Weather in Paris?"}]
    assert sent[0]["tools"][0]["function"]["name"] == "get_weather"
    assert sent[0]["response_format"] == {"type": "json_object"} and sent[0]["reasoning_effort"] == "low"
    assert first["object"] == "response" and first["status"] == "completed"
    assert first["output"] == [{"id": first["output"][0]["id"], "type": "function_call", "status": "completed",
                                "call_id": "toolu_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}]
    assert first["usage"] == {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 3,
                              "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 13}
    second = client.post("/v1/responses", json={**body, "input": [
        {"type": "message", "role": "user", "content": "Weather in Paris?"},
        {"type": "function_call", "call_id": "toolu_1", "name": "get_weather", "arguments": '{"city": "Paris"}'},
        {"type": "function_call_output", "call_id": "toolu_1", "output": "21C"}]}).json()
    assert [m["role"] for m in sent[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["output"][0]["content"][0] == {"type": "output_text", "text": "21C, sunny", "annotations": []}
    client.__exit__(None, None, None)


def test_responses_stream_on_claude(tmp_path) -> None:
    chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
              {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}, "[DONE]"]
    client, sent = _claude_app(tmp_path, iter([chunks]))
    response = client.post("/v1/responses", json={"model": "claude:sonnet", "input": "hi", "stream": True})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    kinds = [event["type"] for event in events]
    assert kinds[:2] == ["response.created", "response.in_progress"] and kinds[-1] == "response.completed"
    assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == "Hello"
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert events[-1]["response"]["usage"]["input_tokens"] == 5
    assert sent[0]["stream_options"] == {"include_usage": True}
    client.__exit__(None, None, None)


def test_claude_conversations_and_local_files(tmp_path) -> None:
    client, sent = _claude_app(tmp_path, iter([_chat("Noted, Zorblat."), _chat("Your name is Zorblat.")]))
    file_id = client.post("/v1/files", files={"file": ("notes.txt", b"secret: mango", "text/plain")},
                          data={"purpose": "user_data"}).json()["id"]
    conversation_id = client.post("/v1/conversations", json={}).json()["id"]
    first = client.post("/v1/chat/completions", json={"model": "claude:sonnet", "conversation": conversation_id,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "I am Zorblat."},
                                                   {"type": "file", "file": {"file_id": file_id}}]}]})
    assert first.status_code == 200
    part = sent[0]["messages"][0]["content"][1]
    assert part["type"] == "file" and part["file"]["file_data"].startswith("data:text/plain;base64,")
    second = client.post("/v1/responses", json={"model": "claude:sonnet", "input": "Who am I?",
                                                "conversation": conversation_id}).json()
    roles = [m["role"] for m in sent[1]["messages"]]
    assert roles == ["user", "assistant", "user"] and second["output"][0]["content"][0]["text"] == "Your name is Zorblat."
    stored = json.dumps(client.app.state.store.conversation_items(conversation_id))
    assert file_id in stored and "base64" not in stored  # references, not file contents
    client.__exit__(None, None, None)


def test_no_tools_route_refuses_tools_on_claude(tmp_path) -> None:
    client, sent = _claude_app(tmp_path, iter([]))
    response = client.post("/no-tools/v1/chat/completions", json={"model": "claude:sonnet", "messages": [
        {"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "f"}}]})
    _assert_error(response, 422)
    assert sent == []
    client.__exit__(None, None, None)


def test_claude_requests_cap_the_size_of_referenced_files(tmp_path, monkeypatch) -> None:
    import airelay.transforms as transforms

    monkeypatch.setattr(transforms, "INLINE_FILES_MAX_BYTES", 10)
    client, sent = _claude_app(tmp_path, iter([]))
    file_id = client.post("/v1/files", files={"file": ("big.txt", b"x" * 64, "text/plain")}).json()["id"]
    response = client.post("/v1/chat/completions", json={"model": "claude:sonnet", "messages": [
        {"role": "user", "content": [{"type": "file", "file": {"file_id": file_id}}]}]})
    _assert_error(response, 413, "request_too_large")
    client.__exit__(None, None, None)


def test_files_and_conversations_serve_claude_only_setups(tmp_path) -> None:
    app = create_app(make_settings(tmp_path, enable_openai_provider=False, enable_claude=True,
                                   claude_models=("claude:sonnet",)))
    with TestClient(app) as client:
        upload = client.post("/v1/files", files={"file": ("a.txt", b"hi", "text/plain")})
        assert upload.status_code == 200 and client.get("/v1/files").json()["data"][0]["id"] == upload.json()["id"]
        assert client.post("/v1/conversations", json={}).status_code == 200
