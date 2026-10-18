from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from airelay.accounts import keyring_era_sign_ins
from airelay.auth import AuthManager, AuthStorage
from airelay.config import Settings
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


@pytest.mark.parametrize("mode", ["auto", "file", "keyring"])
def test_auth_storage_is_a_private_file_in_every_mode(tmp_path, mode) -> None:
    """ADR 0008: one storage everywhere; legacy modes never reach a keyring."""
    import os
    import sys

    storage_root = tmp_path / "airelay" / "accounts" / "one"
    payload = {"tokens": {"access_token": "access", "refresh_token": "refresh"}}
    storage = AuthStorage(storage_root, mode)
    storage.save(payload)
    assert storage.mode == "file"
    assert storage.load() == payload
    assert json.loads(storage.auth_path.read_text(encoding="utf-8")) == payload
    if sys.platform != "win32":
        assert storage.auth_path.stat().st_mode & 0o777 == 0o600
        assert storage_root.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in storage_root.iterdir() if p.name.startswith(".")] == []
    assert storage.delete() and storage.load() is None
    assert not os.path.exists(storage.auth_path)


def test_concurrent_refreshes_rotate_the_token_once(tmp_path, monkeypatch) -> None:
    """Two processes (here: two managers) refreshing one sign-in at once must
    not both spend the rotating refresh token: the second reuses the first's."""
    storage_root = tmp_path / "airelay"
    _write_auth_payload(storage_root, {"tokens": {"id_token": "id-token", "access_token": "old", "refresh_token": "r1"}})
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "new", "refresh_token": "r2"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            calls.append(json["refresh_token"])
            await asyncio.sleep(0.3)
            return FakeResponse()

    monkeypatch.setattr("airelay.auth.httpx.AsyncClient", FakeClient)
    first = AuthManager(storage_root, "file", "https://auth.openai.com")
    second = AuthManager(storage_root, "file", "https://auth.openai.com")

    async def both():
        return await asyncio.gather(first.refresh_tokens(force=True), second.refresh_tokens(force=True))

    records = asyncio.run(both())
    assert calls == ["r1"]
    assert {record.refresh_token for record in records} == {"r2"}
    assert (storage_root / "auth.json.lock").exists()


def test_keyring_era_sign_ins_are_detected_from_files_only(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path, logs_dir=tmp_path / "logs")
    assert keyring_era_sign_ins(settings) == []
    (tmp_path / "accounts" / "keychain-account").mkdir(parents=True)
    _write_auth_payload(tmp_path / "accounts" / "file-account", {"tokens": {"access_token": "a"}})
    assert keyring_era_sign_ins(settings) == ["keychain-account"]
    settings.legacy_auth_storage = "auto"
    assert keyring_era_sign_ins(settings) == ["default", "keychain-account"]
    # The tray rewrites `storage`; OpenAI usage history still marks the default.
    settings.legacy_auth_storage = None
    (tmp_path / "openai-window-tokens.json").write_text("{}", encoding="utf-8")
    assert keyring_era_sign_ins(settings) == ["default", "keychain-account"]
    _write_auth_payload(tmp_path, {"tokens": {"access_token": "a"}})
    assert keyring_era_sign_ins(settings) == ["keychain-account"]


