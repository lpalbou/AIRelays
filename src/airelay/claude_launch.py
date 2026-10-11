"""One launcher for the local `claude` CLI on every OS (ADR 0008).

Inference, control requests, status probes and `airelays claude
login/logout` all start `claude` through this module, so resolution, the
child environment, argument limits and process flags are decided in one
place. Per-OS behavior is data: path lists, environment names and flags.

- Resolution: an explicit `claude_command` (argv list) wins, then
  `claude_bin` (absolute path or bare name) searched on PATH and in known
  install locations. The result stays in memory and is never pinned to a
  versioned realpath: native installers point a stable symlink at the
  current version, and auto-updates move it.
- Windows needs a real executable. `.cmd`/`.bat` shims (npm) would run
  through cmd.exe, which re-parses API-supplied arguments, so they are
  refused with a pointer to `claude_command`.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
WORKDIR_PREFIX = "airelays-claude-"

# Environment the CLI may inherit. Everything else (relay secrets, API keys,
# an ambient CLAUDE_CONFIG_DIR or CLAUDE_CODE_OAUTH_TOKEN) stays out.
COMMON_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TMP", "TEMP",
    "USER", "LOGNAME", "SHELL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)
# Without SYSTEMROOT, Winsock cannot load its providers and a child cannot
# open a socket; the rest locate the profile, temp and Git Bash.
WINDOWS_ENV = (
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS", "CLAUDE_CODE_GIT_BASH_PATH",
)
# `claude auth login` opens a browser on the user's desktop session.
INTERACTIVE_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "BROWSER")
API_KEY_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)
POSIX_SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# Argument limits. Windows caps the whole command line at 32,767 UTF-16
# characters; Linux caps each argument at 128 KiB; every POSIX system caps
# argv plus environment at ARG_MAX.
WINDOWS_MAX_COMMAND_LINE = 32_000
LINUX_MAX_ARG_BYTES = 128 * 1024
ERROR_FILENAME_EXCED_RANGE = 206  # CreateProcess: command line too long
SYSTEM_PROMPT_FILE_FLAG = re.compile(r"--system-prompt(?:-file|\[-file\])")
HELP_TIMEOUT_SECONDS = 10.0
STALE_WORKDIR_SECONDS = 86_400


class ClaudeLaunchError(OSError):
    """The configured `claude` cannot be launched safely (e.g. a Windows
    `.cmd` shim). The message tells the user what to configure instead."""


class ArgumentsTooLong(ValueError):
    """The command line exceeds this OS's limit."""


@dataclass(frozen=True)
class ClaudeCommand:
    argv: tuple[str, ...]
    found: bool
    error: str | None = None

    @property
    def directory(self) -> str | None:
        """Where `claude` was found (the unresolved path, never a realpath)."""
        program = self.argv[0] if self.argv else ""
        return os.path.dirname(program) if self.found and os.path.isabs(program) else None


def _shim_error(path: str) -> str:
    return (
        f"`{path}` is a Windows script shim, which AIRelays will not run: cmd.exe would "
        "re-parse request text. Install Claude Code's native `claude.exe`, or set "
        '`[providers.claude].command = ["C:/path/to/node.exe", "C:/path/to/cli.js"]` '
        "(desktop app: the AIRELAYS_CLAUDE_COMMAND environment variable, as a JSON array)."
    )


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.name))


def known_dirs(environ: Mapping[str, str], home: Path, *, windows: bool) -> list[Path]:
    if windows:
        upper = {key.upper(): value for key, value in environ.items()}
        profile = Path(upper["USERPROFILE"]) if upper.get("USERPROFILE") else home
        local = Path(upper["LOCALAPPDATA"]) if upper.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return [profile / ".local" / "bin", local / "Volta" / "bin", local / "Microsoft" / "WinGet" / "Links"]
    dirs = [
        home / ".local" / "bin", home / ".claude" / "local", Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"), home / ".volta" / "bin", home / ".bun" / "bin",
    ]
    nvm = sorted((home / ".nvm" / "versions" / "node").glob("*/bin/claude"),
                 key=lambda candidate: _version_key(candidate.parent.parent), reverse=True)
    return dirs + [candidate.parent for candidate in nvm[:1]]


def _search_dirs(environ: Mapping[str, str], home: Path, *, windows: bool) -> list[Path]:
    path = next((value for key, value in environ.items() if (key.upper() if windows else key) == "PATH"), "")
    dirs = [Path(entry) for entry in path.split(";" if windows else ":") if entry]
    return dirs + known_dirs(environ, home, windows=windows)


