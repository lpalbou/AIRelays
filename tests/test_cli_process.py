"""Exercise the real terminal entry point with isolated, fake CLI profiles."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from airelay.cli import build_parser
from airelay.config import Settings


@pytest.mark.parametrize("option,value", [
    ("--config", "/tmp/airelays-example.toml"),
    ("--data-dir", "/tmp/airelays-example-data"),
    ("--logs-dir", "/tmp/airelays-example-logs"),
    ("--auth-storage", "file"),
    ("--bearer-token-file", "/tmp/airelays-example-token"),
])
@pytest.mark.parametrize("level", [0, 1, 2])
def test_shared_options_survive_nested_commands(option, value, level):
    arguments = ["claude", "accounts", "--json"]
    arguments[level:level] = [option, value]
    args = build_parser().parse_args(arguments)
    assert getattr(args, option[2:].replace("-", "_")) == value


def test_explicit_nested_option_overrides_parent():
    args = build_parser().parse_args([
        "--config", "first.toml", "claude", "--config", "second.toml",
        "accounts", "--config", "third.toml",
    ])
    assert args.config == "third.toml"


@pytest.fixture
def terminal(tmp_path):
    if os.name == "nt":
        pytest.skip("Fake CLI executable uses a POSIX shebang")
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
profile = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
identity = profile / 'test-identity.json'
if args == ['--version']:
    print('test-cli')
elif args[:2] == ['auth', 'login']:
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ
    profile.mkdir(parents=True, exist_ok=True)
    identity.write_text(json.dumps({'loggedIn': True, 'email': pathlib.Path(__file__).with_name('next-email').read_text(),
        'subscriptionType': 'max', 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'}))
elif args[:2] == ['auth', 'status']:
    print(identity.read_text() if identity.exists() else json.dumps({'loggedIn': False}))
elif args[:2] == ['auth', 'logout']:
    identity.unlink(missing_ok=True)
elif '-p' in args:
    sys.stdin.read()
    print(json.dumps({'type': 'result', 'result': json.loads(identity.read_text())['email'], 'is_error': False}))
else:
    raise SystemExit('Unexpected CLI arguments')
''')
    script.chmod(0o700)
    settings = Settings(
        config_path=tmp_path / "config.toml", data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs", bearer_token_file=tmp_path / "relay-token",
        claude_oauth_token_file=tmp_path / "claude-token", auth_storage_mode="file",
        enable_openai_provider=False, enable_claude=True, claude_bin=str(script),
        claude_balance="round_robin", claude_models=("claude:sonnet",),
    )
    settings.write_config_file()
    # Never read the developer's provider credentials or config overrides.
    env = {key: value for key, value in os.environ.items() if not key.startswith(
        ("AIRELAY", "CLAUDE", "ANTHROPIC", "OPENAI")
    )}
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "default-profile")
    installed_cli = os.environ.get("AIRELAYS_TEST_CLI_EXECUTABLE")
    if installed_cli:
        env.pop("PYTHONPATH", None)
        entrypoint = [installed_cli]
    else:
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        entrypoint = [sys.executable, "-m", "airelays"]
    prefix = [*entrypoint, "--config", str(settings.config_path)]

    def run(*args, email="one@example.com", check=True):
        (tmp_path / "next-email").write_text(email)
        result = subprocess.run(
            [*prefix, *args], env=env,
            cwd=tmp_path, capture_output=True, text=True, timeout=20,
        )
        if check:
            assert result.returncode == 0, result.stderr + result.stdout
        return result

    return settings, env, prefix, run


def test_terminal_account_enrollment_renewal_and_targeted_logout(terminal):
    settings, _, _, run = terminal
    run("init")
    for email in ("one@example.com", "two@example.com"):
        run("claude", "login", email=email)
    payload = json.loads(run("claude", "accounts", "--json").stdout)
    assert payload["balance"] == "round_robin"
    assert {account["email"] for account in payload["accounts"]} == {"one@example.com", "two@example.com"}
    assert all(account["ready_for_requests"] for account in payload["accounts"])
    human = run("claude", "accounts").stdout
    assert "one@example.com" in human and "two@example.com" in human
    rejected = run("claude", "logout", check=False)
    assert rejected.returncode != 0 and "Choose a Claude account" in rejected.stderr
    run("claude", "login", "--replace", "one@example.com")
    run("claude", "logout", "one@example.com", "--json")
    remaining = json.loads(run("claude", "accounts", "--json").stdout)["accounts"]
    assert [account["email"] for account in remaining] == ["two@example.com"]
    assert json.loads(run("status", "--json").stdout)["providers"]["claude"]["ready_for_requests"]
    assert settings.bearer_token_file.exists()


def test_terminal_serve_balances_accounts_and_enforces_local_auth(terminal):
    settings, env, prefix, run = terminal
    run("init")
    for email in ("one@example.com", "two@example.com"):
        run("claude", "login", email=email)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [*prefix, "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, process.communicate()
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=1) as response:
                    assert json.load(response)["ok"]
                break
            except (urllib.error.URLError, TimeoutError):
                assert time.monotonic() < deadline, "CLI relay did not start"
                time.sleep(0.1)
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(base + "/v1/relay/status", timeout=5)
        assert rejected.value.code == 401
        token = settings.bearer_token_file.read_text().strip()
        responses = []
        for _ in range(2):
            request = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "claude:sonnet", "messages": [{"role": "user", "content": "hello"}]}).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                responses.append(json.load(response)["choices"][0]["message"]["content"])
        assert set(responses) == {"one@example.com", "two@example.com"}
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
