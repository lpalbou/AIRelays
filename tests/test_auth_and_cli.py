from __future__ import annotations

import json
from pathlib import Path

import pytest

from airelay.auth import AuthManager, AuthStorage, _compute_store_key
from airelay.cli import _base_settings, build_parser


def _write_auth_payload(storage_root: Path, payload: dict[str, object]) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    (storage_root / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


def _clear_airelay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AIRELAYS_CONFIG",
        "AIRELAYS_DATA_DIR",
        "AIRELAYS_LOGS_DIR",
        "AIRELAYS_BEARER_TOKEN",
        "AIRELAYS_BEARER_TOKEN_FILE",
        "AIRELAYS_PORT",
        "AIRELAYS_AUTH_STORAGE",
        "AIRELAYS_BROWSER_OPEN",
        "AIRELAYS_REQUIRE_BEARER_AUTH",
        "AIRELAYS_AUTO_GENERATE_BEARER_TOKEN",
        "AIRELAY_CONFIG",
        "AIRELAY_CODEX_HOME",
        "AIRELAY_DATA_DIR",
        "AIRELAY_LOGS_DIR",
        "AIRELAY_BEARER_TOKEN",
        "AIRELAY_BEARER_TOKEN_FILE",
        "CODEX_HOME",
        "OPENAI_ENDPOINT_CONFIG",
        "OPENAI_ENDPOINT_CODEX_HOME",
        "OPENAI_ENDPOINT_DATA_DIR",
        "OPENAI_ENDPOINT_LOGS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_auth_storage_auto_falls_back_to_file_when_keyring_fails(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "airelay"
    payload = {"tokens": {"access_token": "access", "refresh_token": "refresh"}}
    _write_auth_payload(storage_root, payload)
    storage = AuthStorage(storage_root, "auto")

    def _raise_runtime_error() -> dict[str, object]:
        raise RuntimeError("no backend")

    monkeypatch.setattr(storage, "_load_keyring", _raise_runtime_error)

    assert storage.load() == payload


def test_auth_storage_auto_migrates_legacy_keyring_payload(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "airelay"
    payload = {"tokens": {"access_token": "access", "refresh_token": "refresh"}}
    serialized = json.dumps(payload)
    calls: list[tuple[str, str, str | None]] = []
    username = _compute_store_key(storage_root)

    def fake_get_password(service: str, username: str) -> str | None:
        calls.append(("get", service, username))
        if service == "AIRelays Auth":
            return None
        if service == "AIRelay Auth":
            return serialized
        return None

    def fake_set_password(service: str, username: str, value: str) -> None:
        calls.append(("set", service, username))
        assert service == "AIRelays Auth"
        assert value == serialized

    def fake_delete_password(service: str, username: str) -> None:
        calls.append(("delete", service, username))
        assert service == "AIRelay Auth"

    monkeypatch.setattr("airelay.auth.keyring.get_password", fake_get_password)
    monkeypatch.setattr("airelay.auth.keyring.set_password", fake_set_password)
    monkeypatch.setattr("airelay.auth.keyring.delete_password", fake_delete_password)

    storage = AuthStorage(storage_root, "auto")

    assert storage.load() == payload
    assert ("set", "AIRelays Auth", username) in calls
    assert ("delete", "AIRelay Auth", username) in calls


def test_auth_manager_status_without_tokens_is_not_ready(tmp_path) -> None:
    storage_root = tmp_path / "airelay"
    _write_auth_payload(storage_root, {})

    manager = AuthManager(storage_root, "file", "https://auth.openai.com")
    status = manager.status()

    assert status["authenticated"] is False
    assert status["credentials_present"] is False
    assert status["account_bound"] is False
    assert status["ready_for_requests"] is False
    assert status["email"] is None
    assert status["plan_type"] is None


@pytest.mark.asyncio
async def test_ensure_fresh_tokens_refreshes_when_access_token_is_missing(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "airelay"
    _write_auth_payload(
        storage_root,
        {
            "tokens": {"refresh_token": "refresh-only"},
            "bound_account_id": "acct_123",
            "last_refresh": "2099-01-01T00:00:00+00:00",
        },
    )

    manager = AuthManager(storage_root, "file", "https://auth.openai.com")

    async def fake_refresh_tokens(force: bool = True):
        assert force is True
        return manager.load().__class__(
            {
                "tokens": {
                    "access_token": "new-access",
                    "refresh_token": "refresh-only",
                    "account_id": "acct_123",
                },
                "bound_account_id": "acct_123",
                "last_refresh": "2099-01-01T00:00:00+00:00",
            }
        )

    monkeypatch.setattr(manager, "refresh_tokens", fake_refresh_tokens)

    record = await manager.ensure_fresh_tokens()

    assert record.access_token == "new-access"


@pytest.mark.asyncio
async def test_refresh_tokens_uses_configured_client_id(tmp_path, monkeypatch) -> None:
    storage_root = tmp_path / "airelay"
    _write_auth_payload(
        storage_root,
        {
            "tokens": {
                "id_token": "header.payload.signature",
                "access_token": "access",
                "refresh_token": "refresh",
                "account_id": "acct_123",
            },
            "bound_account_id": "acct_123",
            "last_refresh": "2000-01-01T00:00:00+00:00",
        },
    )
    manager = AuthManager(
        storage_root,
        "file",
        "https://auth.openai.com",
        client_id="app_custom",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {
                "id_token": "header.payload.signature",
                "access_token": "new-access",
                "refresh_token": "new-refresh",
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

        async def post(self, url: str, json: dict[str, object], headers: dict[str, str]):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("airelay.auth.httpx.AsyncClient", lambda timeout=30.0: FakeClient())

    await manager.refresh_tokens(force=True)

    assert captured["json"]["client_id"] == "app_custom"


def test_cli_data_dir_override_retargets_default_paths(tmp_path, monkeypatch) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        ["status", "--config", str(tmp_path / "config.toml"), "--data-dir", str(tmp_path / "state")]
    )

    settings = _base_settings(args)

    assert settings.data_dir == tmp_path / "state"
    assert settings.logs_dir == tmp_path / "state" / "logs"
    assert settings.bearer_token_file == tmp_path / "state" / "relay-token"


def test_cli_init_generates_token_once_and_hides_existing_token(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"

    args = parser.parse_args(
        ["init", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    first = capsys.readouterr().out

    assert "AIRelays Init" in first
    assert "Client Setup" in first
    assert "Authorization: Bearer <AIRelays token>" in first
    assert str(data_dir / "relay-token") in first

    args = parser.parse_args(
        ["init", "--json", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    second = json.loads(capsys.readouterr().out)

    assert second["config_created"] is False
    assert second["bearer_token_created"] is False
    assert second["relay_token"] is None
    assert second["client"]["reveal_token_command"] == "airelays token show"


def test_cli_init_no_auth_writes_disabled_config_and_skips_token(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"

    args = parser.parse_args(
        ["init", "--no-auth", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    output = capsys.readouterr().out

    assert "AIRelays Init" in output
    assert "Bearer auth" in output
    assert "disabled" in output
    assert "optional placeholder only" in output
    assert not (data_dir / "relay-token").exists()
    assert "require_bearer_auth = false" in config_path.read_text(encoding="utf-8")


def test_cli_status_defaults_to_human_output_and_supports_json(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    (data_dir / "relay-token").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "relay-token").write_text("token\n", encoding="utf-8")

    args = parser.parse_args(
        ["status", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    human = capsys.readouterr().out

    assert "AIRelays Status" in human
    assert "OpenAI Session" in human
    assert "Client Setup" in human
    assert "airelays login" in human

    args = parser.parse_args(
        ["status", "--json", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    machine = json.loads(capsys.readouterr().out)

    assert machine["relay"]["bearer_token_present"] is True
    assert machine["auth"]["ready_for_requests"] is False
    assert machine["next_steps"] == ["airelays login"]


def test_cli_status_prefers_serve_when_claude_is_ready(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    (data_dir / "relay-token").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "relay-token").write_text("token\n", encoding="utf-8")
    config_path.write_text(
        """
[providers.openai]
enabled = true

[providers.claude]
enabled = true
""".strip(),
        encoding="utf-8",
    )

    class _FakeRegistry:
        @staticmethod
        def provider_statuses() -> dict[str, object]:
            return {
                "openai": {
                    "enabled": True,
                    "ready_for_requests": False,
                },
                "claude": {
                    "enabled": True,
                    "ready_for_requests": True,
                    "experimental": True,
                },
            }

    monkeypatch.setattr("airelay.cli._provider_registry", lambda settings, manager: _FakeRegistry())

    args = parser.parse_args(
        ["status", "--json", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    machine = json.loads(capsys.readouterr().out)

    assert machine["next_steps"] == ["airelays serve --host 127.0.0.1 --port 8080"]


def test_cli_init_claude_only_skips_openai_login_hint(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"

    args = parser.parse_args(
        [
            "init",
            "--json",
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
        ]
    )
    monkeypatch.setenv("AIRELAYS_ENABLE_OPENAI", "false")
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL", "true")
    args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert "airelays login" not in payload["next_steps"]
    assert "claude auth login --claudeai" in payload["next_steps"]


def test_cli_token_show_displays_existing_token_and_supports_json(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    token_path = data_dir / "relay-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("token-value\n", encoding="utf-8")

    args = parser.parse_args(
        ["token", "show", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    human = capsys.readouterr().out

    assert "AIRelays Token" in human
    assert "token-value" in human
    assert "Reveal token" not in human

    args = parser.parse_args(
        ["token", "show", "--json", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    machine = json.loads(capsys.readouterr().out)

    assert machine["bearer_token_present"] is True
    assert machine["relay_token"] == "token-value"
    assert machine["client"]["reveal_token_command"] == "airelays token show"


def test_cli_serve_requires_explicit_token_setup_by_default(tmp_path, monkeypatch) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        ["serve", "--config", str(tmp_path / "config.toml"), "--data-dir", str(tmp_path / "state")]
    )

    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert "Run `airelays init`" in str(excinfo.value)


def test_cli_serve_prints_client_auth_guidance(tmp_path, monkeypatch, capsys) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    (data_dir / "relay-token").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "relay-token").write_text("token\n", encoding="utf-8")
    args = parser.parse_args(
        ["serve", "--config", str(config_path), "--data-dir", str(data_dir), "--port", "8090"]
    )

    captured: dict[str, object] = {}

    def fake_run(app, host, port, log_level):  # type: ignore[no-untyped-def]
        del app
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr("airelay.cli.uvicorn.run", fake_run)

    args.func(args)
    output = capsys.readouterr().out

    assert "AIRelays Server" in output
    assert "http://127.0.0.1:8090/v1" in output
    assert "Authorization: Bearer <AIRelays token>" in output
    assert "airelays token show" in output
    assert "ChatGPT login" in output
    assert "airelays login" in output
    assert captured["port"] == 8090


def test_cli_serve_no_auth_starts_open_mode_without_token(tmp_path, monkeypatch, capsys) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "serve",
            "--no-auth",
            "--config",
            str(tmp_path / "config.toml"),
            "--data-dir",
            str(tmp_path / "state"),
            "--port",
            "8090",
        ]
    )

    captured: dict[str, object] = {}

    def fake_run(app, host, port, log_level):  # type: ignore[no-untyped-def]
        del app
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr("airelay.cli.uvicorn.run", fake_run)

    args.func(args)
    output = capsys.readouterr().out

    assert "AIRelays Server" in output
    assert "disabled" in output
    assert "optional placeholder only" in output
    assert "ChatGPT login" in output
    assert "airelays login" in output
    assert captured["port"] == 8090


def test_cli_serve_rejects_no_auth_when_claude_experimental_is_enabled(
    tmp_path, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[providers.claude]
enabled = true
""".strip(),
        encoding="utf-8",
    )
    args = parser.parse_args(
        [
            "serve",
            "--no-auth",
            "--config",
            str(config_path),
            "--data-dir",
            str(tmp_path / "state"),
        ]
    )

    with pytest.raises(SystemExit, match="requires AIRelays bearer auth"):
        args.func(args)


def test_cli_init_rejects_no_auth_when_claude_experimental_is_enabled(
    tmp_path, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[providers.claude]
enabled = true
""".strip(),
        encoding="utf-8",
    )
    args = parser.parse_args(
        [
            "init",
            "--no-auth",
            "--config",
            str(config_path),
            "--data-dir",
            str(tmp_path / "state"),
        ]
    )

    with pytest.raises(SystemExit, match="requires AIRelays bearer auth"):
        args.func(args)
