"""Multi-account discovery, selection, and failover behavior.

These tests illustrate the intended behavior; the pool logic must work for
any account count and any upstream error shape, not just these examples.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from airelay.accounts import (
    ACCOUNTS_DIRNAME,
    OpenAiAccountPool,
    discover_slots,
    find_slot,
    save_manifest,
    slug_for_account,
)
from airelay.auth import AuthManager
from airelay.backend import BackendError, SSEEvent
from airelay.config import Settings


class RecordingTraffic:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def write(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)

    def phases(self) -> list[str]:
        return [entry.get("phase") for entry in self.entries]


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "logs_dir": tmp_path / "logs",
        "auth_storage_mode": "file",
        "config_path": tmp_path / "config.toml",
    }
    values.update(overrides)
    return Settings(**values)


def _fake_id_token(account_id: str, email: str, plan: str = "plus") -> str:
    """A real login stores email/plan only inside the signed id_token; the
    fixture must mirror that shape or it silently validates a bug."""

    def segment(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = segment({"alg": "none", "typ": "JWT"})
    claims = segment(
        {
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": plan,
                "chatgpt_account_id": account_id,
            },
        }
    )
    return f"{header}.{claims}.sig"


def _write_auth(root: Path, account_id: str, email: str, plan: str = "plus") -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "bound_account_id": account_id,
        "tokens": {
            "id_token": _fake_id_token(account_id, email, plan),
            "access_token": f"at-{account_id}",
            "refresh_token": f"rt-{account_id}",
            "account_id": account_id,
        },
        "last_refresh": "2026-07-05T00:00:00+00:00",
    }
    (root / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


class FakeBackend:
    """Duck-typed stand-in for ChatGptCodexBackend."""

    def __init__(self, name: str, fail_with: BackendError | None = None) -> None:
        self.name = name
        self.fail_with = fail_with
        self.calls = 0

    async def collect_response(self, payload, request_id, session_id):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return {"served_by": self.name}

    async def stream_response_events(self, payload, request_id, session_id) -> AsyncIterator[SSEEvent]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        yield SSEEvent(event="response.completed", data=json.dumps({"served_by": self.name}))

    async def list_models(self, request_id):
        return {"models": []}

    async def get_subscription_status(self, request_id):
        return {"account": self.name}

    async def close(self) -> None:
        return None


def _pool(settings: Settings, backends: list[FakeBackend], traffic: RecordingTraffic) -> OpenAiAccountPool:
    accounts = []
    for backend in backends:
        root = settings.data_dir / ACCOUNTS_DIRNAME / backend.name
        _write_auth(root, account_id=f"acct-{backend.name}", email=f"{backend.name}@example.com")
        manager = AuthManager(root, "file", settings.issuer_base_url)
        accounts.append((manager, backend))
    return OpenAiAccountPool(settings, traffic, accounts=accounts)  # type: ignore[arg-type]


# ---------- discovery ----------


def test_discover_legacy_root_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_auth(settings.data_dir, "acct-1", "solo@example.com")
    slots = discover_slots(settings)
    assert [slot.slug for slot in slots] == ["default"]
    assert slots[0].email == "solo@example.com"
    assert slots[0].storage_root == settings.data_dir


def test_discover_legacy_plus_named_accounts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_auth(settings.data_dir, "acct-1", "first@example.com")
    _write_auth(settings.data_dir / ACCOUNTS_DIRNAME / "second-x", "acct-2", "second@example.com")
    slots = discover_slots(settings)
    assert [slot.email for slot in slots] == ["first@example.com", "second@example.com"]


def test_discover_finds_keyring_account_by_directory(tmp_path: Path) -> None:
    # Keyring-mode accounts write no auth.json; the slot directory is what
    # makes them discoverable, so an empty (but present) dir with a keyring
    # payload must still be found.
    import airelay.accounts as accounts_module

    settings = _settings(tmp_path, auth_storage_mode="auto")
    _write_auth(settings.data_dir, "acct-1", "first@example.com")
    slot_dir = settings.data_dir / ACCOUNTS_DIRNAME / "second-x"
    slot_dir.mkdir(parents=True)  # created by login, no file inside

    real_storage = accounts_module.AuthStorage

    class KeyringOnlyStorage(real_storage):
        def load(self):
            if self.storage_root == slot_dir:
                return {
                    "bound_account_id": "acct-2",
                    "tokens": {
                        "id_token": _fake_id_token("acct-2", "second@example.com"),
                        "access_token": "at",
                        "account_id": "acct-2",
                    },
                }
            return super().load()

    accounts_module.AuthStorage = KeyringOnlyStorage
    try:
        slots = discover_slots(settings)
    finally:
        accounts_module.AuthStorage = real_storage
    assert {slot.email for slot in slots} == {"first@example.com", "second@example.com"}


def test_discover_dedupes_same_account_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_auth(settings.data_dir, "acct-1", "first@example.com")
    _write_auth(settings.data_dir / ACCOUNTS_DIRNAME / "dup", "acct-1", "first@example.com")
    slots = discover_slots(settings)
    assert len(slots) == 1


def test_manifest_order_controls_priority(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_auth(settings.data_dir, "acct-1", "first@example.com")
    _write_auth(settings.data_dir / ACCOUNTS_DIRNAME / "second", "acct-2", "second@example.com")
    save_manifest(settings.data_dir, {"order": ["second", "default"]})
    slots = discover_slots(settings)
    assert [slot.slug for slot in slots] == ["second", "default"]


def test_find_slot_by_email_and_prefix(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_auth(settings.data_dir, "acct-1", "work@company.com")
    _write_auth(settings.data_dir / ACCOUNTS_DIRNAME / "perso", "acct-2", "perso@gmail.com")
    slots = discover_slots(settings)
    assert find_slot(slots, "perso@gmail.com").slug == "perso"
    assert find_slot(slots, "work").email == "work@company.com"
    assert find_slot(slots, "nobody") is None


def test_slug_is_path_safe_and_stable() -> None:
    slug_a = slug_for_account("acct_ABC", "Some.User+tag@example.com")
    assert slug_a == slug_for_account("acct_ABC", "Some.User+tag@example.com")
    assert "/" not in slug_a and " " not in slug_a


# ---------- selection ----------


@pytest.mark.asyncio
async def test_ordered_spillover_uses_first_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_balance="ordered")
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    for _ in range(3):
        result = await pool.collect_response({}, "req", None)
        assert result["served_by"] == "a"
    assert (a.calls, b.calls) == (3, 0)


@pytest.mark.asyncio
async def test_round_robin_spreads_requests(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_balance="round_robin")
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    served = [
        (await pool.collect_response({}, "req", None))["served_by"] for _ in range(4)
    ]
    assert served.count("a") == 2 and served.count("b") == 2


@pytest.mark.asyncio
async def test_session_sticks_to_one_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_balance="round_robin")
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    served = {
        (await pool.collect_response({}, "req", "conv-1"))["served_by"] for _ in range(4)
    }
    assert len(served) == 1


# ---------- failover ----------


def _usage_limit_error() -> BackendError:
    body = json.dumps(
        {"error": {"type": "usage_limit_reached", "message": "limit", "resets_in_seconds": 120}}
    )
    return BackendError(429, body)


@pytest.mark.asyncio
async def test_failover_on_usage_limit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_usage_limit_error())
    b = FakeBackend("b")
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)
    result = await pool.collect_response({}, "req", None)
    assert result["served_by"] == "b"
    assert "account_failover" in traffic.phases()
    # Account a is benched: the next request goes straight to b.
    result = await pool.collect_response({}, "req2", None)
    assert result["served_by"] == "b"
    assert a.calls == 1


@pytest.mark.asyncio
async def test_streaming_failover_before_first_byte(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_usage_limit_error())
    b = FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    events = [
        event async for event in pool.stream_response_events({}, "req", None)
    ]
    assert len(events) == 1
    assert json.loads(events[0].data)["served_by"] == "b"


@pytest.mark.asyncio
async def test_client_errors_do_not_fail_over(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=BackendError(400, "bad request"))
    b = FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)
    assert excinfo.value.status_code == 400
    assert b.calls == 0


@pytest.mark.asyncio
async def test_marker_text_in_a_client_error_body_does_not_bench(tmp_path: Path) -> None:
    """A 400 whose body merely echoes a marker string (e.g. request content
    quoting 'usage_limit_reached') must not be classified as a limit: one
    poison request could otherwise bench the entire pool."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=BackendError(400, "echo: usage_limit_reached in prompt"))
    b = FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError):
        await pool.collect_response({}, "req", None)
    assert not pool._accounts[0].is_limited(time.monotonic())
    assert b.calls == 0