def _find(name: str, environ: Mapping[str, str], home: Path, *, windows: bool) -> ClaudeCommand:
    dirs = _search_dirs(environ, home, windows=windows)
    if not windows:
        for directory in dirs:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return ClaudeCommand((str(candidate),), True)
        return ClaudeCommand((name,), False)
    suffix = Path(name).suffix.lower()
    if suffix in (".cmd", ".bat"):
        return ClaudeCommand((name,), False, _shim_error(name))
    executable = name if suffix in (".exe", ".com") else f"{name}.exe"
    # A real executable anywhere wins over a shim earlier on PATH.
    for directory in dirs:
        if (directory / executable).is_file():
            return ClaudeCommand((str(directory / executable),), True)
    upper = {key.upper(): value for key, value in environ.items()}
    npm = [Path(upper["APPDATA"]) / "npm"] if upper.get("APPDATA") else []
    for directory in dirs + npm:
        for extension in (".cmd", ".bat"):
            if (directory / f"{name}{extension}").is_file():
                return ClaudeCommand((name,), False, _shim_error(str(directory / f"{name}{extension}")))
    return ClaudeCommand((name,), False)


def _locate(program: str, environ: Mapping[str, str], home: Path, *, windows: bool) -> ClaudeCommand:
    if not (os.sep in program or "/" in program or (windows and "\\" in program)):
        return _find(program, environ, home, windows=windows)
    path = Path(program).expanduser()
    if windows:
        suffix = path.suffix.lower()
        if suffix in (".cmd", ".bat"):
            return ClaudeCommand((str(path),), False, _shim_error(str(path)))
        if suffix not in (".exe", ".com"):
            path = path.with_name(path.name + ".exe")
    return ClaudeCommand((str(path),), path.is_file())


def resolve_claude(
    command: Sequence[str] | None,
    claude_bin: str,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    windows: bool = WINDOWS,
) -> ClaudeCommand:
    """`claude_command` (argv) wins over `claude_bin`. Paths are kept as
    found: a symlinked `claude` is never replaced by its target."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if command:
        argv = tuple(str(part) for part in command)
        program = _locate(argv[0], environ, home, windows=windows)
        return ClaudeCommand(program.argv + argv[1:], program.found, program.error)
    return _locate(claude_bin or "claude", environ, home, windows=windows)


def child_env(
    command: ClaudeCommand,
    *,
    config_dir: Path,
    oauth_token: str | None,
    strip_api_key_env: bool,
    interactive: bool = False,
    environ: Mapping[str, str] | None = None,
    windows: bool = WINDOWS,
) -> dict[str, str]:
    """One allowlist plus per-OS names, matched case-insensitively only on
    Windows. ADR 0007: every child names its relay-owned profile; an
    inherited CLAUDE_CONFIG_DIR or CLAUDE_CODE_OAUTH_TOKEN never passes."""
    environ = os.environ if environ is None else environ
    names = COMMON_ENV + (WINDOWS_ENV if windows else ()) + (INTERACTIVE_ENV if interactive else ())
    if windows:
        allowed = {name.upper() for name in names}
        env = {key: value for key, value in environ.items() if key.upper() in allowed and key.upper() != "PATH"}
        inherited = next((value for key, value in environ.items() if key.upper() == "PATH"), "")
    else:
        allowed = set(names)
        env = {key: value for key, value in environ.items() if key in allowed and key != "PATH"}
        inherited = environ.get("PATH", "")
    separator = ";" if windows else ":"
    entries = [entry for entry in inherited.split(separator) if entry]
    if command.directory and command.directory not in entries[:1]:
        entries.insert(0, command.directory)
    if not windows:
        entries += [entry for entry in POSIX_SYSTEM_PATH if entry not in entries]
    env["PATH"] = separator.join(entries)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    if strip_api_key_env:
        for name in API_KEY_ENV:
            env.pop(name, None)
    return env


def check_argv(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    platform: str = sys.platform,
    arg_max: int | None = None,
) -> None:
    """Raise ArgumentsTooLong before the OS rejects the spawn."""
    if platform == "win32":
        if len(subprocess.list2cmdline(list(argv))) > WINDOWS_MAX_COMMAND_LINE:
            raise ArgumentsTooLong("the command line exceeds the Windows limit")
        return
    encoded = [part.encode("utf-8", "surrogateescape") for part in argv]
    if platform.startswith("linux") and any(len(part) >= LINUX_MAX_ARG_BYTES for part in encoded):
        raise ArgumentsTooLong("an argument exceeds the Linux 128 KiB limit")
    if arg_max is None:
        try:
            arg_max = os.sysconf("SC_ARG_MAX")
        except (AttributeError, ValueError, OSError):
            arg_max = 256 * 1024
    entries = encoded + [f"{key}={value}".encode("utf-8", "surrogateescape") for key, value in env.items()]
    total = sum(len(entry) + 1 + 8 for entry in entries)  # NUL + pointer
    if total > arg_max - 4096:
        raise ArgumentsTooLong("the arguments exceed this system's ARG_MAX")


def _too_long(error: OSError) -> bool:
    return getattr(error, "winerror", None) == ERROR_FILENAME_EXCED_RANGE or error.errno == errno.E2BIG


def read_help(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> str:
    """`claude --help` output, or "" when it cannot be read."""
    try:
        result = subprocess.run(
            [*argv, "--help"], env=dict(env), cwd=cwd, capture_output=True,
            encoding="utf-8", errors="replace", timeout=HELP_TIMEOUT_SECONDS, check=False,
            **process_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _terminal() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def process_flags(*, interactive: bool = False) -> dict[str, Any]:
    """Windows children start without a console window. Only a sign-in
    run from a terminal shares that terminal; the tray streams sign-in
    output through pipes and needs no console."""
    if not WINDOWS or (interactive and _terminal()):
        return {}
    return {"creationflags": CREATE_NO_WINDOW}


def _inherited_streams() -> dict[str, Any]:
    """Explicit standard handles: a CREATE_NO_WINDOW child would otherwise
    write to its own hidden console instead of the tray's pipes."""
    streams = {"stdin": sys.stdin, "stdout": sys.stdout, "stderr": sys.stderr}
    return {name: stream for name, stream in streams.items() if stream is not None}


