"""Image generation and edits on the ChatGPT subscription runtime.

The upstream (verified live 2026-10-18) validates almost nothing and
honours less than it accepts, so the relay checks every OpenAI Images
parameter itself, fans `n` out, delivers `stream` as OpenAI's events, and
discloses what it could not honour. The image quota is a separate upstream
bucket read from response headers; these tests cover that path too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from airelay.accounts import OpenAiAccountPool
from airelay.app import create_app
from airelay.backend import BackendError, ChatGptCodexBackend
from airelay.errors import InvalidRequestError
from airelay.image_usage import ImageUsageTracker, ImageUsageWindow, parse_image_usage_headers
from airelay.images import (
    EditImage,
    UnknownImageModelError,
    coerce_image_form,
    edit_image_from_bytes,
    image_stream_events,
    merge_image_responses,
    prepare_image_edit,
    prepare_image_generation,
    summarize_image_payload,
    unapplied_image_parameters,
)
from airelay.traffic import TrafficLogger
from airelay.transforms import normalize_subscription_status_payload
from test_accounts import FakeBackend, RecordingTraffic, _pool, _settings
from test_backend_and_app import FakeAuthManager, make_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32

# The upstream's answer, as observed live (one image per call).
UPSTREAM_HEADERS = {
    "x-codex-active-limit": "imagegen_premium",
    "x-codex-plan-type": "enterprise",
    "x-codex-primary-used-percent": "1",
    "x-codex-primary-window-minutes": "1440",
    "x-codex-primary-reset-after-seconds": "85598",
    "x-codex-primary-reset-at": "1792360977",
    "x-codex-secondary-window-minutes": "0",
    "x-codex-secondary-used-percent": "0",
}


def upstream_response(b64: str = "aW1hZ2U=", **overrides):
    payload = {
        "created": 1792274577,
        "background": "opaque",
        "output_format": "png",
        "quality": "low",
        "safety_identifier": None,
        "size": "1024x1024",
        "usage": {
            "input_tokens": 19,
            "input_tokens_details": {"image_tokens": 0, "text_tokens": 19},
            "output_tokens": 515,
            "output_tokens_details": {"image_tokens": 515, "text_tokens": 0},
            "total_tokens": 534,
        },
        "data": [{"b64_json": b64, "generation_id": "gen-1"}],
    }
    payload.update(overrides)
    return payload


def window(used: float = 1.0) -> ImageUsageWindow:
    return ImageUsageWindow("imagegen_premium", used, 86400, 1792360977, 85598, "enterprise")


# ----- request translation -----


def test_generation_defaults_send_only_the_prompt_and_model() -> None:
    prepared = prepare_image_generation({"prompt": "a red circle"})
    assert prepared.operation == "generation"
    assert prepared.payload == {"prompt": "a red circle", "model": "gpt-image-2"}
    assert prepared.model == "gpt-image-2" and prepared.count == 1 and prepared.stream is False
    assert prepared.ignored == []


def test_generation_forwards_the_honoured_parameters_and_discloses_the_rest() -> None:
    prepared = prepare_image_generation({
        "prompt": "p", "model": "gpt-image-1.5", "n": 3, "quality": "high", "size": "1536x864",
        "background": "transparent", "moderation": "low", "stream": True,
        # Not applied upstream (verified live): disclosed, never forwarded.
        "output_format": "jpeg", "output_compression": 50, "partial_images": 2, "response_format": "url",
        "style": "vivid", "user": "u1", "safety_identifier": "s1", "prompt_cache_key": "x",
        # Null means "not set" and is not disclosed.
        "seed": None,
    })
    assert prepared.payload == {
        "prompt": "p", "model": "gpt-image-1.5", "quality": "high", "background": "transparent",
        "moderation": "low", "size": "1536x864",
    }
    assert prepared.count == 3 and prepared.stream is True
    assert prepared.ignored == [
        "partial_images", "output_format", "output_compression", "response_format", "user",
        "safety_identifier", "style", "prompt_cache_key",
    ]


def test_generation_does_not_disclose_values_that_match_what_the_answer_has() -> None:
    prepared = prepare_image_generation({
        "prompt": "p", "output_format": "png", "output_compression": 100, "partial_images": 0,
        "response_format": "b64_json", "user": "", "n": 1, "stream": False,
    })
    assert prepared.ignored == []


@pytest.mark.parametrize("body, param, code", [
    ({}, "prompt", "missing_required_parameter"),
    ({"prompt": "   "}, "prompt", "invalid_value"),
    ({"prompt": "x" * 32001}, "prompt", "string_above_max_length"),
    ({"prompt": "p", "quality": "hd"}, "quality", "invalid_value"),
    ({"prompt": "p", "quality": "ultra"}, "quality", "invalid_value"),
    ({"prompt": "p", "size": "999x999"}, "size", "invalid_value"),
    ({"prompt": "p", "size": "256x256"}, "size", "invalid_value"),
    ({"prompt": "p", "size": "4096x1024"}, "size", "invalid_value"),
    ({"prompt": "p", "size": "3072x1024"}, "size", "invalid_value"),  # 3:1 is allowed, this is 3:1 — pixels ok? see below
    ({"prompt": "p", "n": 0}, "n", "invalid_value"),
    ({"prompt": "p", "n": 11}, "n", "invalid_value"),
    ({"prompt": "p", "n": "2"}, "n", "invalid_value"),
    ({"prompt": "p", "background": "blue"}, "background", "invalid_value"),
    ({"prompt": "p", "moderation": "none"}, "moderation", "invalid_value"),
    ({"prompt": "p", "stream": "yes"}, "stream", "invalid_value"),
    ({"prompt": "p", "partial_images": 4}, "partial_images", "invalid_value"),
    ({"prompt": "p", "output_compression": 101}, "output_compression", "invalid_value"),
    ({"prompt": "p", "output_format": "gif"}, "output_format", "invalid_value"),
    ({"prompt": "p", "response_format": "file"}, "response_format", "invalid_value"),
    ({"prompt": "p", "style": "bold"}, "style", "invalid_value"),
    ({"prompt": "p", "user": 5}, "user", "invalid_value"),
])
def test_generation_rejects_what_the_upstream_would_silently_replace(body, param, code) -> None:
    if body.get("size") == "3072x1024":
        # 3:1 at 3,145,728 pixels is within every rule: accepted, not rejected.
        assert prepare_image_generation(body).payload["size"] == "3072x1024"
        return
    with pytest.raises(InvalidRequestError) as caught:
        prepare_image_generation(body)
    assert caught.value.status_code == 422
    assert caught.value.param == param
    assert caught.value.code == code


@pytest.mark.parametrize("size, expected", [
    ("auto", "auto"), ("AUTO", "auto"), ("1024x1024", "1024x1024"), ("1536x1024", "1536x1024"),
    ("1024x1536", "1024x1536"), ("2048x2048", "2048x2048"), ("3840x2160", "3840x2160"),
    ("2160x3840", "2160x3840"), (" 1536x864 ", "1536x864"),
])
def test_generation_accepts_every_documented_size(size, expected) -> None:
    assert prepare_image_generation({"prompt": "p", "size": size}).payload["size"] == expected


def test_unknown_models_are_404_and_claude_models_are_refused() -> None:
    with pytest.raises(UnknownImageModelError) as caught:
        prepare_image_generation({"prompt": "p", "model": "dall-e-3"})
    assert caught.value.status_code == 404 and caught.value.code == "model_not_found"
    with pytest.raises(InvalidRequestError) as refused:
        prepare_image_generation({"prompt": "p", "model": "claude:sonnet"})
    assert refused.value.status_code == 422 and refused.value.code == "unsupported_for_provider"
    for model in ("gpt-image-2", "gpt-image-1", "gpt-image-1-mini", "gpt-image-2-2026-04-21", "chatgpt-image-latest"):
        assert prepare_image_generation({"prompt": "p", "model": model}).payload["model"] == model


def test_edit_builds_the_upstream_images_and_refuses_masks() -> None:
    images = [edit_image_from_bytes(PNG), edit_image_from_bytes(JPEG)]
    prepared = prepare_image_edit({"prompt": "make it green", "input_fidelity": "high"}, images, has_mask=False)
    assert prepared.operation == "edit"
    assert list(prepared.payload) == ["images", "prompt", "model"]
    assert prepared.payload["images"] == [
        {"image_url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
        {"image_url": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()},
    ]
    assert prepared.ignored == ["input_fidelity"]
    with pytest.raises(InvalidRequestError) as caught:
        prepare_image_edit({"prompt": "p"}, images, has_mask=True)
    assert caught.value.param == "mask" and caught.value.code == "unsupported_parameter"
    with pytest.raises(InvalidRequestError) as missing:
        prepare_image_edit({"prompt": "p"}, [], has_mask=False)
    assert missing.value.code == "missing_required_parameter" and missing.value.param == "image"
    with pytest.raises(InvalidRequestError) as too_many:
        prepare_image_edit({"prompt": "p"}, images * 9, has_mask=False)
    assert too_many.value.param == "image"


def test_edit_images_are_sniffed_and_bounded() -> None:
    assert edit_image_from_bytes(WEBP).media_type == "image/webp"
    for raw, message in ((GIF, "not a PNG, JPEG or WebP"), (b"", "empty")):
        with pytest.raises(InvalidRequestError) as caught:
            edit_image_from_bytes(raw)
        assert message in str(caught.value)
    with pytest.raises(InvalidRequestError) as big:
        edit_image_from_bytes(PNG + b"\x00" * (20 * 1024 * 1024))
    assert big.value.code == "request_too_large"


def test_multipart_fields_are_typed_like_json() -> None:
    assert coerce_image_form({"prompt": "p", "n": "2", "stream": "true", "output_compression": "80",
                              "partial_images": "0", "quality": "low"}) == {
        "prompt": "p", "n": 2, "stream": True, "output_compression": 80, "partial_images": 0, "quality": "low"}
    assert coerce_image_form({"stream": "false"})["stream"] is False
    for fields in ({"n": "two"}, {"stream": "maybe"}, {"output_compression": "1.5"}):
        with pytest.raises(InvalidRequestError):
            coerce_image_form(fields)


def test_merge_sums_usage_and_keeps_every_image_in_order() -> None:
    one = upstream_response("YQ==")
    two = upstream_response("Yg==", created=1792274600)
    merged = merge_image_responses([one, two])
    assert [item["b64_json"] for item in merged["data"]] == ["YQ==", "Yg=="]
    assert merged["created"] == 1792274577 and merged["size"] == "1024x1024"
    assert merged["usage"] == {
        "input_tokens": 38, "input_tokens_details": {"image_tokens": 0, "text_tokens": 38},
        "output_tokens": 1030, "output_tokens_details": {"image_tokens": 1030, "text_tokens": 0},
        "total_tokens": 1068,
    }
    assert merge_image_responses([one]) == one
    # An unreadable usage on one part leaves the first part's usage in place
    # rather than fabricating a sum.
    assert merge_image_responses([one, {**two, "usage": None}])["usage"] == one["usage"]


def test_stream_events_follow_the_openai_image_event_shape() -> None:
    response = merge_image_responses([upstream_response("YQ=="), upstream_response("Yg==")])
    events = image_stream_events(response, "generation")
    assert [event["type"] for event in events] == ["image_generation.completed"] * 2
    assert events[0]["b64_json"] == "YQ==" and events[1]["b64_json"] == "Yg=="
    assert events[0]["created_at"] == 1792274577 and events[0]["output_format"] == "png"
    assert events[0]["quality"] == "low" and events[0]["size"] == "1024x1024" and events[0]["background"] == "opaque"
    assert events[0]["usage"]["total_tokens"] == 1068
    assert image_stream_events(upstream_response(), "edit")[0]["type"] == "image_edit.completed"


def test_unapplied_quality_and_size_are_named_only_when_the_answer_differs() -> None:
    sent = {"prompt": "p", "model": "gpt-image-2", "quality": "high", "size": "1024x1536"}
    assert unapplied_image_parameters(sent, upstream_response(quality="low", size="1254x1254")) == ["quality", "size"]
    assert unapplied_image_parameters(sent, upstream_response(quality="high", size="1024x1536")) == []
    assert unapplied_image_parameters({"prompt": "p", "quality": "auto", "size": "auto"},
                                      upstream_response(quality="low", size="1254x1254")) == []
    assert unapplied_image_parameters({"prompt": "p"}, upstream_response()) == []
    assert unapplied_image_parameters(sent, {"data": []}) == []


def test_summaries_never_carry_image_bytes() -> None:
    data_url = "data:image/png;base64," + "A" * 600
    summary = summarize_image_payload({"data": [{"b64_json": "x" * 10, "generation_id": "g"}],
                                       "images": [{"image_url": data_url}], "image": [data_url], "prompt": "p"})
    assert summary == {"data": [{"b64_json": "<base64 image, 10 chars>", "generation_id": "g"}],
                       "images": [{"image_url": f"<data URL, {len(data_url)} chars>"}],
                       "image": [f"<data URL, {len(data_url)} chars>"], "prompt": "p"}


# ----- image quota -----


def test_quota_headers_become_a_window_regardless_of_case() -> None:
    parsed = parse_image_usage_headers({key.upper(): value for key, value in UPSTREAM_HEADERS.items()})
    assert parsed == ImageUsageWindow("imagegen_premium", 1.0, 86400, 1792360977, 85598, "enterprise")
    assert parsed.exhausted is False
    assert parse_image_usage_headers({"content-type": "application/json"}) is None
    assert parse_image_usage_headers(None) is None
    assert parse_image_usage_headers({"x-codex-primary-used-percent": "100"}).exhausted is True


def test_tracker_counts_relay_images_inside_the_window_and_persists(tmp_path, monkeypatch) -> None:
    path = tmp_path / "images.json"
    tracker = ImageUsageTracker(path)
    assert tracker.snapshot("acct") is None and tracker.snapshot(None) is None
    now = {"value": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: now["value"])
    tracker.record("acct", window(1.0), images=1, usage=upstream_response()["usage"])
    now["value"] += 3600
    tracker.record("acct", window(2.0), images=2, usage={"output_tokens": 700})
    snapshot = tracker.snapshot("acct")
    assert snapshot["limit_id"] == "imagegen_premium" and snapshot["used_percent"] == 2.0
    assert snapshot["window_seconds"] == 86400 and snapshot["window_label"] == "24h"
    assert snapshot["reset_at"] == 1792360977 and snapshot["source"] == "image_response"
    assert snapshot["observed_at"] == "1970-01-12T14:46:40Z"
    assert snapshot["relay"] == {"scope": "last_window_via_this_relay", "window_seconds": 86400,
                                 "requests": 2, "images": 3, "image_tokens": 1215}
    # Older than the window: the first event falls out of the tally.
    now["value"] += 86400 - 1800
    assert tracker.snapshot("acct")["relay"]["images"] == 2
    # A probe refreshes the reading without counting an image.
    tracker.observe("acct", window(5.0), source="probe")
    reloaded = ImageUsageTracker(path).snapshot("acct")
    assert reloaded["used_percent"] == 5.0 and reloaded["source"] == "probe"
    assert reloaded["relay"]["images"] == 2
    assert ImageUsageTracker(path).used_percent("acct") == 5.0


def test_status_payload_carries_the_image_quota_in_the_rate_limit_shape(tmp_path) -> None:
    tracker = ImageUsageTracker(tmp_path / "images.json")
    tracker.observe("acct", window(41.5), source="probe")
    payload = normalize_subscription_status_payload({"plan_type": "pro", "rate_limit": {}},
                                                    image_usage=tracker.snapshot("acct"))
    images = payload["rate_limits"]["image_generation"]
    assert images["limit_id"] == "imagegen_premium"
    assert images["allowed"] is True and images["limit_reached"] is False
    assert images["source"] == "probe" and images["observed_at"].endswith("Z")
    assert images["secondary_window"] is None
    assert images["primary_window"]["used_percent"] == 41.5
    assert images["primary_window"]["remaining_percent"] == 58.5
    assert images["primary_window"]["window_label"] == "24h"
    assert images["primary_window"]["window_minutes"] == 1440
    assert images["primary_window"]["reset_at"] == 1792360977
    assert images["relay"]["images"] == 0
    assert normalize_subscription_status_payload({})["rate_limits"]["image_generation"] is None


# ----- backend -----


@pytest.mark.asyncio
async def test_backend_posts_json_to_the_image_routes_and_reads_the_quota(tmp_path) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        captured["account"] = request.headers.get("chatgpt-account-id")
        if captured["body"] == {}:
            return httpx.Response(400, json={"error": {"message": "Missing required parameter: 'prompt'.",
                                                       "type": "invalid_request_error", "param": "prompt",
                                                       "code": "missing_required_parameter"}},
                                  headers=UPSTREAM_HEADERS)
        if captured["body"].get("prompt") == "limit":
            return httpx.Response(429, json={"error": {"type": "usage_limit_reached", "message": "no",
                                                       "resets_in_seconds": 120}},
                                  headers={**UPSTREAM_HEADERS, "x-codex-primary-used-percent": "100"})
        return httpx.Response(200, json=upstream_response(), headers=UPSTREAM_HEADERS)

    backend = ChatGptCodexBackend(
        settings=make_settings(tmp_path),
        auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
        traffic=TrafficLogger(tmp_path / "logs"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        payload, quota = await backend.create_image("generation", {"prompt": "p", "model": "gpt-image-2"}, "req_1")
        assert captured["url"] == "https://chatgpt.com/backend-api/codex/images/generations"
        assert captured["body"] == {"prompt": "p", "model": "gpt-image-2"}
        assert captured["authorization"] == "Bearer chatgpt-token" and captured["account"] == "account-123"
        assert payload["data"][0]["b64_json"] == "aW1hZ2U="
        assert quota == window(1.0)

        await backend.create_image("edit", {"images": [], "prompt": "p", "model": "gpt-image-2"}, "req_2")
        assert captured["url"] == "https://chatgpt.com/backend-api/codex/images/edits"

        with pytest.raises(BackendError) as limited:
            await backend.create_image("generation", {"prompt": "limit", "model": "gpt-image-2"}, "req_3")
        assert limited.value.status_code == 429
        assert limited.value.image_usage is not None and limited.value.image_usage.exhausted

        probed = await backend.probe_image_usage("req_4")
        assert captured["body"] == {} and probed == window(1.0)
    finally:
        await backend.close()


# ----- account pool -----


class ImageFakeBackend(FakeBackend):
    """Text calls keep FakeBackend's behaviour; image calls fail only
    with `image_fail_with`, since the two quotas are separate upstream."""

    def __init__(self, name, image_fail_with=None, quota=None, probe=None):
        super().__init__(name)
        self.image_fail_with = image_fail_with
        self.quota = quota
        self.probe = probe
        self.image_calls = 0
        self.probes = 0

    async def create_image(self, operation, payload, request_id):
        self.image_calls += 1
        if self.image_fail_with is not None:
            raise self.image_fail_with
        return upstream_response(served_by=self.name), self.quota

    async def probe_image_usage(self, request_id):
        self.probes += 1
        return self.probe


def _limit_error(used: float = 100.0) -> BackendError:
    error = BackendError(429, json.dumps({"error": {"type": "usage_limit_reached", "message": "spent",
                                                    "resets_in_seconds": 90}}))
    error.image_usage = window(used)
    return error


@pytest.mark.asyncio
async def test_pool_fails_over_images_and_benches_only_the_image_bucket(tmp_path) -> None:
    settings = _settings(tmp_path)
    a = ImageFakeBackend("a", image_fail_with=_limit_error())
    b = ImageFakeBackend("b", quota=window(3.0))
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)
    result = await pool.create_image("generation", {"prompt": "p", "model": "gpt-image-2"}, "req_1")
    assert result["served_by"] == "b" and a.image_calls == 1 and b.image_calls == 1
    assert "account_failover" in traffic.phases()
    now = time.perf_counter()
    first, second = pool._accounts
    assert first.is_image_limited(now) and not first.is_limited(now)
    assert not second.is_image_limited(now)
    # Text traffic still uses the image-benched account.
    assert (await pool.collect_response({}, "req_2", None))["served_by"] == "a"
    # The exhausted reading from the failed call was kept for the meter.
    statuses = pool.account_statuses()
    assert statuses[0]["image_usage"]["used_percent"] == 100.0 and statuses[0]["image_limited"] is True
    assert statuses[1]["image_usage"]["relay"]["images"] == 1 and statuses[1]["image_usage"]["used_percent"] == 3.0
    # Balanced routing prefers the account with the lower image usage once both serve.
    first.image_limited_until = 0.0
    assert pool._image_attempt_order()[0].backend.name == "b"


@pytest.mark.asyncio
async def test_pool_does_not_rotate_images_on_request_errors(tmp_path) -> None:
    settings = _settings(tmp_path)
    a = ImageFakeBackend("a", image_fail_with=BackendError(400, json.dumps({"error": {
        "message": "Invalid value: 'blue'.", "type": "invalid_request_error", "param": "background",
        "code": "invalid_value"}})))
    b = ImageFakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as caught:
        await pool.create_image("generation", {"prompt": "p"}, "req_1")
    assert caught.value.status_code == 400 and b.image_calls == 0
    assert not pool._accounts[0].is_image_limited(time.perf_counter())


@pytest.mark.asyncio
async def test_pool_reports_when_every_account_is_out_of_images(tmp_path) -> None:
    settings = _settings(tmp_path)
    pool = _pool(settings, [ImageFakeBackend("a", image_fail_with=_limit_error()),
                            ImageFakeBackend("b", image_fail_with=_limit_error())], RecordingTraffic())
    with pytest.raises(BackendError) as caught:
        await pool.create_image("generation", {"prompt": "p"}, "req_1")
    error = json.loads(caught.value.detail)["error"]
    assert caught.value.status_code == 429
    assert error["message"].startswith("All 2 OpenAI accounts are unavailable for image generation")
    assert error["type"] == "usage_limit_reached" and error["resets_in_seconds"] >= 1


@pytest.mark.asyncio
async def test_usage_probe_reads_the_image_quota_when_enabled(tmp_path) -> None:
    settings = _settings(tmp_path)
    a = ImageFakeBackend("a", probe=window(100.0))
    pool = _pool(settings, [a], RecordingTraffic())
    entries = await pool.subscription_statuses("req_1", force=True)
    assert a.probes == 1
    assert entries[0]["image_usage"]["used_percent"] == 100.0 and entries[0]["image_usage"]["source"] == "probe"
    # An exhausted reading benches the image bucket proactively, text stays open.
    now = time.perf_counter()
    assert pool._accounts[0].is_image_limited(now) and not pool._accounts[0].is_limited(now)
    # A fresher reading with capacity releases it.
    a.probe = window(20.0)
    await pool.subscription_statuses("req_2", force=True)
    assert not pool._accounts[0].is_image_limited(time.perf_counter())
    assert pool.image_usage_snapshot()["used_percent"] == 20.0

    quiet = _pool(_settings(tmp_path / "quiet", image_usage_probe=False), [ImageFakeBackend("q", probe=window())],
                  RecordingTraffic())
    await quiet.subscription_statuses("req_3", force=True)
    assert quiet._accounts[0].backend.probes == 0 and quiet.image_usage_snapshot() is None


# ----- routes -----


def _client(tmp_path, **overrides):
    app = create_app(make_settings(tmp_path, **overrides))
    client = TestClient(app)
    client.__enter__()
    return client


def _install_fake(client, calls: list, response=None, error=None):
    async def create_image(operation, payload, request_id):
        calls.append((operation, payload))
        if error is not None:
            raise error
        return response or upstream_response()

    client.app.state.backend.create_image = create_image


def test_generations_route_answers_the_openai_images_shape_and_discloses(tmp_path) -> None:
    client = _client(tmp_path)
    calls: list = []
    _install_fake(client, calls)
    try:
        response = client.post("/v1/images/generations", json={
            "prompt": "a red circle", "size": "1024x1024", "quality": "low",
            "output_format": "jpeg", "user": "me", "response_format": "b64_json"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["data"][0]["b64_json"] == "aW1hZ2U=" and body["usage"]["total_tokens"] == 534
        assert body["created"] == 1792274577 and body["size"] == "1024x1024"
        assert response.headers["x-airelays-ignored-parameters"] == "output_format,user"
        assert response.headers["x-request-id"].startswith("req_")
        assert calls == [("generation", {"prompt": "a red circle", "model": "gpt-image-2",
                                         "quality": "low", "size": "1024x1024"})]
    finally:
        client.__exit__(None, None, None)


def test_generations_route_discloses_a_replaced_quality_and_size(tmp_path) -> None:
    client = _client(tmp_path)
    _install_fake(client, [], response=upstream_response(quality="low", size="1254x1254"))
    try:
        response = client.post("/v1/images/generations", json={
            "prompt": "p", "quality": "high", "size": "1024x1536", "output_format": "webp"})
        assert response.status_code == 200
        assert response.headers["x-airelays-ignored-parameters"] == "output_format,quality,size"
        assert response.json()["size"] == "1254x1254" and response.json()["quality"] == "low"
        streamed = client.post("/v1/images/generations", json={"prompt": "p", "quality": "high", "stream": True})
        assert streamed.headers["x-airelays-ignored-parameters"] == "quality"
    finally:
        client.__exit__(None, None, None)


def test_generations_route_fans_out_n_and_merges(tmp_path) -> None:
    client = _client(tmp_path)
    calls: list = []
    counter = {"n": 0}

    async def create_image(operation, payload, request_id):
        counter["n"] += 1
        index = counter["n"]
        calls.append(payload)
        await asyncio.sleep(0)
        return upstream_response(base64.b64encode(f"img{index}".encode()).decode())

    client.app.state.backend.create_image = create_image
    try:
        response = client.post("/v1/images/generations", json={"prompt": "p", "n": 3})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(calls) == 3 and all("n" not in payload for payload in calls)
        assert sorted(base64.b64decode(item["b64_json"]).decode() for item in body["data"]) == ["img1", "img2", "img3"]
        assert body["usage"]["total_tokens"] == 534 * 3
        assert "x-airelays-ignored-parameters" not in response.headers
    finally:
        client.__exit__(None, None, None)


def test_generations_route_streams_completed_events(tmp_path) -> None:
    client = _client(tmp_path)
    _install_fake(client, [])
    try:
        response = client.post("/v1/images/generations", json={"prompt": "p", "stream": True, "partial_images": 2})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-airelays-ignored-parameters"] == "partial_images"
        lines = [line for line in response.text.split("\n") if line.startswith("data: ")]
        assert len(lines) == 1
        event = json.loads(lines[0][len("data: "):])
        assert event["type"] == "image_generation.completed" and event["b64_json"] == "aW1hZ2U="
        assert event["usage"]["total_tokens"] == 534
    finally:
        client.__exit__(None, None, None)


def test_edits_route_takes_openai_multipart_and_local_files(tmp_path) -> None:
    client = _client(tmp_path)
    calls: list = []
    _install_fake(client, calls)
    try:
        response = client.post(
            "/v1/images/edits",
            files=[("image", ("a.png", PNG, "image/png")), ("image[]", ("b.jpg", JPEG, "image/jpeg"))],
            data={"prompt": "make it green", "n": "1", "size": "1024x1024", "output_format": "webp",
                  "input_fidelity": "high", "stream": "false"},
        )
        assert response.status_code == 200, response.text
        # The fake renders the requested 1024x1024, so only the dropped
        # parameters are disclosed (a replaced size would be listed too).
        assert response.headers["x-airelays-ignored-parameters"] == "output_format,input_fidelity"
        operation, payload = calls[-1]
        assert operation == "edit"
        assert [image["image_url"][:15] for image in payload["images"]] == ["data:image/png;", "data:image/jpeg"]
        assert payload["prompt"] == "make it green" and payload["size"] == "1024x1024" and "n" not in payload

        # A mask cannot be honoured: refused, never silently dropped.
        masked = client.post("/v1/images/edits", files=[("image", ("a.png", PNG, "image/png")),
                                                        ("mask", ("m.png", PNG, "image/png"))],
                             data={"prompt": "p"})
        assert masked.status_code == 422 and masked.json()["error"]["param"] == "mask"

        # JSON: data URLs and ids of files uploaded to /v1/files.
        file_id = client.post("/v1/files", files={"file": ("c.webp", WEBP, "image/webp")},
                              data={"purpose": "vision"}).json()["id"]
        data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        response = client.post("/v1/images/edits", json={"image": [file_id, data_url], "prompt": "p",
                                                          "background": "transparent"})
        assert response.status_code == 200, response.text
        _, payload = calls[-1]
        assert [image["image_url"][:16] for image in payload["images"]] == ["data:image/webp;", "data:image/png;b"]
        assert payload["background"] == "transparent"
        single = client.post("/v1/images/edits", json={"image": data_url, "prompt": "p"})
        assert single.status_code == 200 and len(calls[-1][1]["images"]) == 1

        unknown = client.post("/v1/images/edits", json={"image": "file_missing", "prompt": "p"})
        assert unknown.status_code == 422 and "file_missing" in unknown.json()["error"]["message"]
        wrong = client.post("/v1/images/edits", json={"image": ["data:image/gif;base64," + base64.b64encode(GIF).decode()],
                                                       "prompt": "p"})
        assert wrong.status_code == 422 and wrong.json()["error"]["param"] == "image"
        empty = client.post("/v1/images/edits", json={"prompt": "p"})
        assert empty.status_code == 422 and empty.json()["error"]["code"] == "missing_required_parameter"
    finally:
        client.__exit__(None, None, None)


def test_image_routes_refuse_what_they_cannot_serve(tmp_path) -> None:
    client = _client(tmp_path)
    _install_fake(client, [])
    try:
        unknown = client.post("/v1/images/generations", json={"prompt": "p", "model": "dall-e-3"})
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "model_not_found"
        claude = client.post("/v1/images/generations", json={"prompt": "p", "model": "claude:sonnet"})
        assert claude.status_code == 422 and claude.json()["error"]["code"] == "unsupported_for_provider"
        invalid = client.post("/v1/images/generations", json={"prompt": "p", "quality": "hd"})
        assert invalid.status_code == 422 and invalid.json()["error"]["param"] == "quality"
        bad_json = client.post("/v1/images/generations", content="{nope", headers={"content-type": "application/json"})
        assert bad_json.status_code == 400
        variations = client.post("/v1/images/variations", json={"image": "x"})
        assert variations.status_code == 501 and variations.headers["x-should-retry"] == "false"
    finally:
        client.__exit__(None, None, None)

    disabled = _client(tmp_path / "disabled", enable_openai_provider=False)
    try:
        response = disabled.post("/v1/images/generations", json={"prompt": "p"})
        assert response.status_code == 501 and response.headers["x-should-retry"] == "false"
        assert "OpenAI runtime" in response.json()["error"]["message"]
    finally:
        disabled.__exit__(None, None, None)


def test_image_quota_errors_reach_the_client_with_retry_after(tmp_path) -> None:
    client = _client(tmp_path)
    _install_fake(client, [], error=BackendError(429, json.dumps({"error": {
        "type": "usage_limit_reached", "message": "Image limit reached.", "resets_in_seconds": 3600}})))
    try:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "3600"
        assert response.json()["error"]["type"] == "usage_limit_reached"
    finally:
        client.__exit__(None, None, None)


def test_models_list_the_image_model_with_its_controls(tmp_path) -> None:
    client = _client(tmp_path)

    async def list_models(request_id):
        return {"models": [{"slug": "gpt-5.5"}]}

    client.app.state.backend.list_models = list_models
    try:
        models = {item["id"]: item for item in client.get("/v1/models").json()["data"]}
        image = models["gpt-image-2"]["airelays"]
        assert image["capabilities"]["routes"]["images"] is True
        assert image["capabilities"]["routes"]["chat_completions"] is False
        assert image["capabilities"]["tool_calls"] is False and image["capabilities"]["inputs"] == ["text", "image"]
        assert image["capabilities"]["image_generation"]["quality"] == ["auto", "low", "medium", "high"]
        assert image["capabilities"]["image_generation"]["mask"] is False
        assert image["reasoning"]["modes"] == []
        text = models["gpt-5.5"]["airelays"]["capabilities"]
        assert text["routes"]["images"] is False and text["image_generation"] is None and text["tool_calls"] is True
        assert client.get("/v1/models/gpt-image-2").json()["id"] == "gpt-image-2"
    finally:
        client.__exit__(None, None, None)


def test_subscription_status_route_includes_the_image_quota(tmp_path) -> None:
    client = _client(tmp_path)

    async def usage(request_id, slug=None):
        return {"plan_type": "pro", "rate_limit": {"primary_window": {"used_percent": 4, "limit_window_seconds": 18000}}}

    tracker = ImageUsageTracker(tmp_path / "images.json")
    tracker.record("acct", window(7.0), images=2, usage={"output_tokens": 1000})
    client.app.state.backend.get_subscription_status = usage
    client.app.state.backend.image_usage_snapshot = lambda slug=None: tracker.snapshot("acct")
    try:
        payload = client.get("/v1/subscription/status").json()
        images = payload["rate_limits"]["image_generation"]
        assert images["primary_window"]["used_percent"] == 7.0 and images["primary_window"]["window_label"] == "24h"
        assert images["relay"] == {"scope": "last_window_via_this_relay", "window_seconds": 86400,
                                   "requests": 1, "images": 2, "image_tokens": 1000}
        assert payload["rate_limits"]["default"]["primary_window"]["window_label"] == "5h"
    finally:
        client.__exit__(None, None, None)