@pytest.mark.asyncio
async def test_dead_credentials_fail_over_to_the_next_account(tmp_path: Path) -> None:
    """One account's expired/broken auth must not kill the request while a
    healthy account sits ready."""
    from airelay.auth import AuthenticationError

    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=AuthenticationError("token refresh failed"))
    b = FakeBackend("b")
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)
    result = await pool.collect_response({}, "req", None)
    assert result["served_by"] == "b"
    assert "account_failover" in traffic.phases()


@pytest.mark.asyncio
async def test_persistent_401_fails_over(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=BackendError(401, "invalid token"))
    b = FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    result = await pool.collect_response({}, "req", None)
    assert result["served_by"] == "b"


@pytest.mark.asyncio
async def test_transport_errors_fail_over(tmp_path: Path) -> None:
    """The backend wraps connect/timeout failures as BackendError(502); the
    pool must treat them as account-scoped and try the next account."""
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=BackendError(502, "Upstream connection failed: timeout"))
    b = FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    result = await pool.collect_response({}, "req", None)
    assert result["served_by"] == "b"


@pytest.mark.asyncio
async def test_all_accounts_limited_reports_actionable_error(tmp_path: Path) -> None:
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_usage_limit_error())
    b = FakeBackend("b", fail_with=_usage_limit_error())
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)
    assert "2 OpenAI accounts" in excinfo.value.detail
    # The last attempt is benched too: an exhausted final account must not be
    # re-selected as "healthy" and hammered by every subsequent request.
    now = time.monotonic()
    assert all(account.is_limited(now) for account in pool._accounts)


@pytest.mark.asyncio
async def test_all_accounts_limited_error_stays_structured_for_retry(tmp_path: Path) -> None:
    """The all-benched rewrite must keep OpenAI error JSON with the pool's
    bench horizon in resets_in_seconds: the retry layer's futile-wait check
    and machine-readable client handling both parse it."""
    from airelay.retry import _resets_in_seconds

    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_usage_limit_error())
    b = FakeBackend("b", fail_with=_usage_limit_error())
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)

    error = json.loads(excinfo.value.detail)["error"]
    assert error["type"] == "usage_limit_reached"
    assert "2 OpenAI accounts" in error["message"]
    # Both benches are quota-kind, so claiming "at their limits" is factual.
    assert "at their limits" in error["message"]
    assert error["resets_in_seconds"] >= 1
    assert _resets_in_seconds(excinfo.value) == error["resets_in_seconds"]


def _transient_502_error() -> BackendError:
    return BackendError(
        502, json.dumps({"error": {"code": "server_is_overloaded", "message": "down"}})
    )


