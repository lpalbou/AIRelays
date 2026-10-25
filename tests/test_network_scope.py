"""`[security] network_scope`: which client addresses may use the relay."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from airelay.app import create_app
from airelay.cli import build_parser
from airelay.config import Settings
from airelay.security import EndpointProtector, address_in_scope


class _Traffic:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def write(self, entry: dict) -> None:
        self.entries.append(entry)


def _settings(tmp_path, **overrides) -> Settings:
    token_file = tmp_path / "data" / "relay-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("secret-token\n", encoding="utf-8")
    values = {
        "host": "0.0.0.0",
        "config_path": tmp_path / "config.toml",
        "data_dir": tmp_path / "data",
        "logs_dir": tmp_path / "logs",
        "bearer_token_file": token_file,
        "enable_claude": False,
    }
    values.update(overrides)
    return Settings(**values)


def _request(client_ip: str, path: str = "/v1/models", headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(b"authorization", b"Bearer secret-token")]
    raw_headers += [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": (client_ip, 50000),
            "server": ("0.0.0.0", 8317),
            "scheme": "http",
        }
    )


def _acquire(protector: EndpointProtector, request: Request):
    lease, rejection = protector.acquire("req-1", request)
    protector.release(lease)
    return rejection


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.20",
        "100.64.0.7",  # Tailscale / CGNAT
        "169.254.10.10",
        "fd12:3456::1",
        "fe80::1",
        "::ffff:192.168.1.20",
    ],
)
def test_private_scope_accepts_private_networks(tmp_path, address) -> None:
    protector = EndpointProtector(_settings(tmp_path), _Traffic())
    assert _acquire(protector, _request(address)) is None


@pytest.mark.parametrize("address", ["203.0.113.5", "8.8.8.8", "2001:db8::5", "::ffff:203.0.113.5", "100.128.0.1"])
def test_private_scope_rejects_public_addresses_before_auth(tmp_path, address) -> None:
    traffic = _Traffic()
    protector = EndpointProtector(_settings(tmp_path), traffic)

    rejection = _acquire(protector, _request(address))

    assert rejection is not None
    assert rejection.status_code == 403
    body = json.loads(rejection.body)
    assert body["error"]["code"] == "client_not_on_private_network"
    assert traffic.entries[-1]["phase"] == "endpoint_rejected"
    assert traffic.entries[-1]["reason"] == "network_scope"
    # Rejected before authentication: no failed-auth record, no block.
    assert not any(entry.get("phase") == "endpoint_auth_failed" for entry in traffic.entries)
    assert protector.diagnostics()["tracked_clients"] == 0


def test_private_scope_also_hides_unprotected_paths(tmp_path) -> None:
    protector = EndpointProtector(_settings(tmp_path), _Traffic())
    assert _acquire(protector, _request("203.0.113.5", path="/healthz")).status_code == 403
    assert _acquire(protector, _request("192.168.1.2", path="/healthz")) is None


def test_any_scope_accepts_public_addresses(tmp_path) -> None:
    protector = EndpointProtector(_settings(tmp_path, network_scope="any"), _Traffic())
    assert _acquire(protector, _request("203.0.113.5")) is None


def test_loopback_scope_and_loopback_host_accept_only_this_machine(tmp_path) -> None:
    for settings in (
        _settings(tmp_path, network_scope="loopback"),
        # A loopback listener implies the loopback scope, whatever the file says.
        _settings(tmp_path, host="127.0.0.1", network_scope="any"),
    ):
        protector = EndpointProtector(settings, _Traffic())
        assert _acquire(protector, _request("127.0.0.1")) is None
        rejection = _acquire(protector, _request("192.168.1.20"))
        assert rejection is not None and rejection.status_code == 403
        assert json.loads(rejection.body)["error"]["code"] == "client_not_on_this_machine"


def test_forwarded_for_is_the_scoped_address_only_when_trusted(tmp_path) -> None:
    forwarded = {"X-Forwarded-For": "203.0.113.5, 127.0.0.1"}
    untrusted = EndpointProtector(_settings(tmp_path), _Traffic())
    # Not trusted: the header is ignored, the private peer decides.
    assert _acquire(untrusted, _request("192.168.1.2", headers=forwarded)) is None

    trusted = EndpointProtector(_settings(tmp_path, trust_x_forwarded_for=True), _Traffic())
    # Trusted proxy on loopback forwarding a public client: out of scope.
    rejection = _acquire(trusted, _request("127.0.0.1", headers=forwarded))
    assert rejection is not None and rejection.status_code == 403
    # A garbage forwarded value never counts as "no network peer".
    assert _acquire(trusted, _request("127.0.0.1", headers={"X-Forwarded-For": "testclient"})).status_code == 403
    assert _acquire(trusted, _request("127.0.0.1", headers={"X-Forwarded-For": "10.0.0.8"})) is None


def test_address_in_scope_rejects_unparsable_values() -> None:
    assert address_in_scope("not-an-ip", "private") is False
    assert address_in_scope("not-an-ip", "any") is True


def test_network_scope_round_trips_and_validates(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIRELAYS_NETWORK_SCOPE", raising=False)
    assert Settings.from_sources(tmp_path / "missing.toml").network_scope == "private"

    settings = _settings(tmp_path, network_scope="any")
    settings.write_config_file(force=True)
    assert 'network_scope = "any"' in settings.config_path.read_text(encoding="utf-8")
    assert Settings.from_sources(settings.config_path).network_scope == "any"

    monkeypatch.setenv("AIRELAYS_NETWORK_SCOPE", " Loopback ")
    assert Settings.from_sources(settings.config_path).network_scope == "loopback"
    # A typo must fail loudly instead of silently picking an audience.
    monkeypatch.setenv("AIRELAYS_NETWORK_SCOPE", "internet")
    with pytest.raises(ValueError, match="network_scope"):
        Settings.from_sources(settings.config_path)


def test_any_scope_requires_bearer_auth(tmp_path) -> None:
    open_any = _settings(tmp_path, network_scope="any", require_bearer_auth=False)
    with pytest.raises(RuntimeError, match="require_bearer_auth"):
        open_any.validate_provider_guardrails()
    with pytest.raises(RuntimeError, match="require_bearer_auth"):
        create_app(open_any)

    # A loopback listener cannot reach the internet: no key needed there.
    _settings(tmp_path, host="127.0.0.1", network_scope="any", require_bearer_auth=False).validate_provider_guardrails()
    _settings(tmp_path, network_scope="private", require_bearer_auth=False).validate_provider_guardrails()
    _settings(tmp_path, network_scope="any").validate_provider_guardrails()


def test_cli_serve_refuses_open_internet_access(tmp_path, monkeypatch) -> None:
    for name in ("AIRELAYS_NETWORK_SCOPE", "AIRELAYS_REQUIRE_BEARER_AUTH", "AIRELAYS_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("airelay.cli.uvicorn.run", lambda *args, **kwargs: None)
    args = build_parser().parse_args(
        [
            "serve",
            "--no-auth",
            "--host",
            "0.0.0.0",
            "--network-scope",
            "any",
            "--config",
            str(tmp_path / "config.toml"),
            "--data-dir",
            str(tmp_path / "state"),
        ]
    )
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert "require_bearer_auth" in str(excinfo.value)


def test_status_reports_the_enforced_scope(tmp_path) -> None:
    settings = _settings(tmp_path)
    assert settings.summary()["network_scope"] == "private"
    assert settings.summary()["effective_network_scope"] == "private"
    # The in-process test client has no network peer, so it is never scoped out.
    with TestClient(create_app(settings)) as client:
        payload = client.get("/v1/relay/status", headers={"Authorization": "Bearer secret-token"}).json()
    assert payload["relay"]["network_scope"] == "private"
    assert payload["security"]["network_scope"] == "private"


# ---- headless configuration: `airelays init` and `airelays status` ----


def _clear_env(monkeypatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("AIRELAYS_"):
            monkeypatch.delenv(name, raising=False)


def test_cli_init_writes_host_and_network_scope_and_status_prints_them(tmp_path, capsys, monkeypatch) -> None:
    from airelay.cli import build_parser

    _clear_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    parser = build_parser()
    args = parser.parse_args(
        ["init", "--host", "0.0.0.0", "--port", "8411", "--network-scope", "any",
         "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "Who can connect" in out and "anyone on the internet" in out
    assert "--network-scope any" in out  # the suggested serve command
    text = config_path.read_text(encoding="utf-8")
    assert 'host = "0.0.0.0"' in text and "port = 8411" in text
    assert 'network_scope = "any"' in text and "require_bearer_auth = true" in text
    assert (data_dir / "relay-token").exists()

    args = parser.parse_args(["status", "--config", str(config_path), "--data-dir", str(data_dir)])
    args.func(args)
    out = capsys.readouterr().out
    assert "Who can connect" in out and "any (anyone on the internet (API key required))" in out


def test_cli_init_refuses_internet_scope_without_auth(tmp_path, capsys, monkeypatch) -> None:
    from airelay.cli import build_parser

    _clear_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    config_path = tmp_path / "config.toml"
    parser = build_parser()
    args = parser.parse_args(
        ["init", "--no-auth", "--host", "0.0.0.0", "--network-scope", "any",
         "--config", str(config_path), "--data-dir", str(tmp_path / "state")]
    )
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert "require_bearer_auth" in str(exc.value)
    assert not config_path.exists()


def test_cli_status_narrows_scope_on_a_loopback_listener(tmp_path, capsys, monkeypatch) -> None:
    from airelay.cli import build_parser

    _clear_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    parser = build_parser()
    args = parser.parse_args(
        ["init", "--host", "127.0.0.1", "--network-scope", "any",
         "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "loopback (this machine only); configured any, narrowed by the loopback listener" in out


def test_cli_init_refuses_listener_flags_on_an_existing_config_without_force(tmp_path, capsys, monkeypatch) -> None:
    from airelay.cli import build_parser

    _clear_env(monkeypatch)
    monkeypatch.setenv("AIRELAYS_ENABLE_CLAUDE", "false")
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "state"
    parser = build_parser()
    parser.parse_args(["init", "--config", str(config_path), "--data-dir", str(data_dir)]).func(
        parser.parse_args(["init", "--config", str(config_path), "--data-dir", str(data_dir)])
    )
    capsys.readouterr()
    args = parser.parse_args(
        ["init", "--host", "0.0.0.0", "--network-scope", "any", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert "--force" in str(exc.value) and "--network-scope" in str(exc.value)
    assert 'network_scope = "any"' not in config_path.read_text(encoding="utf-8")

    args = parser.parse_args(
        ["init", "--force", "--host", "0.0.0.0", "--network-scope", "any", "--config", str(config_path), "--data-dir", str(data_dir)]
    )
    args.func(args)
    assert 'network_scope = "any"' in config_path.read_text(encoding="utf-8")

