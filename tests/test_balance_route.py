from __future__ import annotations

from fastapi.testclient import TestClient

from airelay.app import create_app
from airelay.config import Settings


def _settings(root):
    return Settings(
        data_dir=root / "data", logs_dir=root / "logs", config_path=root / "config.toml",
        bearer_token_file=root / "token", bearer_token="test-token",
        enable_openai_provider=False, enable_claude=False,
    )


def test_balance_route_requires_auth_validates_and_switches_live(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/relay/balance").status_code == 401
        assert client.put("/v1/relay/balance", json={"openai": "ping_pong"}).status_code == 401
        client.headers["Authorization"] = "Bearer test-token"

        shown = client.get("/v1/relay/balance").json()
        assert shown["openai"] == "balanced" and shown["claude"] == "balanced"
        assert "weighted" in shown["strategies"] and shown["runtime_only"] is True

        response = client.put("/v1/relay/balance", json={"openai": "Ping-Pong"})
        assert response.status_code == 200
        assert response.json()["openai"] == "ping_pong"
        # The pools read this very object on every selection: no restart.
        assert settings.openai_balance == "ping_pong"
        assert settings.claude_balance == "balanced"

        assert client.put("/v1/relay/balance", json={"claude": "weighted"}).json()["claude"] == "weighted"
        assert settings.claude_balance == "weighted"

        assert client.put("/v1/relay/balance", json={"openai": "fastest"}).status_code == 422
        assert client.put("/v1/relay/balance", json={}).status_code == 422
        assert client.put("/v1/relay/balance", json={"gemini": "ordered"}).status_code == 422
        assert settings.openai_balance == "ping_pong"