@pytest.mark.asyncio
async def test_all_transient_failures_do_not_claim_account_limits(tmp_path) -> None:
    """operator 2026-08-01: the relay answered "All 2 OpenAI accounts are
    at their limits (earliest retry in 25s)" off the back of 30-second
    transient cooldowns while the accounts had 64%/33% of their weekly
    budgets left. "At their limits" is a factual claim about the accounts:
    a round of transient 5xx benches must say what actually happened and
    must not advertise a recovery horizon it does not have."""
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_transient_502_error())
    b = FakeBackend("b", fail_with=_transient_502_error())
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)

    assert excinfo.value.status_code == 502
    error = json.loads(excinfo.value.detail)["error"]
    assert "All 2 OpenAI accounts failed for this request" in error["message"]
    assert "at their limits" not in error["message"]
    assert "resets_in_seconds" not in error
    # The benches themselves are labeled as the heuristic rests they are.
    assert [account.limited_kind for account in pool._accounts] == ["transient", "transient"]


@pytest.mark.asyncio
async def test_mixed_bench_kinds_do_not_claim_account_limits(tmp_path) -> None:
    """One account genuinely at its limit plus one transient failure is NOT
    "all accounts at their limits" — the claim requires every bench in the
    round to be limit-class evidence."""
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_usage_limit_error())
    b = FakeBackend("b", fail_with=_transient_502_error())
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)

    error = json.loads(excinfo.value.detail)["error"]
    assert "at their limits" not in error["message"]
    assert "All 2 OpenAI accounts failed for this request" in error["message"]
    assert {account.limited_kind for account in pool._accounts} == {"quota", "transient"}


def _created_stream_event(response_id: str = "resp_1") -> SSEEvent:
    return SSEEvent(
        "response.created",
        json.dumps({"response": {"id": response_id, "status": "in_progress"}}),
    )


def _delta_stream_event(text: str = "hi") -> SSEEvent:
    return SSEEvent("response.output_text.delta", json.dumps({"delta": text}))


def _completed_stream_event(served_by: str) -> SSEEvent:
    return SSEEvent(
        "response.completed",
        json.dumps(
            {
                "response": {
                    "id": f"resp_{served_by}",
                    "status": "completed",
                    "served_by": served_by,
                    "usage": {"total_tokens": 2},
                }
            }
        ),
    )


def _failure_stream_events(code: str = "usage_limit_reached") -> list[SSEEvent]:
    return [
        SSEEvent(
            "error",
            json.dumps(
                {
                    "type": "error",
                    "error": {"type": code, "code": code, "message": "limit hit"},
                    "sequence_number": 2,
                }
            ),
        ),
        SSEEvent(
            "response.failed",
            json.dumps(
                {
                    "response": {
                        "id": "resp_fail",
                        "status": "failed",
                        "error": {"code": code, "message": "limit hit"},
                    }
                }
            ),
        ),
    ]


class EventStreamBackend(FakeBackend):
    """FakeBackend whose stream yields a scripted event sequence."""

    def __init__(self, name: str, events: list[SSEEvent]) -> None:
        super().__init__(name)
        self.events = events

    async def stream_response_events(self, payload, request_id, session_id) -> AsyncIterator[SSEEvent]:
        self.calls += 1
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_stream_failure_event_before_content_benches_and_fails_over(tmp_path: Path) -> None:
    """The 2026-08-01 incident grammar (`created` then failure events, no
    content): the pool must bench the failing account and serve the request
    from the next one, exactly like the non-streaming path."""
    import time

    settings = _settings(tmp_path)
    a = EventStreamBackend("a", [_created_stream_event(), *_failure_stream_events()])
    b = EventStreamBackend(
        "b", [_created_stream_event("resp_b"), _delta_stream_event(), _completed_stream_event("b")]
    )
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)

    events = [event async for event in pool.stream_response_events({}, "req", None)]

    assert [event.event for event in events] == [
        "response.created",
        "response.output_text.delta",
        "response.completed",
    ]
    assert json.loads(events[-1].data)["response"]["served_by"] == "b"
    assert "account_failover" in traffic.phases()
    assert pool._accounts[0].is_limited(time.monotonic())


@pytest.mark.asyncio
async def test_stream_pre_content_silent_death_fails_over(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = EventStreamBackend("a", [_created_stream_event()])  # dies before content
    b = EventStreamBackend("b", [_created_stream_event("resp_b"), _completed_stream_event("b")])
    pool = _pool(settings, [a, b], RecordingTraffic())

    events = [event async for event in pool.stream_response_events({}, "req", None)]

    assert json.loads(events[-1].data)["response"]["served_by"] == "b"


def _invalid_request_stream_events() -> list[SSEEvent]:
    """The 2026-08-01 incident grammar (req_7d3b0b16f7ee43a5a6569c38b6d46133):
    a deterministic invalid_request_error with param="input", then
    response.failed. Its real code was scrubbed from the incident log; the
    stand-in code is also client-class on its own."""
    message = (
        "Your input exceeds the context window of this model. "
        "Please adjust your input and try again."
    )
    return [
        SSEEvent(
            "error",
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                        "message": message,
                        "param": "input",
                    },
                }
            ),
        ),
        SSEEvent(
            "response.failed",
            json.dumps(
                {
                    "response": {
                        "id": "resp_fail",
                        "status": "failed",
                        "error": {"code": "context_length_exceeded", "message": message},
                    }
                }
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_invalid_request_stream_error_fails_fast_without_rotation_or_bench(tmp_path: Path) -> None:
    """The 2026-08-01 incident regression at pool level: a request-scoped
    upstream rejection (oversized input) must surface immediately as the
    upstream's own 400 — zero failover, zero benching, zero extra paid
    upstream calls. Rotating accounts on a deterministic client error can
    only reproduce it at full price (8 calls burned in the incident)."""
    import time

    settings = _settings(tmp_path)
    a = EventStreamBackend("a", [_created_stream_event(), *_invalid_request_stream_events()])
    b = EventStreamBackend("b", [_created_stream_event("resp_b"), _completed_stream_event("b")])
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)

    with pytest.raises(BackendError) as excinfo:
        async for _ in pool.stream_response_events({}, "req", None):
            pass

    assert excinfo.value.status_code == 400
    error = json.loads(excinfo.value.detail)["error"]
    # Faithful passthrough: the client sees the real upstream vocabulary.
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"
    assert error["param"] == "input"
    assert "context window" in error["message"]
    assert "at their limits" not in error["message"]
    assert b.calls == 0
    assert "account_failover" not in traffic.phases()
    assert not pool._accounts[0].is_limited(time.monotonic())
    assert pool._accounts[0].limited_kind is None


@pytest.mark.asyncio
async def test_invalid_request_error_fails_fast_on_collect_path(tmp_path: Path) -> None:
    """Same taxonomy on the non-streaming collect path, entering through
    the real classifier (failure_backend_error) rather than a hand-built
    400 — the two paths must never diverge."""
    import time

    from airelay.backend import failure_backend_error

    settings = _settings(tmp_path)
    upstream_error = {
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
        "message": "Your input exceeds the context window of this model.",
        "param": "input",
    }
    a = FakeBackend("a", fail_with=failure_backend_error(upstream_error))
    b = FakeBackend("b")
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)

    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)

    assert excinfo.value.status_code == 400
    assert json.loads(excinfo.value.detail)["error"] == upstream_error
    assert b.calls == 0
    assert "account_failover" not in traffic.phases()
    assert not pool._accounts[0].is_limited(time.monotonic())


