"""The one `claude` launcher (ADR 0008): resolution, child environment,
argument limits and process flags, checked for every OS from any OS."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from airelay import claude_launch
from airelay.claude_auth import new_profile
from airelay.claude_launch import ArgumentsTooLong, ClaudeCommand, check_argv, child_env, resolve_claude
from airelay.config import Settings
from airelay.providers import ClaudeCliRuntime, ProviderError, ProviderRegistry
from claude_fakes import use_fake_cli

# Captured before conftest stubs them for every test.
REAL_READ_HELP = claude_launch.read_help
REAL_FIND = claude_launch._find

BODY = {"model": "claude:sonnet", "messages": [
    {"role": "system", "content": "Be terse."}, {"role": "user", "content": "Hi"},
]}


@pytest.fixture(autouse=True)
def real_search(monkeypatch):
    # Resolver tests pass their own PATH and home; nothing reads the
    # developer's environment.
    monkeypatch.setattr(claude_launch, "_find", REAL_FIND)


def make_settings(tmp_path, **overrides) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs",
                        bearer_token_file=tmp_path / "data" / "relay-token",
                        claude_oauth_token_file=tmp_path / "data" / "claude-token",
                        enable_claude=True, claude_models=("claude:sonnet",))
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def touch(path, executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if executable and os.name != "nt":
        path.chmod(0o755)
    return path


# ----- resolution -----

def test_windows_refuses_cmd_shim_and_points_to_claude_command(tmp_path):
    npm = tmp_path / "npm"
    touch(npm / "claude.cmd")
    command = resolve_claude(None, "claude", environ={"PATH": str(npm)}, home=tmp_path, windows=True)
    assert not command.found
    assert command.error and "[providers.claude].command" in command.error
    explicit = resolve_claude(None, str(npm / "claude.cmd"), environ={}, home=tmp_path, windows=True)
    assert explicit.error and not explicit.found
    batch = resolve_claude([str(npm / "claude.bat")], "claude", environ={}, home=tmp_path, windows=True)
    assert batch.error and not batch.found
    # The npm directory is checked even when it is not on PATH.
    appdata = tmp_path / "AppData" / "Roaming"
    touch(appdata / "npm" / "claude.cmd")
    hidden = resolve_claude(None, "claude", environ={"APPDATA": str(appdata)}, home=tmp_path, windows=True)
    assert hidden.error and "claude.cmd" in hidden.error


def test_windows_prefers_a_real_exe_across_all_path_dirs(tmp_path):
    shim_dir, exe_dir = tmp_path / "npm", tmp_path / "native"
    touch(shim_dir / "claude.cmd")
    touch(exe_dir / "claude.exe")
    command = resolve_claude(None, "claude", environ={"Path": f"{shim_dir};{exe_dir}"}, home=tmp_path, windows=True)
    assert command.found and command.error is None
    assert command.argv == (str(exe_dir / "claude.exe"),)


def test_windows_known_install_locations(tmp_path):
    local = tmp_path / "Local"
    for relative in (("Volta", "bin"), ("Microsoft", "WinGet", "Links")):
        exe = touch(local.joinpath(*relative) / "claude.exe")
        command = resolve_claude(None, "claude", environ={"LOCALAPPDATA": str(local)}, home=tmp_path, windows=True)
        assert command.argv == (str(exe),)
        exe.unlink()
    native = touch(tmp_path / "profile" / ".local" / "bin" / "claude.exe")
    command = resolve_claude(None, "claude", environ={"USERPROFILE": str(tmp_path / "profile")},
                             home=tmp_path, windows=True)
    assert command.argv == (str(native),)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits and symlinks")
def test_symlinked_claude_is_kept_as_found_not_its_versioned_target(tmp_path):
    target = touch(tmp_path / "share" / "claude" / "versions" / "2.1.0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").symlink_to(target)
    command = resolve_claude(None, "claude", environ={"PATH": str(bin_dir)}, home=tmp_path, windows=False)
    assert command.argv == (str(bin_dir / "claude"),)
    env = child_env(command, config_dir=tmp_path / "config", oauth_token=None, strip_api_key_env=True,
                    environ={"PATH": "/usr/bin"}, windows=False)
    assert env["PATH"].split(":")[0] == str(bin_dir)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
def test_posix_known_dirs_and_newest_nvm(tmp_path):
    for version in ("v18.20.0", "v20.11.1", "v9.0.0"):
        touch(tmp_path / ".nvm" / "versions" / "node" / version / "bin" / "claude")
    command = resolve_claude(None, "claude", environ={"PATH": ""}, home=tmp_path, windows=False)
    assert command.argv == (str(tmp_path / ".nvm" / "versions" / "node" / "v20.11.1" / "bin" / "claude"),)
    local = touch(tmp_path / ".local" / "bin" / "claude")
    assert resolve_claude(None, "claude", environ={}, home=tmp_path, windows=False).argv == (str(local),)
    touch(tmp_path / "plain" / "claude", executable=False)
    assert resolve_claude(None, "claude", environ={"PATH": str(tmp_path / "plain")},
                          home=tmp_path / "nowhere", windows=False).found is False


def test_claude_command_wins_over_claude_bin(tmp_path):
    command = resolve_claude([sys.executable, "fake.py"], "claude", environ={}, home=tmp_path)
    assert command.found and command.argv == (sys.executable, "fake.py")


# ----- child environment -----

WINDOWS_NAMES = {
    "SystemRoot": r"C:\Windows", "windir": r"C:\Windows", "ComSpec": r"C:\Windows\system32\cmd.exe",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD", "USERPROFILE": r"C:\Users\u", "APPDATA": r"C:\Users\u\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "ProgramData": r"C:\ProgramData",
    "ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramW6432": r"C:\Program Files", "SystemDrive": "C:", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\u",
    "USERNAME": "u", "COMPUTERNAME": "PC", "NUMBER_OF_PROCESSORS": "8", "TEMP": r"C:\Temp", "TMP": r"C:\Temp",
    "CLAUDE_CODE_GIT_BASH_PATH": r"C:\Program Files\Git\bin\bash.exe",
    "HTTPS_PROXY": "http://proxy:3128", "NODE_EXTRA_CA_CERTS": r"C:\ca.pem", "SSL_CERT_FILE": r"C:\ca.pem",
}
SECRETS = {
    "ANTHROPIC_API_KEY": "x", "CLAUDE_CONFIG_DIR": "personal", "CLAUDE_CODE_OAUTH_TOKEN": "ambient",
    "AIRELAYS_BEARER_TOKEN": "relay-secret", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/bus",
}


def test_windows_child_env_keeps_system_variables_case_insensitively(tmp_path):
    exe = tmp_path / "native" / "claude.exe"
    command = ClaudeCommand((str(exe),), True)
    environ = {**WINDOWS_NAMES, **SECRETS, "Path": r"C:\Windows\system32", "DISPLAY": ":0"}
    env = child_env(command, config_dir=tmp_path / "config", oauth_token=None, strip_api_key_env=True,
                    environ=environ, windows=True)
    for name, value in WINDOWS_NAMES.items():
        assert env[name] == value
    assert env["PATH"] == f"{exe.parent};C:\\Windows\\system32"
    assert "Path" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")
    assert not set(SECRETS) - {"CLAUDE_CONFIG_DIR"} & set(env)
    assert "DISPLAY" not in env
    interactive = child_env(command, config_dir=tmp_path / "config", oauth_token="setup", strip_api_key_env=True,
                            environ=environ, windows=True, interactive=True)
    assert interactive["DISPLAY"] == ":0" and interactive["CLAUDE_CODE_OAUTH_TOKEN"] == "setup"
    assert "DBUS_SESSION_BUS_ADDRESS" not in interactive


def test_posix_child_env_is_case_sensitive_and_keeps_system_path(tmp_path):
    command = ClaudeCommand((str(tmp_path / "bin" / "claude"),), True)
    environ = {"SystemRoot": "x", "path": "/ignored", "PATH": "/opt/tools:/bin", "HOME": "/home/u",
               "XAUTHORITY": "/x", **SECRETS}
    env = child_env(command, config_dir=tmp_path / "config", oauth_token=None, strip_api_key_env=True,
                    environ=environ, windows=False)
    assert "SystemRoot" not in env and "path" not in env and "XAUTHORITY" not in env
    assert env["PATH"] == ":".join([str(tmp_path / "bin"), "/opt/tools", "/bin", "/usr/bin", "/usr/sbin", "/sbin"])
    assert env["HOME"] == "/home/u" and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    interactive = child_env(command, config_dir=tmp_path / "config", oauth_token=None, strip_api_key_env=True,
                            environ=environ, windows=False, interactive=True)
    assert interactive["XAUTHORITY"] == "/x" and "DBUS_SESSION_BUS_ADDRESS" not in interactive


def test_real_child_with_relay_env_can_resolve_and_open_sockets(tmp_path):
    """On Windows a child without SYSTEMROOT cannot initialize Winsock."""
    settings = make_settings(tmp_path)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    env = runtime._subprocess_env()
    if os.name == "nt":
        assert {"SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "LOCALAPPDATA"} <= {k.upper() for k in env}
    result = subprocess.run(
        [sys.executable, "-c", "import socket; socket.getaddrinfo('localhost', 80); print('ok')"],
        env=env, capture_output=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ----- argument limits -----

def test_argv_limits_per_os():
    check_argv(["claude", "x" * 1000], {}, platform="win32")
    with pytest.raises(ArgumentsTooLong):
        check_argv(["claude", "x" * 40_000], {}, platform="win32")
    with pytest.raises(ArgumentsTooLong):  # list2cmdline escapes every quote
        check_argv(["claude", '"' * 20_000], {}, platform="win32")
    check_argv(["claude", "x" * 100_000], {}, platform="linux", arg_max=2 * 1024 * 1024)
    with pytest.raises(ArgumentsTooLong):
        check_argv(["claude", "x" * (128 * 1024)], {}, platform="linux", arg_max=2 * 1024 * 1024)
    check_argv(["claude", "x" * (512 * 1024)], {}, platform="darwin", arg_max=1024 * 1024)
    with pytest.raises(ArgumentsTooLong):
        check_argv(["claude", "x" * (600 * 1024)], {"BIG": "y" * (500 * 1024)}, platform="darwin",
                   arg_max=1024 * 1024)


@pytest.mark.asyncio
async def test_oversized_system_prompt_is_a_413_and_never_launches(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    script = tmp_path / "fake-claude.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    settings = make_settings(tmp_path)
    use_fake_cli(settings, script)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    body = {**BODY, "messages": [{"role": "system", "content": "x" * (2 * 1024 * 1024)},
                                 {"role": "user", "content": "Hi"}]}
    with pytest.raises(ProviderError) as excinfo:
        await runtime.create_chat_completion(body, "req")
    assert excinfo.value.status_code == 413 and excinfo.value.code == "invalid_request_error"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_windows_command_line_too_long_is_413_not_cli_not_found(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, claude_bin=sys.executable)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))

    async def too_long(*args, **kwargs):
        error = FileNotFoundError(errno.ENOENT, "The filename or extension is too long")
        error.winerror = 206  # what CreateProcess reports for > 32,767 chars
        raise error

    monkeypatch.setattr(asyncio, "create_subprocess_exec", too_long)
    with pytest.raises(ProviderError) as excinfo:
        await runtime.create_chat_completion(BODY, "req")
    assert excinfo.value.status_code == 413
    assert "not found" not in excinfo.value.detail


@pytest.mark.asyncio
async def test_missing_cli_is_resolved_again_and_never_spawned(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, claude_bin=str(tmp_path / "missing" / "claude"))
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    spawned = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: spawned.append(a))
    with pytest.raises(ProviderError) as excinfo:
        await runtime.create_chat_completion(BODY, "req")
    assert excinfo.value.status_code == 503 and "not found" in excinfo.value.detail
    assert spawned == []
    installed = touch(tmp_path / "missing" / "claude.exe" if os.name == "nt" else tmp_path / "missing" / "claude")
    assert runtime._launcher.argv(["-p"])[0] == str(installed)


@pytest.mark.asyncio
async def test_refused_shim_reaches_the_client_with_guidance(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    refused = ClaudeCommand(("claude",), False, claude_launch._shim_error("claude.cmd"))
    monkeypatch.setattr(claude_launch, "resolve_claude", lambda *args, **kwargs: refused)
    runtime._launcher.refresh()
    with pytest.raises(ProviderError) as excinfo:
        await runtime.create_chat_completion(BODY, "req")
    assert excinfo.value.status_code == 503 and "[providers.claude].command" in excinfo.value.detail
    assert runtime._run_status_command()["installed"] is False
    assert runtime.status()["cli_error"] == refused.error


# ----- system prompt off argv -----

HELP_AND_ECHO = r'''
import json, os, stat, sys
args = sys.argv[1:]
if args == ["--help"]:
    print("  --bare   ... Explicitly provide context via: --system-prompt[-file],")
    sys.exit(0)
sys.stdin.read()
path = args[args.index("--system-prompt-file") + 1] if "--system-prompt-file" in args else None
print(json.dumps({"type": "result", "is_error": False, "result": json.dumps({
    "prompt": open(path, encoding="utf-8").read() if path else None,
    "inline": "--system-prompt" in args,
    "mode": stat.S_IMODE(os.stat(path).st_mode) if path else None,
})}))
'''


@pytest.mark.asyncio
async def test_system_prompt_travels_in_a_private_file_when_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_launch, "read_help", REAL_READ_HELP)
    script = tmp_path / "fake-claude.py"
    script.write_text(HELP_AND_ECHO, encoding="utf-8")
    settings = make_settings(tmp_path)
    use_fake_cli(settings, script)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    payload = await runtime.create_chat_completion(BODY, "req")
    echoed = json.loads(payload["choices"][0]["message"]["content"])
    assert echoed["prompt"] == "Be terse." and echoed["inline"] is False
    if os.name != "nt":
        assert echoed["mode"] == 0o600


@pytest.mark.asyncio
async def test_system_prompt_stays_inline_when_the_cli_lacks_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_launch, "read_help", lambda argv, env, cwd: "Usage: claude [options]")
    script = tmp_path / "fake-claude.py"
    script.write_text(HELP_AND_ECHO, encoding="utf-8")
    settings = make_settings(tmp_path)
    use_fake_cli(settings, script)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    echoed = json.loads((await runtime.create_chat_completion(BODY, "req"))["choices"][0]["message"]["content"])
    assert echoed["prompt"] is None and echoed["inline"] is True


# ----- Windows process flags -----

def test_windows_children_start_without_a_console(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_launch, "WINDOWS", True)
    monkeypatch.setattr(claude_launch, "_terminal", lambda: False)
    assert claude_launch.process_flags() == {"creationflags": 0x08000000}
    # The tray's sign-in streams through pipes: no console, explicit handles.
    assert claude_launch.process_flags(interactive=True) == {"creationflags": 0x08000000}
    monkeypatch.setattr(claude_launch, "_terminal", lambda: True)
    assert claude_launch.process_flags(interactive=True) == {}
    assert claude_launch.process_flags() == {"creationflags": 0x08000000}


def test_run_cli_passes_flags_and_utf8_decoding(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, claude_bin=sys.executable)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(claude_launch, "WINDOWS", True)
    monkeypatch.setattr(claude_launch, "_terminal", lambda: False)
    runtime.run_cli(["--version"], timeout=5)
    runtime.run_cli(["auth", "login"], interactive=True, capture=False)
    probe, login = calls
    assert probe["creationflags"] == 0x08000000 and probe["encoding"] == "utf-8"
    assert login["creationflags"] == 0x08000000 and login["stdout"] is sys.stdout


# ----- probes -----

def test_probe_timeout_is_unknown_not_uninstalled(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, claude_bin=sys.executable)
    runtime = ClaudeCliRuntime(settings, profile=new_profile(settings))

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("claude", 5)

    monkeypatch.setattr(runtime, "run_cli", slow)
    first = runtime._run_status_command()
    assert first["installed"] is True and first["probe_timed_out"] is True and first["logged_in"] is None
    runtime._last_probe = {"logged_in": True, "email": "me@example.com", "version": "2.1.0"}
    again = runtime._run_status_command()
    assert again["installed"] is True and again["logged_in"] is True
    assert again["email"] == "me@example.com" and again["version"] == "2.1.0"


@pytest.mark.asyncio
async def test_first_status_probe_runs_off_the_event_loop(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, enable_openai_provider=False)
    settings.write_claude_oauth_token("setup-token")
    threads = []

    def probe(self):
        threads.append(threading.current_thread() is threading.main_thread())
        self._last_probe = {"installed": True, "logged_in": True}
        return self._last_probe

    monkeypatch.setattr(ClaudeCliRuntime, "_run_status_command", probe)
    registry = ProviderRegistry(settings, openai_auth=type("Auth", (), {"status": staticmethod(lambda: {
        "ready_for_requests": False})})())
    await registry.warm_status_probes()
    assert threads == [False]
    assert registry.provider_statuses()["claude"]["ready_for_requests"] is True
    assert threads == [False]  # cached; the loop never waited on a CLI


# ----- temp directories -----

def test_stale_workdirs_are_swept_once(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_launch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(claude_launch, "_swept", False)
    old = tmp_path / "airelays-claude-old"
    fresh = tmp_path / "airelays-claude-control-new"
    other = tmp_path / "unrelated-old"
    for directory in (old, fresh, other):
        directory.mkdir()
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    os.utime(other, (two_days_ago, two_days_ago))
    claude_launch.sweep_stale_workdirs()
    assert not old.exists() and fresh.exists() and other.exists()
    os.utime(fresh, (two_days_ago, two_days_ago))
    claude_launch.sweep_stale_workdirs()  # once per process
    assert fresh.exists()
