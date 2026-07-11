from __future__ import annotations

from pathlib import Path

import pytest

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
        enable_claude=True,
        models_cache_ttl_seconds=42.0,
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
    assert loaded.enable_claude is True
    assert loaded.models_cache_ttl_seconds == 42.0
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
    monkeypatch.setenv("AIRELAYS_MODELS_CACHE_TTL_SECONDS", "17.5")

    loaded = Settings.from_sources(Path(config_path))

    assert loaded.port == 7777
    assert loaded.models_cache_ttl_seconds == 17.5


def test_openai_balance_defaults_to_balanced(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIRELAYS_OPENAI_BALANCE", raising=False)
    loaded = Settings.from_sources(tmp_path / "missing.toml")
    assert loaded.openai_balance == "balanced"


def test_openai_balance_is_normalized_and_validated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIRELAYS_OPENAI_BALANCE", " Round-Robin ")
    assert Settings.from_sources(tmp_path / "missing.toml").openai_balance == "round_robin"
    monkeypatch.setenv("AIRELAYS_OPENAI_BALANCE", "Balanced")
    assert Settings.from_sources(tmp_path / "missing.toml").openai_balance == "balanced"
    monkeypatch.setenv("AIRELAYS_OPENAI_BALANCE", "ordered")
    assert Settings.from_sources(tmp_path / "missing.toml").openai_balance == "ordered"
    # A typo must fail loudly, not silently select a routing policy.
    monkeypatch.setenv("AIRELAYS_OPENAI_BALANCE", "roundrobin")
    with pytest.raises(ValueError, match="balance"):
        Settings.from_sources(tmp_path / "missing.toml")


def test_claude_is_enabled_by_default(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "missing.toml"
    monkeypatch.delenv("AIRELAYS_ENABLE_CLAUDE", raising=False)
    monkeypatch.delenv("AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL", raising=False)
    monkeypatch.delenv("AIRELAY_ENABLE_CLAUDE_EXPERIMENTAL", raising=False)

    loaded = Settings.from_sources(config_path)

    assert loaded.enable_claude is True


def test_legacy_claude_experimental_env_still_disables_claude(tmp_path, monkeypatch) -> None:
    """Environments configured while the runtime carried the "experimental"
    label must keep working after the rename."""
    config_path = tmp_path / "missing.toml"
    monkeypatch.delenv("AIRELAYS_ENABLE_CLAUDE", raising=False)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL", "false")

    loaded = Settings.from_sources(config_path)

    assert loaded.enable_claude is False


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


def test_claude_guardrails_allow_open_mode_but_require_loopback(tmp_path) -> None:
    settings = Settings(
        host="127.0.0.1",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        bearer_token_file=tmp_path / "data" / "relay-token",
        require_bearer_auth=False,
        enable_claude=True,
    )

    settings.validate_provider_guardrails()

    settings.host = "0.0.0.0"
    try:
        settings.validate_provider_guardrails()
    except RuntimeError as exc:
        assert "restricted to loopback" in str(exc)
    else:
        raise AssertionError("Expected Claude guardrails to reject non-loopback host")
