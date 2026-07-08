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
async def test_all_accounts_limited_reports_actionable_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    a = FakeBackend("a", fail_with=_usage_limit_error())
    b = FakeBackend("b", fail_with=_usage_limit_error())
    pool = _pool(settings, [a, b], RecordingTraffic())
    with pytest.raises(BackendError) as excinfo:
        await pool.collect_response({}, "req", None)
    assert "2 OpenAI accounts" in excinfo.value.detail


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
    )
    assert not account.is_limited(time.monotonic())


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