@pytest.mark.asyncio
async def test_unknown_stream_error_defaults_to_failover_with_transient_bench(tmp_path: Path) -> None:
    """Pins the chosen default for AMBIGUOUS stream errors (no limit
    vocabulary, no client vocabulary, no param): treat as upstream-fault —
    short transient bench, fail over, stay retriable. Rationale: genuine
    upstream bad windows arrive with vocabulary the relay has never seen
    and failing fast would trade away the resilience the retry layer
    exists for, while deterministic client rejections carry OpenAI's
    explicit labels (the incident error carried type, code AND param) and
    are caught before any rotation. The bench kind is what keeps this
    default honest: an unknown-error round can no longer masquerade as
    "all accounts at their limits"."""
    import time

    settings = _settings(tmp_path)
    unknown = SSEEvent(
        "error",
        json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "weird_error",
                    "code": "mysterious_upstream_hiccup",
                    "message": "something went wrong",
                    "param": None,
                },
            }
        ),
    )
    a = EventStreamBackend("a", [_created_stream_event(), unknown])
    b = EventStreamBackend("b", [_created_stream_event("resp_b"), _completed_stream_event("b")])
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)

    events = [event async for event in pool.stream_response_events({}, "req", None)]

    assert json.loads(events[-1].data)["response"]["served_by"] == "b"
    assert "account_failover" in traffic.phases()
    assert pool._accounts[0].is_limited(time.monotonic())
    assert pool._accounts[0].limited_kind == "transient"


class FlakyBackend(FakeBackend):
    """FakeBackend that raises a scripted list of failures, then serves."""

    def __init__(self, name: str, failures: list[BackendError]) -> None:
        super().__init__(name)
        self.failures = list(failures)

    async def collect_response(self, payload, request_id, session_id):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"served_by": self.name}


@pytest.mark.asyncio
async def test_retry_repass_recovers_when_the_upstream_window_clears(tmp_path: Path) -> None:
    """Retry × pool integration: pass 1 fails on every account (transient
    upstream window), the retry re-runs the whole failover pass, and the
    request is ultimately served — the client sees only the success."""
    from airelay.retry import RetryPolicy, retry_call

    settings = _settings(tmp_path)
    a = FlakyBackend("a", [BackendError(502, "down"), BackendError(502, "down")])
    b = FlakyBackend("b", [BackendError(502, "down")])
    pool = _pool(settings, [a, b], RecordingTraffic())
    retry_traffic = RecordingTraffic()

    result = await retry_call(
        lambda: pool.collect_response({}, "req", None),
        policy=RetryPolicy(attempts=2, backoff_seconds=(0.0,)),
        request_id="req",
        traffic=retry_traffic,
    )

    assert result["served_by"] == "b"
    # Pass 1 tried and benched both accounts; pass 2 (after backoff) tried
    # the least-limited first and failed over to the one that had recovered.
    assert a.calls == 2 and b.calls == 2
    assert retry_traffic.phases().count("retry_backoff") == 1


@pytest.mark.asyncio
async def test_usage_maxed_account_is_skipped_directly(tmp_path: Path) -> None:
    """An account whose usage probe says the window is spent is benched
    proactively: requests route straight to the available account — no
    upstream hit on the maxed one, no failover round trip."""
    import time

    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)
    pool._bench_from_usage(
        pool._accounts[0],
        {
            "rate_limit_reached_type": "usage_limit_reached",
            "rate_limit": {
                "secondary_window": {"used_percent": 100, "reset_after_seconds": 3600},
            },
        },
        time.monotonic(),
    )

    for request_id in ("req1", "req2"):
        result = await pool.collect_response({}, request_id, None)
        assert result["served_by"] == "b"

    assert a.calls == 0
    assert "account_failover" not in traffic.phases()


@pytest.mark.asyncio
async def test_stream_failure_after_content_passes_through_verbatim(tmp_path: Path) -> None:
    """Once content reached the consumer, failover would be dishonest: the
    failure events flow through unchanged (translated lanes render them
    in-band; the Responses passthrough delivers them verbatim)."""
    import time

    settings = _settings(tmp_path)
    a = EventStreamBackend(
        "a", [_created_stream_event(), _delta_stream_event(), *_failure_stream_events()]
    )
    b = EventStreamBackend("b", [_completed_stream_event("b")])
    pool = _pool(settings, [a, b], RecordingTraffic())

    events = [event async for event in pool.stream_response_events({}, "req", None)]

    assert [event.event for event in events] == [
        "response.created",
        "response.output_text.delta",
        "error",
        "response.failed",
    ]
    assert b.calls == 0
    assert not pool._accounts[0].is_limited(time.monotonic())


