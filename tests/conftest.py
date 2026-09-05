from __future__ import annotations

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
    from airelay.providers import ClaudeCliRuntime

    async def no_catalog(self):
        return []

    monkeypatch.setattr(ClaudeCliRuntime, "_discover_models", no_catalog)
    monkeypatch.setattr(model_discovery, "_installed_codex", lambda: None)
    monkeypatch.setattr(model_discovery, "_version_cache", None)