def test_config_toml_round_trips_windows_paths_and_reports_legacy_storage(tmp_path, monkeypatch) -> None:
    import tomllib
    from pathlib import PureWindowsPath

    settings = Settings(
        data_dir=PureWindowsPath(r"C:\Users\someone\.airelays"),
        logs_dir=PureWindowsPath(r"C:\Users\someone\.airelays\logs"),
        claude_bin=r'C:\Tools\say "claude".exe',
    )
    parsed = tomllib.loads(settings.render_config_toml())
    assert parsed["paths"]["data_dir"] == r"C:\Users\someone\.airelays"
    assert parsed["providers"]["claude"]["bin"] == r'C:\Tools\say "claude".exe'
    assert parsed["auth"]["storage"] == "file"

    _clear_airelay_env(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('[auth]\nstorage = "keyring"\n', encoding="utf-8")
    loaded = Settings.from_sources(config)
    assert loaded.auth_storage_mode == "file"
    assert loaded.legacy_auth_storage == "keyring"


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

    assert re.search(r"AIRelays \S+ Init", first)
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

    assert re.search(r"AIRelays \S+ Init", output)
    assert "Bearer auth" in output
    assert "disabled" in output
    assert "optional placeholder only" in output
    assert not (data_dir / "relay-token").exists()
    assert "require_bearer_auth = false" in config_path.read_text(encoding="utf-8")


def test_cli_status_defaults_to_human_output_and_supports_json(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    # The login hint depends on the machine (desktop → browser flow,
    # headless → device flow); pin it so the assertion is deterministic
    # on displayless CI runners.
    monkeypatch.setattr("airelay.cli._is_headless_environment", lambda: False)
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

    # The title carries the running version (e.g. "AIRelays 0.6.0 Status").
    assert re.search(r"AIRelays \S+ Status", human)
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


def test_cli_doctor_reports_actionable_failures_for_missing_setup(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    # Pin the environment-dependent login hint (see status test above).
    monkeypatch.setattr("airelay.cli._is_headless_environment", lambda: False)
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    args = parser.parse_args(
        [
            "doctor",
            "--json",
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
        ]
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(args.async_func(args))
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}

    assert excinfo.value.code == 1
    assert payload["ok"] is False
    assert checks["relay_token"]["status"] == "fail"
    assert checks["openai_auth"]["status"] == "fail"
    assert checks["openai_models"]["status"] == "skip"
    assert "airelays init" in payload["next_steps"]
    assert "airelays login" in payload["next_steps"]


def test_cli_doctor_runs_upstream_model_and_response_smoke_checks(
    tmp_path, capsys, monkeypatch
) -> None:
    _clear_airelay_env(monkeypatch)
    # This test exercises the OpenAI smoke checks. The Claude readiness
    # check depends on the host machine (installed + signed-in CLI passes,
    # anything else fails doctor), so pin the runtime off to keep the
    # result deterministic on CI runners without the claude CLI.
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    parser = build_parser()
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    data_dir = tmp_path / "state"
    (data_dir / "relay-token").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "relay-token").write_text("relay-token\n", encoding="utf-8")
    _write_auth_payload(
        data_dir,
        {
            "tokens": {
                "access_token": "access",
                "refresh_token": "refresh",
                "account_id": "acct_123",
            },
            "bound_account_id": "acct_123",
            "last_refresh": "2099-01-01T00:00:00+00:00",
        },
    )
    calls: list[str] = []

    class FakeBackend:
        def __init__(self, settings, manager, traffic):  # type: ignore[no-untyped-def]
            del settings, manager, traffic

        async def list_models(self, request_id: str) -> dict[str, object]:
            calls.append(f"models:{request_id}")
            return {"models": [{"slug": "gpt-doctor"}]}

        async def collect_response(
            self,
            payload: dict[str, object],
            request_id: str,
            session_id: str | None,
        ) -> dict[str, object]:
            calls.append(f"response:{request_id}")
            assert payload["model"] == "gpt-doctor"
            assert session_id is None
            return {
                "id": "resp_doctor",
                "status": "completed",
                "model": "gpt-doctor",
                "output": [],
            }

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("airelay.cli.ChatGptCodexBackend", FakeBackend)
    args = parser.parse_args(
        [
            "doctor",
            "--json",
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "--auth-storage",
            "file",
        ]
    )

    asyncio.run(args.async_func(args))
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}

    assert payload["ok"] is True
    assert checks["config"]["status"] == "pass"
    assert checks["relay_token"]["status"] == "pass"
    assert checks["openai_auth"]["status"] == "pass"
    assert checks["openai_models"]["status"] == "pass"
    assert checks["openai_models"]["data"]["selected_model"] == "gpt-doctor"
    assert checks["openai_response"]["status"] == "pass"
    assert calls == ["models:doctor_models", "response:doctor_response", "close"]


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
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "true")
    args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert "airelays login" not in payload["next_steps"]
    assert "airelays claude login" in payload["next_steps"]


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

    assert re.search(r"AIRelays \S+ Token", human)
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

    assert re.search(r"AIRelays \S+ Server", output)
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

    assert re.search(r"AIRelays \S+ Server", output)
    assert "disabled" in output
    assert "optional placeholder only" in output
    assert "ChatGPT login" in output
    assert "airelays login" in output
    assert captured["port"] == 8090