@pytest.mark.asyncio
async def test_default_balance_spreads_load_across_accounts(tmp_path: Path) -> None:
    """The out-of-the-box configuration must balance charge across available
    accounts — no config key required. Without a usage signal the balanced
    strategy falls back to fair rotation."""
    settings = _settings(tmp_path)
    assert settings.openai_balance == "balanced"
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    for _ in range(6):
        await pool.collect_response({}, "req", None)
    assert (a.calls, b.calls) == (3, 3)


@pytest.mark.asyncio
async def test_balanced_mode_prefers_the_account_with_most_remaining_quota(tmp_path: Path) -> None:
    """Equal request counts drain a small plan far faster than a large one;
    the balanced default must route to the lowest used_percent of each
    account's longest-horizon (weekly) window so consumption equalizes as a
    percentage of each plan's scarce budget."""
    import time
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    now = time.monotonic()
    pool._accounts[0].used_percent = 25.0
    pool._accounts[0].usage_observed_at = now
    pool._accounts[1].used_percent = 1.0
    pool._accounts[1].usage_observed_at = now
    for _ in range(5):
        await pool.collect_response({}, "req", None)
    assert (a.calls, b.calls) == (0, 5)


@pytest.mark.asyncio
async def test_balanced_mode_ignores_stale_usage_signal(tmp_path: Path) -> None:
    """A capacity signal older than the routing window must not steer
    traffic; the strategy falls back to fair rotation instead of trusting
    day-old percentages."""
    import time
    from airelay.accounts import USAGE_ROUTING_MAX_AGE_SECONDS

    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    stale = time.monotonic() - USAGE_ROUTING_MAX_AGE_SECONDS - 1
    pool._accounts[0].used_percent = 90.0
    pool._accounts[0].usage_observed_at = stale
    pool._accounts[1].used_percent = 1.0
    pool._accounts[1].usage_observed_at = stale
    for _ in range(4):
        await pool.collect_response({}, "req", None)
    assert (a.calls, b.calls) == (2, 2)


@pytest.mark.asyncio
async def test_usage_probes_are_cached_and_coalesced(tmp_path: Path) -> None:
    """Status consumers must not multiply hits on the upstream usage
    endpoint: within the TTL a second probe is served from cache, and a
    forced refresh bypasses it."""
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    b = FakeBackend("b")
    counts = {"a": 0, "b": 0}

    def usage_fn(name):
        async def probe(request_id):
            counts[name] += 1
            return {"rate_limit": {"primary_window": {"used_percent": 10}}}
        return probe

    a.get_subscription_status = usage_fn("a")  # type: ignore[assignment]
    b.get_subscription_status = usage_fn("b")  # type: ignore[assignment]
    pool = _pool(settings, [a, b], RecordingTraffic())

    await pool.subscription_statuses("req1")
    await pool.subscription_statuses("req2")  # within TTL: cache serves it
    assert counts == {"a": 1, "b": 1}

    await pool.subscription_statuses("req3", force=True)
    assert counts == {"a": 2, "b": 2}
    # Probes feed the balanced strategy's capacity signal.
    assert pool._accounts[0].used_percent == 10.0


@pytest.mark.asyncio
async def test_balanced_selection_stays_fair_under_membership_churn(tmp_path: Path) -> None:
    """Least-recently-selected must not starve an account when the healthy
    set changes between calls (the failure mode of a shared modulo counter)."""
    import time
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    # Bench b briefly; traffic flows to a.
    pool._mark_limited(pool._accounts[1], 0.05, "blip")
    await pool.collect_response({}, "req1", None)
    await pool.collect_response({}, "req2", None)
    time.sleep(0.06)  # b recovers
    # b is now the least-recently-selected and must be picked next.
    await pool.collect_response({}, "req3", None)
    assert b.calls == 1


# Usage payload shapes as observed live (2026-07). Which windows a plan
# reports is upstream policy: a Plus account carries its weekly window ALONE
# in the primary slot (no 5h window exists for the plan), while an Enterprise
# account keeps the classic 5h primary plus a weekly secondary. The pool must
# identify windows by duration, never by slot — these fixtures exist to pin
# that down with the real shapes.


def _plus_shaped_usage(weekly_percent: float, *, weekly_reset_at: int = 1785763200) -> dict[str, Any]:
    return {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": weekly_percent,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 551663,
                "reset_at": weekly_reset_at,
            },
        },
    }


def _enterprise_shaped_usage(
    five_hour_percent: float,
    weekly_percent: float,
    *,
    five_hour_reset_at: int = 1785214800,
    weekly_reset_at: int = 1785732000,
) -> dict[str, Any]:
    return {
        "plan_type": "enterprise",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": five_hour_percent,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 4200,
                "reset_at": five_hour_reset_at,
            },
            "secondary_window": {
                "used_percent": weekly_percent,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 518000,
                "reset_at": weekly_reset_at,
            },
        },
    }


def test_probe_records_weekly_signal_from_weekly_only_payload(tmp_path: Path) -> None:
    """A Plus-shaped payload has only the weekly window; its percentage is
    the capacity signal (and 91% used is nowhere near a bench)."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    pool._bench_from_usage(account, _plus_shaped_usage(91), time.monotonic())
    assert account.used_percent == 91.0
    assert not account.is_limited(time.monotonic())


def test_probe_records_weekly_signal_not_the_5h_one(tmp_path: Path) -> None:
    """An Enterprise-shaped payload keeps a 5h window in the primary slot;
    the signal must still be the WEEKLY percentage — slot position does not
    identify a horizon, limit_window_seconds does."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    pool._bench_from_usage(account, _enterprise_shaped_usage(37, 4), time.monotonic())
    assert account.used_percent == 4.0


