from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from airelay import auth as auth_module
from airelay.auth import (
    AuthManager,
    BROWSER_CALLBACK_PORT,
    BROWSER_LOGIN_SCOPE,
    LoginCallbackServer,
    _browser_redirect_uri,
    _build_browser_authorize_url,
    _encode_query,
    _generate_state,
)


def test_browser_redirect_uri_matches_codex_localhost_shape() -> None:
    assert _browser_redirect_uri(BROWSER_CALLBACK_PORT) == "http://localhost:1455/auth/callback"


def test_generate_state_matches_codex_length_shape() -> None:
    state = _generate_state()
    assert len(state) == 43
    assert "+" not in state
    assert "/" not in state


def test_encode_query_uses_percent_encoding_not_plus_for_spaces() -> None:
    encoded = _encode_query({"scope": BROWSER_LOGIN_SCOPE})
    assert "openid%20profile%20email%20offline_access" in encoded
    assert "+" not in encoded


def test_browser_authorize_url_matches_current_codex_scope_shape() -> None:
    url = _build_browser_authorize_url(
        issuer_base_url="https://auth.openai.com",
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        redirect_uri="http://localhost:1455/auth/callback",
        code_challenge="challenge",
        state="state",
        workspace_id="ws_123",
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert query["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert query["scope"] == [BROWSER_LOGIN_SCOPE]
    assert query["allowed_workspace_id"] == ["ws_123"]
    assert "scope=openid%20profile%20email%20offline_access" in url


def test_login_callback_server_accepts_localhost_callback() -> None:
    try:
        server = LoginCallbackServer(port=0)
    except OSError as exc:
        pytest.skip(f"localhost callback server cannot bind in this environment: {exc}")
    server.start()
    try:
        with urlopen(
            f"http://localhost:{server.port}/auth/callback?code=test-code&state=test-state",
            timeout=3,
        ) as response:
            body = response.read().decode("utf-8")
        assert "Login completed" in body
        code, error = server.wait(1)
        assert error is None
        assert code == "test-code"
        assert server.state == "test-state"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_browser_login_uses_localhost_redirect_uri(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCallbackServer:
        def __init__(self) -> None:
            self.port = 58325
            self.state = "test-state"

        def start(self) -> None:
            return None

        def wait(self, timeout_seconds: float) -> tuple[str | None, str | None]:
            del timeout_seconds
            return "auth-code", None

        def close(self) -> None:
            return None

    async def fake_exchange_code_for_tokens(
        self,
        client_id: str,
        redirect_uri: str,
        code: str,
        code_verifier: str,
    ) -> dict[str, str]:
        del self, client_id, code, code_verifier
        captured["redirect_uri"] = redirect_uri
        return {
            "id_token": "id-token",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }

    monkeypatch.setattr(auth_module, "LoginCallbackServer", FakeCallbackServer)
    monkeypatch.setattr(auth_module, "_generate_state", lambda: "test-state")
    monkeypatch.setattr(
        AuthManager,
        "_exchange_code_for_tokens",
        fake_exchange_code_for_tokens,
    )

    manager = AuthManager(tmp_path / "airelay", "file", "https://auth.openai.com")
    record = await manager.browser_login(
        client_id="app_EMoamEEZ73f0CkXaXp7hrann",
        open_browser=False,
        timeout_seconds=1,
    )

    assert captured["redirect_uri"] == "http://localhost:58325/auth/callback"
    assert record.access_token == "access-token"