def test_cli_serve_allows_no_auth_when_claude_is_enabled(
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

    captured: dict[str, object] = {}

    class _FakeRegistry:
        @staticmethod
        def provider_statuses() -> dict[str, object]:
            return {
                "openai": {"enabled": False, "ready_for_requests": False},
                "claude": {"enabled": True, "ready_for_requests": True},
            }

    def fake_run(app, host, port, log_level):  # type: ignore[no-untyped-def]
        del app
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr("airelay.cli._provider_registry", lambda settings, manager: _FakeRegistry())
    monkeypatch.setattr("airelay.cli.uvicorn.run", fake_run)

    args.func(args)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8080


def test_cli_init_allows_no_auth_when_claude_is_enabled(
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

    args.func(args)

    assert not (tmp_path / "state" / "relay-token").exists()


def test_refresh_proceeds_with_a_warning_when_the_filesystem_cannot_lock(tmp_path, monkeypatch, caplog) -> None:
    """NFS without lockd refuses locks: refresh must still work (and say why
    it is unguarded), not stall and report a phantom concurrent refresh."""
    import errno

    from airelay import private_io

    storage_root = tmp_path / "airelay"
    _write_auth_payload(storage_root, {"tokens": {"id_token": "id-token", "access_token": "old", "refresh_token": "r1"}})

    def no_locks(fd):
        raise OSError(errno.ENOLCK, "No locks available")

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "new", "refresh_token": "r2"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr(private_io, "_lock", no_locks)
    monkeypatch.setattr("airelay.auth.httpx.AsyncClient", FakeClient)
    manager = AuthManager(storage_root, "file", "https://auth.openai.com")
    with caplog.at_level("WARNING"):
        record = asyncio.run(manager.refresh_tokens(force=True))
    assert record.refresh_token == "r2"
    assert "without the cross-process lock" in caplog.text


def test_cancelled_refresh_never_strands_the_refresh_lock(tmp_path, monkeypatch) -> None:
    """A client disconnecting while its refresh waits for the lock must not
    leave the lock held, and the refresh it started still saves its token."""
    from airelay.private_io import InterProcessLock

    storage_root = tmp_path / "airelay"
    _write_auth_payload(storage_root, {"tokens": {"id_token": "id-token", "access_token": "old", "refresh_token": "r1"}})

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "new", "refresh_token": "r2"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr("airelay.auth.httpx.AsyncClient", FakeClient)
    manager = AuthManager(storage_root, "file", "https://auth.openai.com")
    holder = InterProcessLock(manager.storage.lock_path)
    holder.acquire(timeout=1)

    async def scenario():
        caller = asyncio.create_task(manager.refresh_tokens(force=True))
        await asyncio.sleep(0.2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        holder.release()
        for _ in range(50):
            await asyncio.sleep(0.1)
            if manager.load().refresh_token == "r2":
                break

    asyncio.run(scenario())
    assert manager.load().refresh_token == "r2"
    probe = InterProcessLock(manager.storage.lock_path)
    probe.acquire(timeout=2)
    probe.release()


def test_one_relay_per_data_directory(tmp_path, capsys) -> None:
    """A second relay on the same data directory is refused with the first
    one's pid and a status the desktop app recognises; the lock frees
    itself when the holder releases it."""
    import os

    from airelay.cli import RELAY_ALREADY_RUNNING, _hold_relay_lock
    from airelay.config import Settings

    settings = Settings(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    settings.ensure_directories()
    first = _hold_relay_lock(settings)
    try:
        with pytest.raises(SystemExit) as excinfo:
            _hold_relay_lock(settings, wait=0)
    finally:
        first.release()
    assert excinfo.value.code == RELAY_ALREADY_RUNNING == 75
    assert f"already running with the data directory {settings.data_dir} (pid {os.getpid()})" in capsys.readouterr().err
    _hold_relay_lock(settings, wait=0).release()


def test_cli_accounts_resets_and_redeem_talk_to_the_running_relay(tmp_path, capsys, monkeypatch) -> None:
    """`accounts resets` reads /v1/relay/accounts/resets and `accounts redeem`
    posts to .../redeem through the running relay (which holds the sign-ins
    and re-benches the account); --yes skips the prompt and the outcome is
    shown verbatim."""
    import httpx

    _clear_airelay_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    data_dir = tmp_path / "state"
    data_dir.mkdir(parents=True)
    (data_dir / "relay-token").write_text("secret-token\n", encoding="utf-8")
    seen: list[dict[str, object]] = []

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return Response({
            "object": "accounts.resets",
            "available_count": 1,
            "accounts": [
                {"slug": "default", "email": "one@example.com", "resets": {
                    "available_count": 1, "applicable_available_count": 0, "redeemable_now": False,
                    "credits": [{"id": "RateLimitResetCredit_1", "status": "available", "title": "Full reset",
                                 "granted_by": "Codex Team", "granted_at": "2026-10-08T00:00:05Z",
                                 "expires_at": "2026-11-07T00:00:05Z", "expires_in_seconds": 20 * 86400 + 3 * 3600}],
                }},
                {"slug": "two", "email": "two@example.com", "resets": {"available_count": 0, "credits": []}},
                {"slug": "dead", "email": "dead@example.com", "error": "Token refresh failed: 401"},
            ],
        })

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return Response({
            "object": "accounts.reset_redeem", "slug": "default", "email": "one@example.com",
            "outcome": "reset", "reset_applied": True, "windows_reset": 1, "credit_id": "RateLimitResetCredit_1",
            "status": {"rate_limits": {"default": {"primary_window": {"used_percent": 0, "window_label": "weekly"}}}},
        })

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    parser = build_parser()
    common = ["--config", str(tmp_path / "config.toml"), "--data-dir", str(data_dir)]

    args = parser.parse_args(["accounts", "resets", *common])
    args.func(args)
    human = capsys.readouterr().out
    assert seen[0]["method"] == "GET" and seen[0]["url"].endswith("/v1/relay/accounts/resets")
    assert seen[0]["headers"] == {"authorization": "Bearer secret-token"}
    assert "one@example.com" in human
    assert "1 available, held; nothing to reset right now" in human
    assert "Full reset by Codex Team, granted 2026-10-08; expires 2026-11-07 (in 20d 3h)" in human
    assert "none available" in human
    assert "unavailable: Token refresh failed: 401" in human
    assert "airelays accounts redeem <email>" in human

    args = parser.parse_args(["accounts", "redeem", "one@", "--credit", "RateLimitResetCredit_1", "--yes", *common])
    args.func(args)
    human = capsys.readouterr().out
    assert seen[1]["method"] == "POST" and seen[1]["url"].endswith("/v1/relay/accounts/resets/redeem")
    assert seen[1]["json"] == {"account": "one@", "credit_id": "RateLimitResetCredit_1"}
    assert "Usage reset applied" in human
    assert "weekly used" in human and "0%" in human

    # Without --yes the prompt gates the irreversible call.
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    args = parser.parse_args(["accounts", "redeem", "one@", *common])
    args.func(args)
    assert "Nothing redeemed." in capsys.readouterr().out
    assert len(seen) == 2

    args = parser.parse_args(["accounts", "resets", "--json", *common])
    args.func(args)
    assert json.loads(capsys.readouterr().out)["available_count"] == 1


def test_balance_command_shows_and_switches_the_running_relay(tmp_path, monkeypatch, capsys):
    import httpx

    _clear_airelay_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    data_dir = tmp_path / "state"
    data_dir.mkdir(parents=True)
    (data_dir / "relay-token").write_text("secret-token\n", encoding="utf-8")
    seen: list[dict[str, object]] = []
    state = {"openai": "balanced", "claude": "balanced"}

    class Response:
        def __init__(self, payload, status_code=200):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def payload():
        return {"object": "relay.balance", **state, "strategies": ["balanced", "weighted"], "runtime_only": True}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append({"method": "GET", "url": url, "headers": headers})
        return Response(payload())

    def fake_put(url, headers=None, json=None, timeout=None):
        seen.append({"method": "PUT", "url": url, "headers": headers, "json": json})
        if json.get("openai") == "fastest":
            return Response({"detail": "Invalid balance value"}, status_code=422)
        state.update(json)
        return Response(payload())

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", fake_put)
    parser = build_parser()
    common = ["--config", str(tmp_path / "config.toml"), "--data-dir", str(data_dir)]

    args = parser.parse_args(["balance", *common])
    args.func(args)
    human = capsys.readouterr().out
    assert seen[0]["method"] == "GET" and seen[0]["url"].endswith("/v1/relay/balance")
    assert seen[0]["headers"] == {"authorization": "Bearer secret-token"}
    assert "balanced" in human and "Applied" not in human

    args = parser.parse_args(["balance", "weighted", *common])
    args.func(args)
    human = capsys.readouterr().out
    assert seen[1]["method"] == "PUT" and seen[1]["url"].endswith("/v1/relay/balance")
    assert seen[1]["json"] == {"openai": "weighted"}
    assert "weighted" in human and "Applied to the running relay" in human

    args = parser.parse_args(["balance", "ordered", "--provider", "claude", "--json", *common])
    args.func(args)
    assert seen[2]["json"] == {"claude": "ordered"}
    assert json.loads(capsys.readouterr().out)["claude"] == "ordered"

    with pytest.raises(SystemExit) as failure:
        args = parser.parse_args(["balance", "weighted", *common])
        seen.clear()
        monkeypatch.setattr(httpx, "put", lambda url, headers=None, json=None, timeout=None: Response({"detail": "Invalid balance value"}, status_code=422))
        args.func(args)
    assert "422" in str(failure.value)
