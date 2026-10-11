"""Portable fake `claude` CLIs: Python scripts run by this interpreter, so
the same test runs on macOS, Linux and Windows (no shebang, no chmod)."""

from __future__ import annotations

import sys
from pathlib import Path

from airelay.config import Settings


def use_fake_cli(settings: Settings, script: Path) -> None:
    settings.claude_command = (sys.executable, str(script))
