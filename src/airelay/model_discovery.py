"""Provider catalog discovery helpers; no generation requests are needed."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path


MIN_CODEX_CATALOG_VERSION = "0.167.2"
LEGACY_CODEX_CLIENT_VERSION = "0.124.0"
_version_lock = threading.Lock()
_version_cache: tuple[str | None, float, str] | None = None


def _installed_codex() -> str | None:
    executable = shutil.which("codex")
    if executable:
        return executable
    # Finder/desktop launchers do not load interactive shell profiles (NVM
    # commonly lives in .zshrc). Search stable install locations as well.
    home = Path.home()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        directories = [home / ".local" / "bin", appdata / "npm"]
        names = ("codex.exe", "codex.cmd")
    else:
        directories = [
            home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
            home / ".volta" / "bin", home / ".bun" / "bin",
        ]
        names = ("codex",)

    def version_key(path: Path) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", path.name))

    nodes = home / ".nvm" / "versions" / "node"
    directories.extend(path / "bin" for path in sorted(nodes.glob("*"), key=version_key, reverse=True))
    # The Codex desktop app also installs a native client, independently
    # of npm. Never read its cached catalog: it belongs to another login.
    releases = home / ".codex" / "packages" / "app-server-daemon" / "releases"
    directories.extend(path / "bin" for path in sorted(releases.glob("*"), key=version_key, reverse=True))
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def codex_catalog_version(configured: str) -> str:
    """Follow the installed CLI, with a tested floor for standalone installs.

    The former shipped default is migrated here as well, because desktop
    installations persist it to config.toml. Other explicit pins are honored.
    Call from a worker thread: resolving a CLI version can spawn a process.
    """
    if configured not in ("auto", "", LEGACY_CODEX_CLIENT_VERSION):
        return configured
    global _version_cache
    executable = _installed_codex()
    with _version_lock:
        now = time.monotonic()
        if _version_cache is not None:
            cached_executable, fetched_at, version = _version_cache
            if executable == cached_executable and now - fetched_at < 300:
                return version
        version = MIN_CODEX_CATALOG_VERSION
        if executable:
            try:
                env = dict(os.environ)
                # npm's executable uses /usr/bin/env node; finding the
                # shim is insufficient if its sibling node is off PATH.
                env["PATH"] = os.pathsep.join((str(Path(executable).parent), env.get("PATH", "")))
                result = subprocess.run(
                    [executable, "--version"], capture_output=True, text=True,
                    timeout=3, check=True, env=env,
                )
                match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
                if match:
                    candidate = match.group(1)
                    if tuple(map(int, candidate.split("."))) > tuple(map(int, version.split("."))):
                        version = candidate
            except (OSError, subprocess.SubprocessError):
                pass
        _version_cache = (executable, time.monotonic(), version)
        return version
