from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from airelay.app import create_app
from airelay.claude_accounts import ClaudeAccountPool
from airelay.claude_auth import ClaudeProfile, discover_profiles, enroll_profile, forget_profile, new_profile, suspend_profile
from airelay.cli import build_parser
from airelay.config import Settings
from airelay.providers import ClaudeCliRuntime, ProviderError, _claude_failure
from claude_fakes import use_fake_cli


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, logs_dir=tmp_path / "logs",
                    claude_oauth_token_file=tmp_path / "claude-token",
                    bearer_token_file=tmp_path / "relay-token", require_bearer_auth=False,
                    enable_openai_provider=False, enable_claude=True,
                    # An existing executable; CLI sign-in tests stub subprocess.run.
                    claude_bin=sys.executable,
                    claude_models=("claude:sonnet", "claude:opus"))


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    """Never inspect the developer's real CLI sign-in or query its usage."""
    def probe(self):
        self._last_probe = {"installed": True, "logged_in": True, **self.profile.identity}
        return self._last_probe

    monkeypatch.setattr(ClaudeCliRuntime, "_run_status_command", probe)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def add(settings, email):
    profile = new_profile(settings)
    enroll_profile(profile, {"logged_in": True, "email": email, "subscription_type": "pro"})
    return next(p for p in discover_profiles(settings) if p.slug == profile.slug)


@pytest.fixture
def pool(settings):
    add(settings, "one@example.com")
    add(settings, "two@example.com")
    return ClaudeAccountPool(settings)


def accounts(pool):
    return [a for a in pool._accounts.values() if a.runtime.profile.managed]


BODY = {"model": "claude:sonnet", "messages": [{"role": "user", "content": "Hello"}]}


def serve(monkeypatch, account, result="ok", error=None):
    async def run(request, request_id):
        if error:
            raise error
        return {"result": result}
    monkeypatch.setattr(account.runtime, "_run_json", run)


def usage(account, *, weekly=0, short=0, age=0, scoped=None):
    account.usage_at = time.time() - age
    account.usage = {"rate_limits": {
        "default": {"primary_window": {"used_percent": short, "window_seconds": 18000, "reset_at": time.time() + 60},
                    "secondary_window": {"used_percent": weekly, "window_seconds": 604800, "reset_at": time.time() + 3600}},
        "additional": scoped or [],
    }}


