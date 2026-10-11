from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolate_model_discovery(monkeypatch):
    """Unit tests must never launch a logged-in local Claude/Codex CLI."""
    from airelay import model_discovery
    from airelay.providers import ClaudeCliRuntime, ProviderError

    async def no_catalog(self):
        return []

    async def no_cli_session(self, request, **kwargs):
        raise ProviderError(503, "Unit tests never launch a Claude CLI session.")

    monkeypatch.setattr(ClaudeCliRuntime, "_discover_models", no_catalog)
    monkeypatch.setattr(ClaudeCliRuntime, "_control_request", no_cli_session)
    monkeypatch.setattr(model_discovery, "_installed_codex", lambda: None)
    monkeypatch.setattr(model_discovery, "_version_cache", None)


@pytest.fixture(autouse=True)
def isolate_claude_token(monkeypatch):
    """Settings defaults name the real data dir. A stored setup token there
    would become a Claude account in any test that only overrides data_dir."""
    from airelay.config import Settings

    real = Settings.__dataclass_fields__["claude_oauth_token_file"].default
    resolve = Settings.resolve_claude_oauth_token
    monkeypatch.setattr(Settings, "resolve_claude_oauth_token",
                        lambda self: None if self.claude_oauth_token_file == real else resolve(self))


@pytest.fixture(autouse=True)
def isolate_claude_launcher(monkeypatch):
    """Never run the developer's `claude --help` (system-prompt-file
    detection) and never sweep the real temp directory."""
    from airelay import claude_launch

    monkeypatch.setattr(claude_launch, "read_help", lambda argv, env, cwd: "")
    # A bare `claude` never resolves to the developer's installed CLI;
    # resolver tests pass their own environment.
    real_find = claude_launch._find

    def find(name, environ, home, *, windows):
        if environ is os.environ:
            return claude_launch.ClaudeCommand((name,), False)
        return real_find(name, environ, home, windows=windows)

    monkeypatch.setattr(claude_launch, "_find", find)
    monkeypatch.setattr(claude_launch, "_help_cache", {})
    monkeypatch.setattr(claude_launch, "_swept", True)
