"""Device-code (headless) login flow.

The flow must work with no browser and no localhost callback: get a user
code, poll until approval, exchange the code. These tests mock the upstream
via httpx transports; the logic must hold for any poll cadence or payload
shape the upstream produces.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import airelay.auth as auth_module
from airelay.auth import AuthenticationError, AuthManager


# Captured before any monkeypatching: auth.py shares this module object.
_RealAsyncClient = httpx.AsyncClient


def _mock_client_factory(handler):
    """Replaces auth.httpx.AsyncClient so device_login talks to a mock."""

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=httpx.MockTransport(handler))

    return factory


async def _fake_exchange(self, client_id, redirect_uri, code, code_verifier):
    del self, client_id, redirect_uri, code, code_verifier
    return {
        "id_token": "id-token",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }


@pytest.mark.asyncio
async def test_device_login_happy_path_with_pending_polls(tmp_path, monkeypatch) -> None:
    polls = {"count": 0}
    prompts: list[tuple[str, str]] = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/usercode"):
            return httpx.Response(
                200,
                json={"user_code": "ABCD-1234", "device_auth_id": "dev-1", "interval": 0},
            )
        if request.url.path.endswith("/deviceauth/token"):
            polls["count"] += 1
            if polls["count"] < 3:
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={"authorization_code": "auth-code", "code_verifier": "verifier"},
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _mock_client_factory(handler))
    monkeypatch.setattr(AuthManager, "_exchange_code_for_tokens", _fake_exchange)

    manager = AuthManager(tmp_path / "data", "file", "https://auth.openai.com")
    record = await manager.device_login(
        client_id="client",
        timeout_seconds=30,
        on_device_code=lambda url, code: prompts.append((url, code)),
        on_waiting=waits.append,
    )

    assert record.access_token == "access-token"
    assert polls["count"] == 3
    assert prompts == [("https://auth.openai.com/codex/device", "ABCD-1234")]
    assert len(waits) == 2  # one per pending poll


@pytest.mark.asyncio
async def test_device_login_accepts_usercode_key_alias(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/usercode"):
            return httpx.Response(
                200, json={"usercode": "WXYZ", "device_auth_id": "dev-2", "interval": 0}
            )
        return httpx.Response(
            200, json={"authorization_code": "auth-code", "code_verifier": "verifier"}
        )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _mock_client_factory(handler))
    monkeypatch.setattr(AuthManager, "_exchange_code_for_tokens", _fake_exchange)

    manager = AuthManager(tmp_path / "data", "file", "https://auth.openai.com")
    record = await manager.device_login(client_id="client", timeout_seconds=30)
    assert record.access_token == "access-token"


@pytest.mark.asyncio
async def test_device_login_not_enabled_is_a_clear_error(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _mock_client_factory(handler))
    manager = AuthManager(tmp_path / "data", "file", "https://auth.openai.com")
    with pytest.raises(AuthenticationError, match="not enabled"):
        await manager.device_login(client_id="client", timeout_seconds=5)


@pytest.mark.asyncio
async def test_device_login_timeout_reports_configured_duration(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/usercode"):
            return httpx.Response(
                200, json={"user_code": "CODE", "device_auth_id": "dev-3", "interval": 0}
            )
        return httpx.Response(403)  # never approved

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _mock_client_factory(handler))
    manager = AuthManager(tmp_path / "data", "file", "https://auth.openai.com")
    with pytest.raises(AuthenticationError) as excinfo:
        await manager.device_login(client_id="client", timeout_seconds=0.05)
    message = str(excinfo.value)
    assert "not approved" in message
    assert "15 minutes" not in message  # honest about the configured timeout


@pytest.mark.asyncio
async def test_device_login_unexpected_poll_status_is_readable(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/usercode"):
            return httpx.Response(
                200, json={"user_code": "CODE", "device_auth_id": "dev-4", "interval": 0}
            )
        return httpx.Response(429)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _mock_client_factory(handler))
    manager = AuthManager(tmp_path / "data", "file", "https://auth.openai.com")
    with pytest.raises(AuthenticationError, match="status 429"):
        await manager.device_login(client_id="client", timeout_seconds=5)
