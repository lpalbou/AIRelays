from __future__ import annotations

from pathlib import Path

from airelay.config import Settings


def test_config_file_round_trip(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    settings = Settings(
        host="127.0.0.1",
        port=9090,
        config_path=config_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        enable_claude_experimental=True,
        claude_models=("claude:sonnet",),
    )

    assert settings.write_config_file(force=True) is True

    loaded = Settings.from_sources(config_path)

    assert loaded.config_path == config_path
    assert loaded.port == 9090
    assert loaded.data_dir == tmp_path / "data"
    assert loaded.auth_file() == tmp_path / "data" / "auth.json"
    assert loaded.logs_dir == tmp_path / "logs"
    assert loaded.bearer_token_file == tmp_path / "data" / "relay-token"
    assert loaded.enable_claude_experimental is True
    assert loaded.claude_models == ("claude:sonnet",)


def test_env_overrides_config_file(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    settings = Settings(
        port=9090,
        config_path=config_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
    )
    settings.write_config_file(force=True)

    monkeypatch.setenv("AIRELAYS_PORT", "7777")

    loaded = Settings.from_sources(Path(config_path))

    assert loaded.port == 7777


def test_explicit_bearer_token_overrides_existing_token_file(tmp_path) -> None:
    token_file = tmp_path / "data" / "relay-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("file-token\n", encoding="utf-8")

    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=token_file,
        bearer_token="inline-token",
    )

    assert settings.resolve_bearer_token() == "inline-token"


def test_legacy_codex_home_is_ignored_when_loading_config(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[paths]
codex_home = "/tmp/legacy-codex"
data_dir = "/tmp/airelay-data"
logs_dir = "/tmp/airelay-logs"
""".strip(),
        encoding="utf-8",
    )

    loaded = Settings.from_sources(config_path)

    assert loaded.data_dir == Path("/tmp/airelay-data")
    assert loaded.auth_file() == Path("/tmp/airelay-data/auth.json")
    assert "codex_home" not in loaded.render_config_toml()


def test_claude_guardrails_require_loopback_and_bearer_auth(tmp_path) -> None:
    settings = Settings(
        host="127.0.0.1",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        require_bearer_auth=False,
        enable_claude_experimental=True,
    )

    try:
        settings.validate_provider_guardrails()
    except RuntimeError as exc:
        assert "requires AIRelays bearer auth" in str(exc)
    else:
        raise AssertionError("Expected Claude guardrails to reject open mode")