_help_cache: dict[tuple[str, ...], bool] = {}
_swept = False


def sweep_stale_workdirs(max_age: float = STALE_WORKDIR_SECONDS) -> None:
    """Once per process: remove per-request directories a previous run could
    not delete (Windows keeps a directory busy while any process holds it)."""
    global _swept
    if _swept:
        return
    _swept = True
    cutoff = time.time() - max_age
    try:
        entries = list(os.scandir(tempfile.gettempdir()))
    except OSError:
        return
    for entry in entries:
        try:
            if (entry.name.startswith(WORKDIR_PREFIX) and entry.is_dir(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


def workdir(prefix: str = WORKDIR_PREFIX) -> tempfile.TemporaryDirectory[str]:
    # A child (or an antivirus scan) can briefly hold the directory on
    # Windows; a finished request must not fail on cleanup.
    return tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)


class ClaudeLauncher:
    """Resolution plus spawn helpers for one settings object."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._key: tuple[Any, ...] | None = None
        self._command = self.refresh()

    def _settings_key(self) -> tuple[Any, ...]:
        return (tuple(getattr(self._settings, "claude_command", None) or ()), self._settings.claude_bin)

    def refresh(self) -> ClaudeCommand:
        """Resolve again (at start, after a launch found nothing, and when
        the configured command changes)."""
        self._key = self._settings_key()
        self._command = resolve_claude(self._key[0], self._key[1])
        return self._command

    @property
    def command(self) -> ClaudeCommand:
        return self._command if self._key == self._settings_key() else self.refresh()

    def describe(self) -> str:
        return " ".join(self.command.argv)

    def argv(self, args: Sequence[str]) -> list[str]:
        """Full argv for a launch. Raises ClaudeLaunchError for a refused
        shim, and FileNotFoundError without spawning when nothing was found:
        the OS would search the same PATH."""
        command = self.command if self.command.found else self.refresh()
        if command.error:
            raise ClaudeLaunchError(command.error)
        if not command.found:
            raise FileNotFoundError(errno.ENOENT, "Claude CLI not found", command.argv[0])
        return [*command.argv, *args]

    def env(self, *, config_dir: Path, oauth_token: str | None, interactive: bool = False) -> dict[str, str]:
        return child_env(
            self.command, config_dir=config_dir, oauth_token=oauth_token,
            strip_api_key_env=self._settings.claude_strip_api_key_env, interactive=interactive,
        )

    async def spawn(self, args: Sequence[str], *, env: Mapping[str, str], cwd: Path | str,
                    **kwargs: Any) -> asyncio.subprocess.Process:
        argv = self.argv(args)
        check_argv(argv, env)
        try:
            return await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=dict(env), **process_flags(), **kwargs)
        except OSError as error:
            # Windows reports an over-long command line as "file not found".
            if _too_long(error):
                raise ArgumentsTooLong(str(error)) from error
            if isinstance(error, FileNotFoundError):
                self.refresh()
            raise

    def run(self, args: Sequence[str], *, env: Mapping[str, str], cwd: Path | str,
            timeout: float | None = None, capture: bool = True,
            interactive: bool = False) -> subprocess.CompletedProcess[Any]:
        argv = self.argv(args)
        flags = process_flags(interactive=interactive)
        if capture:
            output: dict[str, Any] = {"capture_output": True, "encoding": "utf-8", "errors": "replace"}
        else:
            output = _inherited_streams() if flags else {}
            if "stdout" in output:
                sys.stdout.flush()
        try:
            return subprocess.run(argv, env=dict(env), cwd=cwd, check=False, timeout=timeout,
                                  **output, **flags)
        except FileNotFoundError:
            self.refresh()
            raise

    async def supports_system_prompt_file(self, *, env: Mapping[str, str], cwd: Path) -> bool:
        """Detected once per resolved CLI from its help text."""
        if self.command.error or not self.command.found:
            return False
        key = self.command.argv
        if key not in _help_cache:
            text = await asyncio.to_thread(read_help, key, env, cwd)
            if not text:
                return False
            _help_cache[key] = bool(SYSTEM_PROMPT_FILE_FLAG.search(text))
        return _help_cache[key]