@pytest.mark.asyncio
async def test_balanced_pool_routes_by_weekly_percent_across_plan_shapes(tmp_path: Path) -> None:
    """A Plus account deep into its weekly budget must not receive traffic
    while an Enterprise account's weekly budget is nearly untouched — even
    though the Enterprise PRIMARY slot (its busy 5h window) reads worse than
    the Plus primary slot. Comparing primary slots did exactly that and
    hammered the small plan's scarce weekly budget."""
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")

    async def plus_usage(request_id):
        return _plus_shaped_usage(91)

    async def enterprise_usage(request_id):
        return _enterprise_shaped_usage(95, 4)

    a.get_subscription_status = plus_usage  # type: ignore[assignment]
    b.get_subscription_status = enterprise_usage  # type: ignore[assignment]
    pool = _pool(settings, [a, b], RecordingTraffic())
    await pool.subscription_statuses("probe", force=True)
    for _ in range(5):
        await pool.collect_response({}, "req", None)
    assert (a.calls, b.calls) == (0, 5)


@pytest.mark.asyncio
async def test_windowless_usage_payload_keeps_signal_none_and_rotates(tmp_path: Path) -> None:
    """A payload carrying no windows at all yields no comparable capacity
    signal: the account keeps signal None and the pool falls back to fair
    rotation instead of trusting a fabricated percentage."""
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")

    async def windowless(request_id):
        return {"plan_type": "plus", "rate_limit": {"allowed": True, "limit_reached": False}}

    a.get_subscription_status = windowless  # type: ignore[assignment]
    b.get_subscription_status = windowless  # type: ignore[assignment]
    pool = _pool(settings, [a, b], RecordingTraffic())
    await pool.subscription_statuses("probe", force=True)
    assert pool._accounts[0].used_percent is None
    assert pool._accounts[1].used_percent is None
    for _ in range(4):
        await pool.collect_response({}, "req", None)
    assert (a.calls, b.calls) == (2, 2)


@pytest.mark.asyncio
async def test_pool_reloads_new_account_without_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_auth(settings.data_dir, "acct-1", "first@example.com")
    pool = OpenAiAccountPool(settings, RecordingTraffic(), slots=discover_slots(settings))
    assert pool.size == 1

    # A second account is enrolled while the pool is live.
    _write_auth(settings.data_dir / ACCOUNTS_DIRNAME / "second", "acct-2", "second@example.com")
    pool._last_reload_check = 0.0  # bypass the throttle for the test
    assert pool.refresh_if_changed() is True
    assert pool.size == 2
    assert {s.email for s in pool.slots()} == {"first@example.com", "second@example.com"}


def _model_backend(name: str, models: list[str]) -> FakeBackend:
    backend = FakeBackend(name)

    async def list_models(request_id):
        return {"models": [{"slug": m} for m in models]}

    backend.list_models = list_models  # type: ignore[assignment]
    return backend


@pytest.mark.asyncio
async def test_list_models_returns_intersection_across_accounts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = _model_backend("a", ["gpt-5.5", "gpt-5-pro", "shared"])
    b = _model_backend("b", ["gpt-5.5", "shared"])
    pool = _pool(settings, [a, b], RecordingTraffic())
    payload = await pool.list_models("req")
    slugs = {item["slug"] for item in payload["models"]}
    assert slugs == {"gpt-5.5", "shared"}  # gpt-5-pro (a-only) excluded


@pytest.mark.asyncio
async def test_request_routes_to_account_supporting_the_model(tmp_path: Path) -> None:
    settings = _settings(tmp_path, openai_balance="ordered")
    # Account a is first but lacks the requested model; b has it.
    a = _model_backend("a", ["gpt-5.5"])
    b = _model_backend("b", ["gpt-5-pro"])
    pool = _pool(settings, [a, b], RecordingTraffic())
    # Prime the per-account model caches.
    await pool.list_models("warm")
    result = await pool.collect_response({"model": "gpt-5-pro"}, "req", None)
    assert result["served_by"] == "b"
    assert a.calls == 0


def test_bench_from_usage_proactively_cools_a_maxed_account(tmp_path: Path) -> None:
    import time
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")
    pool = _pool(settings, [a, b], RecordingTraffic())
    account = pool._accounts[0]
    assert not account.is_limited(time.monotonic())
    pool._bench_from_usage(
        account,
        {
            "rate_limit_reached_type": "usage_limit_reached",
            "rate_limit": {
                "secondary_window": {"used_percent": 100, "reset_after_seconds": 3600},
            },
        },
        time.monotonic(),
    )
    assert account.is_limited(time.monotonic())
    # Cooldown tracks the window reset (~1h), not the default.
    assert 3000 < (account.limited_until - time.monotonic()) <= 3600


def test_bench_from_usage_ignores_healthy_account(tmp_path: Path) -> None:
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    pool._bench_from_usage(
        account,
        {"rate_limit": {"primary_window": {"used_percent": 42, "reset_after_seconds": 100}}},
        time.monotonic(),
    )
    assert not account.is_limited(time.monotonic())


def test_bench_from_usage_reads_explicit_reached_booleans(tmp_path: Path) -> None:
    """The payload's `limit_reached`/`allowed` booleans must bench even when
    the nullable reached-type field is absent and no window shows 100%."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    pool._bench_from_usage(
        account,
        {"rate_limit": {"limit_reached": True, "primary_window": {"used_percent": 99}}},
        time.monotonic(),
    )
    assert account.is_limited(time.monotonic())


def test_bench_from_usage_waits_for_longest_exhausted_window(tmp_path: Path) -> None:
    """With the 5h AND the weekly window exhausted, the bench must cover the
    longest reset — the shorter one would re-bench-flap every window cycle."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    pool._bench_from_usage(
        account,
        {
            "rate_limit": {
                "primary_window": {"used_percent": 100, "reset_after_seconds": 3600},
                "secondary_window": {"used_percent": 100, "reset_after_seconds": 86400},
            },
        },
        time.monotonic(),
    )
    remaining = account.limited_until - time.monotonic()
    assert 80000 < remaining <= 86400


