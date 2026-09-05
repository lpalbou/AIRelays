"""Provider catalog discovery helpers; no generation requests are needed."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time


MIN_CODEX_CATALOG_VERSION = "0.153.4"
LEGACY_CODEX_CLIENT_VERSION = "0.124.0"
_version_lock = threading.Lock()
_version_cache: tuple[str | None, float, str] | None = None


def _installed_codex() -> str | None:
    return shutil.which("codex")


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
                result = subprocess.run(
                    [executable, "--version"], capture_output=True, text=True,
                    timeout=3, check=True,
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