def test_profile_credentials_ignore_global_token_and_never_modify_environment(settings, monkeypatch):
    profile = add(settings, "one@example.com")
    settings.write_claude_oauth_token("legacy-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    runtime = ClaudeCliRuntime(settings, profile=profile)
    env = runtime._subprocess_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(profile.config_dir)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient-secret"
    assert ClaudeCliRuntime(settings)._subprocess_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "legacy-secret"


def test_ambient_claude_code_sign_in_is_never_an_account(settings, monkeypatch, tmp_path):
    """ADR 0007: the user's own Claude Code (or an inherited config dir or
    token) never becomes a relay account."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-secret")
    pool = ClaudeAccountPool(settings)
    status = pool.status()
    assert status["accounts"] == [] and status["ready_for_requests"] is False
    with pytest.raises(ProviderError) as excinfo:
        pool.resolve_model("claude:sonnet")
    assert excinfo.value.status_code == 503 and "No Claude account is connected" in excinfo.value.detail
    assert pool.resolve_model("gpt-5.5") is None  # other providers still route
    settings.write_claude_oauth_token("stored")
    assert [row["slug"] for row in pool.status()["accounts"]] == ["default"]


def test_usage_backoff_state_is_separate_per_account(pool):
    first, second = accounts(pool)
    first.runtime._usage_failed("usage_unavailable", "Claude did not return live usage.")
    reloaded = ClaudeAccountPool(pool._settings)
    assert reloaded._accounts[first.runtime.profile.slug].runtime._usage_retry_at > time.time()
    assert reloaded._accounts[second.runtime.profile.slug].runtime._usage_retry_at == 0


def test_only_completed_signins_are_discovered_and_paths_stay_fixed(settings):
    pending = new_profile(settings)
    assert discover_profiles(settings) == []
    enroll_profile(pending, {"logged_in": True, "email": "one@example.com"})
    profile, = discover_profiles(settings)
    assert profile.config_dir == pending.config_dir
    if os.name != "nt":  # Windows protects it with the profile ACL instead
        assert (profile.root / "account.json").stat().st_mode & 0o777 == 0o600
    forget_profile(profile)
    assert discover_profiles(settings) == []
    assert profile.config_dir.exists()


def test_identity_mismatch_prevents_routing(pool, monkeypatch):
    account = accounts(pool)[0]
    monkeypatch.setattr(account.runtime, "_cached_probe", lambda: {
        "installed": True, "logged_in": True, "email": "different@example.com",
    })
    status = account.runtime.status()
    assert status["identity_mismatch"] is True
    assert status["ready_for_requests"] is False
    assert status["email"] == account.runtime.profile.identity["email"]


@pytest.mark.parametrize("strategy", ["balanced", "round-robin", "ordered"])
def test_claude_balancing_configuration_roundtrips(settings, tmp_path, monkeypatch, strategy):
    monkeypatch.setenv("AIRELAYS_CLAUDE_BALANCE", strategy)
    monkeypatch.setenv("AIRELAYS_CLAUDE_ACCOUNT_COOLDOWN_SECONDS", "87")
    loaded = Settings.from_sources(tmp_path / "missing.toml")
    assert loaded.claude_balance == strategy.replace("-", "_")
    assert loaded.claude_account_cooldown_seconds == 87
    path = tmp_path / "settings.toml"
    path.write_text(loaded.render_config_toml())
    monkeypatch.delenv("AIRELAYS_CLAUDE_BALANCE")
    monkeypatch.delenv("AIRELAYS_CLAUDE_ACCOUNT_COOLDOWN_SECONDS")
    reloaded = Settings.from_sources(path)
    assert reloaded.claude_balance == loaded.claude_balance
    assert reloaded.claude_account_cooldown_seconds == 87


def test_claude_settings_reject_bad_strategy_and_prevent_zero_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRELAYS_CLAUDE_BALANCE", "bad-strategy")
    with pytest.raises(ValueError, match="providers.claude"):
        Settings.from_sources(tmp_path / "missing.toml")
    monkeypatch.setenv("AIRELAYS_CLAUDE_BALANCE", "balanced")
    monkeypatch.setenv("AIRELAYS_CLAUDE_MAX_CONCURRENT_REQUESTS", "0")
    assert Settings.from_sources(tmp_path / "missing.toml").claude_max_concurrent_requests == 1


@pytest.mark.asyncio
async def test_round_robin_and_add_remove_live(pool, settings, monkeypatch):
    settings.claude_balance = "round_robin"
    for account in accounts(pool):
        serve(monkeypatch, account, account.runtime.profile.slug)
    outputs = [(await pool.create_chat_completion(BODY, str(i)))["choices"][0]["message"]["content"] for i in range(4)]
    assert outputs[0] != outputs[1]
    assert outputs[:2] == outputs[2:]
    third = add(settings, "third@example.com")
    pool.refresh_if_changed()
    serve(monkeypatch, pool._accounts[third.slug], "third")
    assert (await pool.create_chat_completion(BODY, "new"))["choices"][0]["message"]["content"] == "third"
    forget_profile(third)
    pool.refresh_if_changed()
    assert third.slug not in pool._accounts


@pytest.mark.asyncio
async def test_balancing_uses_weekly_quota_and_skips_exhausted_short_window(pool, monkeypatch):
    first, second = accounts(pool)
    serve(monkeypatch, first, "first")
    serve(monkeypatch, second, "second")
    usage(first, weekly=10, short=90)
    usage(second, weekly=60, short=1)
    assert (await pool.create_chat_completion(BODY, "weekly"))["choices"][0]["message"]["content"] == "first"
    usage(first, weekly=10, short=100)
    assert (await pool.create_chat_completion(BODY, "short"))["choices"][0]["message"]["content"] == "second"


@pytest.mark.asyncio
async def test_model_cap_does_not_bench_other_models(pool, monkeypatch):
    first, second = accounts(pool)
    serve(monkeypatch, first, "first")
    serve(monkeypatch, second, "second")
    usage(first, weekly=5, scoped=[{"limit_name": "Opus", "rate_limit": {
        "primary_window": {"used_percent": 100, "window_seconds": 604800, "reset_at": time.time() + 3600},
    }}])
    usage(second, weekly=60)
    opus = await pool.create_chat_completion({**BODY, "model": "claude:opus"}, "opus")
    sonnet = await pool.create_chat_completion(BODY, "sonnet")
    assert opus["choices"][0]["message"]["content"] == "second"
    assert sonnet["choices"][0]["message"]["content"] == "first"


@pytest.mark.asyncio
async def test_stale_usage_rotates_and_all_quota_exhausted_makes_no_requests(pool, monkeypatch):
    first, second = accounts(pool)
    for i, account in enumerate((first, second)):
        serve(monkeypatch, account, str(i))
        usage(account, weekly=20 + i * 40, age=901)
    results = [await pool.create_chat_completion(BODY, str(i)) for i in range(2)]
    assert results[0]["choices"] != results[1]["choices"]
    for account in (first, second):
        usage(account, weekly=100)
    with pytest.raises(ProviderError) as error:
        await pool.create_chat_completion(BODY, "blocked")
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds > 3500


@pytest.mark.asyncio
async def test_known_exhaustion_survives_stale_usage(pool):
    for account in accounts(pool):
        usage(account, weekly=100, age=901)
    with pytest.raises(ProviderError) as error:
        await pool.create_chat_completion(BODY, "old-but-exhausted")
    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_failover_rechecks_removed_accounts(pool, settings, monkeypatch):
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    async def remove_then_fail(request, request_id):
        forget_profile(second.runtime.profile)
        raise ProviderError(429, "exhausted", code="provider_quota_exhausted")
    monkeypatch.setattr(first.runtime, "_run_json", remove_then_fail)
    serve(monkeypatch, second, "must not run")
    with pytest.raises(ProviderError):
        await pool.create_chat_completion(BODY, "removed")
    assert second.last_selected == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 502, 504])
async def test_account_failures_fail_over(pool, settings, monkeypatch, status):
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    serve(monkeypatch, first, error=ProviderError(status, "failed"))
    serve(monkeypatch, second, "second")
    result = await pool.create_chat_completion(BODY, "failover")
    assert result["choices"][0]["message"]["content"] == "second"
    assert first.blocked_until > time.time()


@pytest.mark.asyncio
async def test_bad_requests_never_rotate(pool, settings, monkeypatch):
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    serve(monkeypatch, first, error=ProviderError(422, "invalid prompt"))
    serve(monkeypatch, second, "must not run")
    with pytest.raises(ProviderError, match="invalid prompt"):
        await pool.create_chat_completion(BODY, "invalid")
    assert first.blocked_until == 0
    assert second.last_selected == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [False, True])
async def test_stream_failover_only_before_first_byte(pool, settings, monkeypatch, sent):
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    closed = []
    async def failing(body, request_id):
        try:
            if sent:
                yield b"first"
            raise ProviderError(429, "exhausted", code="provider_quota_exhausted")
        finally:
            closed.append(True)
    async def succeeding(body, request_id):
        yield b"second"
    monkeypatch.setattr(first.runtime, "stream_chat_completion", failing)
    monkeypatch.setattr(second.runtime, "stream_chat_completion", succeeding)
    output = []
    if sent:
        with pytest.raises(ProviderError):
            async for chunk in pool.stream_chat_completion(BODY, "stream"):
                output.append(chunk)
        assert output == [b"first"]
        assert second.last_selected == 0
    else:
        output = [chunk async for chunk in pool.stream_chat_completion(BODY, "stream")]
        assert output == [b"second"]
    assert closed == [True]
    assert first.in_flight == second.in_flight == 0


@pytest.mark.asyncio
async def test_cancelled_stream_closes_source_and_releases_slot(pool, settings, monkeypatch):
    settings.claude_balance = "ordered"
    first = accounts(pool)[0]
    closed = []
    async def stream(body, request_id):
        try:
            yield b"first"
            await asyncio.Future()
        finally:
            closed.append(True)
    monkeypatch.setattr(first.runtime, "stream_chat_completion", stream)
    output = pool.stream_chat_completion(BODY, "cancel")
    assert await anext(output) == b"first"
    await output.aclose()
    assert closed == [True]
    assert first.in_flight == 0


@pytest.mark.asyncio
async def test_balanced_concurrent_requests_use_available_accounts(pool, settings, monkeypatch):
    settings.claude_max_concurrent_requests = 1
    started = []
    release = asyncio.Event()
    for account in accounts(pool):
        async def generate(request, request_id, account=account):
            started.append(account.runtime.profile.slug)
            await release.wait()
            return {"result": "ok"}
        monkeypatch.setattr(account.runtime, "_run_json", generate)
        usage(account, weekly=len(started))
    tasks = [asyncio.create_task(pool.create_chat_completion(BODY, str(i))) for i in range(2)]
    try:
        async with asyncio.timeout(2):
            while len(started) < 2:
                await asyncio.sleep(0.001)
        assert len(set(started)) == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)


def test_same_subscription_is_not_counted_twice(settings):
    add(settings, "same@example.com")
    add(settings, "same@example.com")
    rows = ClaudeAccountPool(settings).status()["accounts"]
    assert len(rows) == 2
    assert sum(bool(row["ready_for_requests"]) for row in rows) == 1
    assert sum(bool(row.get("duplicate_of")) for row in rows) == 1


def test_signed_out_profile_is_neither_a_mismatch_nor_a_duplicate(settings, monkeypatch):
    ready = add(settings, "same@example.com")
    signed_out = add(settings, "same@example.com")
    pool = ClaudeAccountPool(settings)
    monkeypatch.setattr(ClaudeCliRuntime, "_cached_probe", lambda self: (
        {"installed": True, "logged_in": False} if self.profile.slug == signed_out.slug
        else {"installed": True, "logged_in": True, "email": "same@example.com"}
    ))
    rows = {row["slug"]: row for row in pool.status()["accounts"]}
    assert rows[ready.slug]["ready_for_requests"] is True
    stale = rows[signed_out.slug]
    assert stale["ready_for_requests"] is False
    assert stale["identity_mismatch"] is False
    assert "duplicate_of" not in stale and "served_by" not in stale


def test_browser_profile_wins_over_setup_token_for_one_subscription(settings, monkeypatch):
    profile = add(settings, "same@example.com")
    settings.write_claude_oauth_token("stored")
    monkeypatch.setattr(ClaudeCliRuntime, "_cached_probe", lambda self: {
        "installed": True, "logged_in": True, "email": "same@example.com",
    })
    rows = {row["slug"]: row for row in ClaudeAccountPool(settings).status()["accounts"]}
    assert rows[profile.slug]["ready_for_requests"] is True
    assert rows["default"]["ready_for_requests"] is False
    assert rows["default"]["duplicate_of"] == profile.slug


def test_signed_in_profile_with_unreadable_identity_stays_unroutable(pool, monkeypatch):
    account = accounts(pool)[0]
    monkeypatch.setattr(account.runtime, "_cached_probe", lambda: {"installed": True, "logged_in": True})
    status = account.runtime.status()
    assert status["identity_mismatch"] is False
    assert status["ready_for_requests"] is False


def test_reauth_suspends_routing_until_successful_enrollment(pool):
    account = accounts(pool)[0]
    profile = account.runtime.profile
    suspend_profile(profile)
    rows = pool.status()["accounts"]
    assert not next(row for row in rows if row["slug"] == profile.slug)["ready_for_requests"]
    enroll_profile(profile, {"logged_in": True, **profile.identity})
    rows = pool.status()["accounts"]
    assert next(row for row in rows if row["slug"] == profile.slug)["ready_for_requests"]


def test_relogin_replaces_runtime_and_clears_credential_cooldown(pool):
    first = accounts(pool)[0]
    first.blocked_until = time.time() + 300
    profile = first.runtime.profile
    enroll_profile(profile, {"logged_in": True, **profile.identity})
    pool.refresh_if_changed()
    assert pool._accounts[profile.slug] is not first
    assert pool._accounts[profile.slug].blocked_until == 0


def test_account_status_api_lists_independent_usage_failures(settings):
    add(settings, "one@example.com")
    add(settings, "two@example.com")
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/subscription/status?provider=claude&all_accounts=true")
        assert response.status_code == 200
        rows = response.json()["accounts"]
        assert len(rows) == 2
        assert all("error" in row for row in rows)
        assert client.get("/v1/subscription/status?provider=claude&account=unknown").status_code == 404


@pytest.mark.parametrize("detail,expected", [
    ("API Error: 401 invalid bearer token", 401),
    ("API Error: 429 rate_limit_error", 429),
    ("You've hit your limit", 429),
    ("invalid_request_error: prompt is too long", 422),
    ("overloaded_error", 503),
    # The request's own fault: never an account problem.
    ("API Error: 400 Unable to download the file. Please verify the URL and try again.", 422),
    ("API Error: 413 request_too_large", 413),
    ("API Error: 404 not_found_error model: claude-x", 404),
    ("API Error: 529 Overloaded", 503),
    ("Not logged in · Please run /login", 401),
    ("something unexpected", 502),
])
def test_cli_errors_keep_account_vs_request_failure_distinction(detail, expected):
    assert _claude_failure(detail).status_code == expected


def test_cli_has_additive_login_and_targeted_logout():
    parser = build_parser()
    assert parser.parse_args(["claude", "login"]).replace is None
    assert parser.parse_args(["claude", "login", "--replace", "work@example.com"]).replace == "work@example.com"
    args = parser.parse_args(["claude", "logout", "work@example.com", "--json"])
    assert args.account == "work@example.com" and args.json
    assert parser.parse_args(["claude", "accounts", "--json"]).json
    with pytest.raises(SystemExit):
        parser.parse_args(["claude", "logout", "work@example.com", "--all"])


def test_cli_login_adds_isolated_profiles_without_replacing_legacy_token(settings, monkeypatch):
    from airelay import cli

    settings.write_claude_oauth_token("legacy-token")
    monkeypatch.setattr(cli, "_base_settings", lambda args: settings)
    calls = []
    email = ["first@example.com"]
    def probe(self):
        return {"installed": True, "logged_in": self.profile.managed,
                "email": self.profile.identity.get("email") or (email[0] if self.profile.managed else None)}
    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(ClaudeCliRuntime, "_run_status_command", probe)
    monkeypatch.setattr("subprocess.run", run)
    for address in ("first@example.com", "second@example.com"):
        email[0] = address
        cli._run_claude_login(build_parser().parse_args(["claude", "login"]))
    profiles = discover_profiles(settings)
    assert len(profiles) == 3
    assert {p.identity.get("email") for p in profiles if p.managed} == {"first@example.com", "second@example.com"}
    assert calls[0][1]["CLAUDE_CONFIG_DIR"] != calls[1][1]["CLAUDE_CONFIG_DIR"]
    assert all("CLAUDE_CODE_OAUTH_TOKEN" not in env for _, env in calls)
    assert settings.resolve_claude_oauth_token() == "legacy-token"


def test_targeted_cli_logout_keeps_other_profiles_and_legacy_token(settings, monkeypatch):
    from airelay import cli

    first = add(settings, "first@example.com")
    second = add(settings, "second@example.com")
    settings.write_claude_oauth_token("legacy-token")
    monkeypatch.setattr(cli, "_base_settings", lambda args: settings)
    calls = []
    def run(command, **kwargs):
        calls.append(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("subprocess.run", run)
    cli._run_claude_logout(build_parser().parse_args(["claude", "logout", first.slug, "--json"]))
    assert calls == [str(first.config_dir)]
    assert (second.root / "account.json").exists()
    assert not (first.root / "account.json").exists()
    assert settings.resolve_claude_oauth_token() == "legacy-token"


def test_failed_cli_logout_retains_profile(settings, monkeypatch):
    from airelay import cli

    profile = add(settings, "first@example.com")
    monkeypatch.setattr(cli, "_base_settings", lambda args: settings)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="keychain locked"))
    with pytest.raises(SystemExit, match="keychain locked"):
        cli._run_claude_logout(build_parser().parse_args(["claude", "logout", profile.slug]))
    assert (profile.root / "account.json").exists()


def test_wrong_account_during_reauth_stays_unroutable(settings, monkeypatch):
    from airelay import cli

    profile = add(settings, "original@example.com")
    monkeypatch.setattr(cli, "_base_settings", lambda args: settings)
    monkeypatch.setattr(ClaudeCliRuntime, "_run_status_command", lambda self: {
        "installed": True, "logged_in": self.profile.managed, "email": "wrong@example.com",
    })
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    with pytest.raises(SystemExit, match="different Claude account"):
        cli._run_claude_login(build_parser().parse_args(["claude", "login", "--replace", profile.slug]))
    refreshed = next(p for p in discover_profiles(settings) if p.slug == profile.slug)
    assert refreshed.identity["email"] == "original@example.com"
    assert refreshed.identity["reauth_required"] is True
    assert not ClaudeAccountPool(settings).status()["ready_for_requests"]


def test_setup_token_account_is_relay_configuration_only(settings, monkeypatch, capsys):
    """Browser renewal is for browser profiles; signing out the setup token
    deletes the stored file and runs no CLI command at all."""
    from airelay import cli

    settings.write_claude_oauth_token("legacy-token")
    monkeypatch.setattr(cli, "_base_settings", lambda args: settings)
    calls = []
    monkeypatch.setattr("subprocess.run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(SystemExit, match="set-token"):
        cli._run_claude_login(build_parser().parse_args(["claude", "login", "--replace", "default"]))
    cli._run_claude_logout(build_parser().parse_args(["claude", "logout", "default", "--json"]))
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "slug": "default", "token_removed": True, "cli_signed_out": True, "cli_error": None,
    }
    assert calls == [] and settings.resolve_claude_oauth_token() is None
    assert discover_profiles(settings) == []
    with pytest.raises(SystemExit, match="No Claude account"):
        cli._run_claude_logout(build_parser().parse_args(["claude", "logout"]))
    assert calls == []


@pytest.mark.asyncio
async def test_real_subprocesses_use_distinct_profiles_and_scrub_token(pool, settings, tmp_path, monkeypatch):
    script = tmp_path / "fake-claude.py"
    script.write_text('''
import json, os, sys
sys.stdin.read()
print(json.dumps({"result": os.environ["CLAUDE_CONFIG_DIR"],
                  "is_error": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ}))
''')
    use_fake_cli(settings, script)
    settings.claude_balance = "round_robin"
    settings.write_claude_oauth_token("legacy-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-secret")
    results = await asyncio.gather(*(pool.create_chat_completion(BODY, str(i)) for i in range(2)))
    directories = {r["choices"][0]["message"]["content"] for r in results}
    assert directories == {str(a.runtime.profile.config_dir) for a in accounts(pool)}


@pytest.mark.asyncio
async def test_real_stream_rejection_fails_over_before_error_text_reaches_client(pool, settings, tmp_path):
    first, second = accounts(pool)
    script = tmp_path / "fake-claude-stream.py"
    script.write_text(f'''
import json, os, sys
sys.stdin.read()
if os.environ["CLAUDE_CONFIG_DIR"] == {str(first.runtime.profile.config_dir)!r}:
    print(json.dumps({{"type": "assistant", "error": "rate_limit_error", "message": {{"content": [{{"type": "text", "text": "You've hit your limit"}}]}}}}))
else:
    print(json.dumps({{"type": "result", "result": "second account", "is_error": False}}))
''')
    use_fake_cli(settings, script)
    settings.claude_balance = "ordered"
    output = b"".join([chunk async for chunk in pool.stream_chat_completion(BODY, "real-stream")])
    assert b"second account" in output
    assert b"hit your limit" not in output
    assert first.blocked_until > time.time()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [True, False])
async def test_closing_stream_reaps_real_subprocess_and_releases_semaphore(pool, settings, tmp_path, monkeypatch, chat):
    script = tmp_path / "fake-claude-slow-stream.py"
    script.write_text('''
import json, sys, time
sys.stdin.read()
print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}}), flush=True)
time.sleep(60)
''')
    use_fake_cli(settings, script)
    settings.claude_balance = "ordered"
    processes = []
    create = asyncio.create_subprocess_exec
    async def record(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    stream = (pool.stream_chat_completion(BODY, "close-real") if chat else
              pool.stream_completion({"model": "claude:sonnet", "prompt": "Hello"}, "close-real"))
    try:
        assert b"hello" in await asyncio.wait_for(anext(stream), timeout=5)
    finally:
        await asyncio.wait_for(stream.aclose(), timeout=5)
    assert len(processes) == 1 and processes[0].returncode is not None
    assert all(a.in_flight == 0 for a in accounts(pool))
    assert all(a.runtime._semaphore._value == settings.claude_max_concurrent_requests for a in accounts(pool))


@pytest.mark.asyncio
@pytest.mark.parametrize("status, verdict", [(401, True), (403, True), (401, False), (401, None)])
async def test_login_like_failures_are_checked_before_blaming_the_account(pool, settings, monkeypatch,
                                                                          status, verdict):
    """A login-like CLI failure is checked against Anthropic. A working
    sign-in after a 401 is temporary (no pause); after a 403 only that model
    pauses on that account; a rejected sign-in asks for a new sign-in (no
    timer); an unknown answer pauses the account."""
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    serve(monkeypatch, first, error=ProviderError(status, "Not logged in · Please run /login", code="provider_auth_error"))
    serve(monkeypatch, second, "second")
    checks = []

    async def check_sign_in():
        checks.append(1)
        return verdict

    monkeypatch.setattr(first.runtime, "check_sign_in", check_sign_in)
    suspended = []
    monkeypatch.setattr("airelay.claude_accounts.suspend_profile", lambda profile: suspended.append(profile.slug))
    result = await pool.create_chat_completion(BODY, "login-like")
    assert result["choices"][0]["message"]["content"] == "second" and checks == [1]
    paused_model = first.model_blocked.get(BODY["model"], 0) > time.time()
    assert paused_model is (status == 403 and verdict is True)
    assert (first.blocked_until > time.time()) is (verdict is None)
    assert suspended == ([first.runtime.profile.slug] if verdict is False else [])


@pytest.mark.asyncio
async def test_model_pause_keeps_other_models_and_explains_itself(pool, settings, monkeypatch):
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    for account in (first, second):
        serve(monkeypatch, account, error=ProviderError(403, "permission_error: model not enabled",
                                                         code="provider_auth_error"))

        async def works():
            return True

        monkeypatch.setattr(account.runtime, "check_sign_in", works)
    with pytest.raises(ProviderError) as error:
        await pool.create_chat_completion(BODY, "model-only")
    assert error.value.status_code == 404 and error.value.code == "model_unavailable"
    assert "sign-in works" in error.value.detail
    assert first.blocked_until == 0 and second.blocked_until == 0
    with pytest.raises(ProviderError) as again:
        await pool.create_chat_completion(BODY, "still-paused")
    assert again.value.code == "model_unavailable" and "is not available on this account" in again.value.detail
    other = {**BODY, "model": "claude:opus"}
    serve(monkeypatch, first, "opus works")
    assert (await pool.create_chat_completion(other, "other-model"))["choices"][0]["message"]["content"] == "opus works"


@pytest.mark.parametrize("detail, expected", [
    # A number quoted inside the message never outranks the reported status.
    ("API Error: 400 Unable to download the file: the server returned 403 Forbidden.", 422),
    ("API Error: 400 image URL returned HTTP 401", 422),
    ("Error at cli.js:401:17", 502),
])
def test_quoted_status_numbers_do_not_classify(detail, expected):
    assert _claude_failure(detail).status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, expected", [
    ({"rate_limits_available": True, "rate_limits": {}}, None),  # snapshot, not live: proves nothing
    (ProviderError(502, "API Error: 401 authentication_error: OAuth token revoked"), False),
    (ProviderError(502, "OAuth token refresh failed"), None),  # ambiguous: never suspend
])
async def test_sign_in_check_needs_live_evidence(pool, monkeypatch, answer, expected):
    runtime = accounts(pool)[0].runtime

    async def control(request, **kwargs):
        if isinstance(answer, ProviderError):
            raise answer
        return answer

    monkeypatch.setattr(runtime, "_control_request", control)
    assert await runtime.check_sign_in() is expected


@pytest.mark.asyncio
async def test_sign_in_verdict_applies_even_if_the_request_is_cancelled(pool, monkeypatch):
    first = accounts(pool)[0]
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_check():
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr(first.runtime, "check_sign_in", slow_check)
    waiter = asyncio.create_task(pool._check_sign_in(first, "cancelled"))
    await started.wait()
    waiter.cancel()
    release.set()
    await asyncio.sleep(0.05)
    assert first.blocked_until > time.time()


@pytest.mark.asyncio
async def test_request_errors_from_the_cli_never_pause_accounts(pool, settings, monkeypatch):
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    bad_url = _claude_failure("API Error: 400 Unable to download the file. Please verify the URL and try again.")
    serve(monkeypatch, first, error=bad_url)
    serve(monkeypatch, second, "must not run")
    with pytest.raises(ProviderError) as error:
        await pool.create_chat_completion(BODY, "bad-url")
    assert error.value.status_code == 422 and first.blocked_until == 0 and second.last_selected == 0


@pytest.mark.asyncio
async def test_invalid_json_answers_never_pause_or_rotate(pool, settings, monkeypatch):
    """A JSON-mode answer that is not JSON is the model's answer, not an
    account problem: no pause and no second account."""
    settings.claude_balance = "ordered"
    first, second = accounts(pool)
    serve(monkeypatch, first, error=ProviderError(502, "not JSON", code="invalid_json_output"))
    serve(monkeypatch, second, "must not run")
    with pytest.raises(ProviderError) as error:
        await pool.create_chat_completion(BODY, "bad-json")
    assert error.value.code == "invalid_json_output"
    assert first.blocked_until == 0 and second.last_selected == 0