def test_stale_usage_snapshot_cannot_release_a_newer_bench(tmp_path: Path) -> None:
    """A usage probe that started before a 429 benched the account carries no
    evidence about that bench and must not erase it."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    probe_started = time.monotonic()
    pool._mark_limited(account, 3600, "status 429")  # bench lands after the probe began
    pool._bench_from_usage(
        account,
        {"rate_limit": {"primary_window": {"used_percent": 12}}},
        probe_started,
    )
    assert account.is_limited(time.monotonic())
    # A probe that started after the bench is authoritative and releases it.
    pool._bench_from_usage(
        account,
        {"rate_limit": {"primary_window": {"used_percent": 12}}},
        time.monotonic(),
    )
    assert not account.is_limited(time.monotonic())


def test_mark_limited_never_shortens_an_existing_bench(tmp_path: Path) -> None:
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    pool._mark_limited(account, 7200, "usage limit")
    long_until = account.limited_until
    pool._mark_limited(account, 30, "transient 5xx")
    assert account.limited_until == long_until


@pytest.mark.asyncio
async def test_hard_refresh_keeps_bench_when_usage_confirms_the_limit(tmp_path: Path) -> None:
    """The refresh action must never open a window in which live traffic can
    hit a known-exhausted account: releases are evidence-gated."""
    import time
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")

    async def maxed_usage(request_id):
        return {"rate_limit": {"limit_reached": True, "primary_window": {"used_percent": 100, "reset_after_seconds": 900}}}

    a.get_subscription_status = maxed_usage  # type: ignore[assignment]
    pool = _pool(settings, [a, b], RecordingTraffic())
    pool._mark_limited(pool._accounts[0], 3600, "status 429")

    await pool.hard_refresh("req")

    assert pool._accounts[0].is_limited(time.monotonic())
    assert not pool._accounts[1].is_limited(time.monotonic())


@pytest.mark.asyncio
async def test_hard_refresh_keeps_bench_when_probe_fails(tmp_path: Path) -> None:
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")

    async def broken_usage(request_id):
        raise BackendError(502, "usage endpoint down")

    a.get_subscription_status = broken_usage  # type: ignore[assignment]
    pool = _pool(settings, [a], RecordingTraffic())
    pool._mark_limited(pool._accounts[0], 3600, "status 429")

    await pool.hard_refresh("req")

    assert pool._accounts[0].is_limited(time.monotonic())


@pytest.mark.asyncio
async def test_warm_start_benches_exhausted_accounts_and_learns_models(tmp_path: Path) -> None:
    """A freshly launched relay must balance correctly from the first
    request: accounts already at their limit are benched by the startup
    probe (no wasted 429), and per-account model catalogs are filled so
    model-aware routing works immediately."""
    import time
    settings = _settings(tmp_path)
    a = _model_backend("a", ["gpt-5.5"])
    b = _model_backend("b", ["gpt-5.5", "gpt-5-pro"])

    async def maxed_usage(request_id):
        return {"rate_limit": {"limit_reached": True, "primary_window": {"used_percent": 100, "reset_after_seconds": 1800}}}

    a.get_subscription_status = maxed_usage  # type: ignore[assignment]
    traffic = RecordingTraffic()
    pool = _pool(settings, [a, b], traffic)

    await pool.warm_start()

    assert pool._accounts[0].is_limited(time.monotonic())
    assert not pool._accounts[1].is_limited(time.monotonic())
    assert pool._accounts[0].models == {"gpt-5.5"}
    assert pool._accounts[1].models == {"gpt-5.5", "gpt-5-pro"}
    assert "account_pool_warmed" in traffic.phases()
    # Live traffic goes straight to the account with capacity.
    result = await pool.collect_response({"model": "gpt-5.5"}, "req", None)
    assert result["served_by"] == "b"


@pytest.mark.asyncio
async def test_warm_start_survives_probe_failures(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a, b = FakeBackend("a"), FakeBackend("b")

    async def broken(request_id):
        raise BackendError(502, "usage endpoint down")

    a.get_subscription_status = broken  # type: ignore[assignment]
    a.list_models = broken  # type: ignore[assignment]
    pool = _pool(settings, [a, b], RecordingTraffic())

    await pool.warm_start()  # must not raise

    result = await pool.collect_response({}, "req", None)
    assert result["served_by"] in {"a", "b"}


@pytest.mark.asyncio
async def test_hard_refresh_releases_bench_when_usage_shows_capacity(tmp_path: Path) -> None:
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")

    async def healthy_usage(request_id):
        return {"rate_limit": {"primary_window": {"used_percent": 12}}}

    a.get_subscription_status = healthy_usage  # type: ignore[assignment]
    pool = _pool(settings, [a], RecordingTraffic())
    pool._mark_limited(pool._accounts[0], 3600, "status 429")

    await pool.hard_refresh("req")

    assert not pool._accounts[0].is_limited(time.monotonic())


@pytest.mark.asyncio
async def test_window_token_tally_tracks_models_and_survives_reload(tmp_path: Path) -> None:
    """The Accounts card's "more" hover shows per-model in/out tokens for
    the current window: fed by non-stream responses, scoped to the window
    identity, cleared on rollover, persisted across pool rebuilds."""
    settings = _settings(tmp_path)
    a = FakeBackend("a")

    async def respond(payload, request_id, session_id):
        a.calls += 1
        return {
            "served_by": "a",
            "model": "gpt-test",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 50,
                "input_tokens_details": {"cached_tokens": 400},
            },
        }

    a.collect_response = respond  # type: ignore[assignment]
    pool = _pool(settings, [a], RecordingTraffic())
    account_id = pool._accounts[0].slot.account_id

    for _ in range(3):
        await pool.collect_response({}, "req", None)

    snap = pool._tally.snapshot(account_id)
    assert snap is not None
    assert snap["models"][0]["model"] == "gpt-test"
    assert snap["models"][0]["input_tokens"] == 3000
    assert snap["models"][0]["output_tokens"] == 150
    assert snap["totals"]["input_tokens"] == 3000
    assert snap["totals"]["cached_input_tokens"] == 1200

    # The breakdown reaches the status payload the desktop reads.
    statuses = pool.account_statuses()
    assert statuses[0]["window_tokens"]["totals"]["output_tokens"] == 150

    # Same window anchor: tally persists; new anchor: window rolled, cleared.
    pool._tally.set_window(account_id, 1783800000)
    assert pool._tally.snapshot(account_id) is not None
    pool._tally.set_window(account_id, 1783820000)
    assert pool._tally.snapshot(account_id) is None

    # Persistence: a new tally on the same path reloads saved state.
    await pool.collect_response({}, "req", None)
    pool._tally.save()
    from airelay.usage_tally import WindowTokenTally

    reloaded = WindowTokenTally(settings.data_dir / "openai-window-tokens.json")
    snap = reloaded.snapshot(account_id)
    assert snap is not None and snap["totals"]["input_tokens"] == 1000


@pytest.mark.asyncio
async def test_window_token_tally_captures_streamed_usage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a")

    async def stream(payload, request_id, session_id):
        yield SSEEvent(
            event="response.completed",
            data=json.dumps(
                {
                    "response": {
                        "model": "gpt-stream",
                        "usage": {"input_tokens": 200, "output_tokens": 20},
                    }
                }
            ),
        )

    a.stream_response_events = stream  # type: ignore[assignment]
    pool = _pool(settings, [a], RecordingTraffic())
    async for _ in pool.stream_response_events({}, "req", None):
        pass
    snap = pool._tally.snapshot(pool._accounts[0].slot.account_id)
    assert snap is not None
    assert snap["models"][0]["model"] == "gpt-stream"
    assert snap["totals"]["input_tokens"] == 200


def test_tally_survives_5h_roll_and_clears_on_weekly_roll(tmp_path: Path) -> None:
    """The per-model breakdown is anchored to the longest (weekly) window:
    the 5h bucket rolling every few hours must no longer wipe it (that left
    the desktop "more" panel permanently empty on 5h+weekly plans), a probe
    without windows must not touch it, and a weekly rollover still clears
    it because the numbers no longer describe the shown percentage."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    account_id = account.slot.account_id

    pool._bench_from_usage(
        account,
        _enterprise_shaped_usage(80, 4, five_hour_reset_at=1785214800, weekly_reset_at=1785732000),
        time.monotonic(),
    )
    pool._tally.record(account_id, "gpt-test", {"input_tokens": 100, "output_tokens": 10})
    assert pool._tally.snapshot(account_id) is not None

    # The 5h bucket rolls (new primary anchor); the weekly anchor is
    # unchanged, so the breakdown survives.
    pool._bench_from_usage(
        account,
        _enterprise_shaped_usage(1, 5, five_hour_reset_at=1785232800, weekly_reset_at=1785732000),
        time.monotonic(),
    )
    snap = pool._tally.snapshot(account_id)
    assert snap is not None and snap["totals"]["input_tokens"] == 100

    # A degenerate probe with no windows keeps the current tally.
    pool._bench_from_usage(account, {"rate_limit": {"allowed": True}}, time.monotonic())
    assert pool._tally.snapshot(account_id) is not None

    # The weekly window itself rolls: cleared.
    pool._bench_from_usage(
        account,
        _enterprise_shaped_usage(0, 0, five_hour_reset_at=1785250800, weekly_reset_at=1786336800),
        time.monotonic(),
    )
    assert pool._tally.snapshot(account_id) is None


def test_tally_loads_state_written_before_window_seconds_existed(tmp_path: Path) -> None:
    """STATE_VERSION deliberately stayed at 1: a pre-upgrade state file (no
    window_seconds) must load with its tokens intact and an unlabeled
    snapshot; the next probe fills the metadata in place when the anchor
    still matches (Plus-shaped accounts were already weekly-anchored)."""
    from airelay.usage_tally import WindowTokenTally

    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "acct-1": {
                        "reset_at": 1785763200,
                        "models": {"gpt-test": {"input": 5, "output": 2, "cached": 0}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    tally = WindowTokenTally(path)
    snap = tally.snapshot("acct-1")
    assert snap is not None and snap["totals"]["input_tokens"] == 5
    assert snap["window_label"] is None and snap["window_seconds"] is None
    # Same anchor re-probed, now with the duration: metadata only, kept.
    tally.set_window("acct-1", 1785763200, 604800)
    snap = tally.snapshot("acct-1")
    assert snap is not None
    assert snap["window_label"] == "weekly"
    assert snap["totals"]["input_tokens"] == 5


def test_tally_snapshot_labels_the_window(tmp_path: Path) -> None:
    """The "more" panel title derives from the tally payload, so the
    snapshot must say WHICH window the numbers cover instead of a fixed 5h
    claim — and keep every field existing consumers read."""
    import time
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    pool = _pool(settings, [a], RecordingTraffic())
    account = pool._accounts[0]
    account_id = account.slot.account_id
    pool._bench_from_usage(account, _plus_shaped_usage(91), time.monotonic())
    pool._tally.record(account_id, "gpt-test", {"input_tokens": 10, "output_tokens": 1})
    snap = pool._tally.snapshot(account_id)
    assert snap is not None
    assert snap["window_label"] == "weekly"
    assert snap["window_seconds"] == 604800
    assert snap["scope"] == "current_usage_window_via_this_relay"
    assert snap["window_reset_at"] == 1785763200
    assert snap["models"][0]["model"] == "gpt-test"
    assert snap["totals"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_single_account_pool_matches_legacy_behavior(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a")
    traffic = RecordingTraffic()
    pool = _pool(settings, [a], traffic)
    result = await pool.collect_response({}, "req", None)
    assert result["served_by"] == "a"
    # No account_selected noise for single-account installs.
    assert "account_selected" not in traffic.phases()
