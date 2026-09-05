from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from airelay import model_discovery
from airelay.config import Settings
from airelay.providers import ClaudeCliRuntime, ProviderError, ProviderRegistry


discover_claude_models = ClaudeCliRuntime._discover_models


def settings(tmp_path, **kwargs):
    return Settings(data_dir=tmp_path, logs_dir=tmp_path / "logs", **kwargs)


def registry(tmp_path, backend):
    record = SimpleNamespace(
        authenticated=True, account_id="a", bound_account_id="a",
        account_matches_binding=lambda: True,
    )
    return ProviderRegistry(
        settings(tmp_path, enable_claude=False),
        openai_auth=SimpleNamespace(load=lambda: record), openai_backend=backend,
    )


@pytest.mark.asyncio
async def test_backend_queries_effective_catalog_version(tmp_path, monkeypatch):
    from airelay.backend import ChatGptCodexBackend

    backend = ChatGptCodexBackend(settings(tmp_path, client_version="0.124.0"), None, None)
    captured = {}

    async def request(**kwargs):
        captured.update(kwargs)
        return {"models": [{"slug": "gpt-6-astra"}]}

    monkeypatch.setattr(backend, "_request_json", request)
    try:
        assert (await backend.list_models("probe"))["models"][0]["slug"] == "gpt-6-astra"
    finally:
        await backend.close()
    assert captured["path"] == f"/models?client_version={model_discovery.MIN_CODEX_CATALOG_VERSION}"


@pytest.mark.asyncio
async def test_claude_initializes_configured_alias_missing_from_default_picker(tmp_path, monkeypatch):
    runtime = ClaudeCliRuntime(settings(tmp_path, claude_models=("claude:fable",)))
    selectors = []

    async def initialize(selector):
        selectors.append(selector)
        if selector == "default":
            return [{"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1"}]
        return [{"value": "fable", "resolvedModel": "claude-fable-5-1"}]

    monkeypatch.setattr(runtime, "_initialize_models", initialize)
    payload = await discover_claude_models(runtime)
    assert selectors == ["default", "fable"]
    assert payload[-1] == {"value": "fable", "resolvedModel": "claude-fable-5-1"}


@pytest.mark.parametrize("configured", ["auto", "0.124.0", ""])
def test_catalog_version_migrates_old_default_and_follows_cli(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(model_discovery, "_installed_codex", lambda: "/bin/codex")

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 3
        return SimpleNamespace(stdout="codex-cli 0.160.1\n")

    monkeypatch.setattr(model_discovery.subprocess, "run", run)
    assert model_discovery.codex_catalog_version(configured) == "0.160.1"
    assert model_discovery.codex_catalog_version(configured) == "0.160.1"
    assert calls == [["/bin/codex", "--version"]]


def test_catalog_version_explicit_pin(monkeypatch):
    assert model_discovery.codex_catalog_version("0.150.0") == "0.150.0"
    assert model_discovery._version_cache is None


@pytest.mark.parametrize("output", ["codex-cli 0.100.0", "invalid"])
def test_catalog_version_uses_floor_for_old_or_invalid_cli(output, monkeypatch):
    monkeypatch.setattr(model_discovery, "_installed_codex", lambda: "/bin/codex")
    monkeypatch.setattr(model_discovery.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=output))
    assert model_discovery.codex_catalog_version("auto") == model_discovery.MIN_CODEX_CATALOG_VERSION


def test_catalog_version_missing_and_timed_out_cli(monkeypatch):
    assert model_discovery.codex_catalog_version("auto") == model_discovery.MIN_CODEX_CATALOG_VERSION
    monkeypatch.setattr(model_discovery, "_installed_codex", lambda: "/bin/codex")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 3)

    monkeypatch.setattr(model_discovery.subprocess, "run", timeout)
    assert model_discovery.codex_catalog_version("auto") == model_discovery.MIN_CODEX_CATALOG_VERSION


@pytest.mark.asyncio
async def test_catalog_refresh_discovers_new_models_and_preserves_capabilities(tmp_path):
    class Backend:
        calls = 0
        invalidations = 0

        def invalidate_models_cache(self):
            self.invalidations += 1

        async def list_models(self, request_id):
            self.calls += 1
            if self.calls == 1:
                return {"models": [{"slug": "gpt-5.5"}]}
            return {"models": [{
                "slug": name,
                "supported_reasoning_levels": [{"effort": effort} for effort in ["low", "max", "ultra"]],
                "default_reasoning_level": "max",
            } for name in ["gpt-6-astra", "gpt-5.6-luna"]]}

    backend = Backend()
    providers = registry(tmp_path, backend)
    assert (await providers.list_models("first"))["data"][0]["id"] == "gpt-5.5"
    await providers.list_models("cached")
    assert backend.calls == 1
    refreshed = await providers.list_models("refresh", refresh=True)
    assert [m["id"] for m in refreshed["data"]] == ["gpt-6-astra", "gpt-5.6-luna"]
    assert backend.invalidations == 1
    assert refreshed["data"][0]["airelays"]["reasoning"] == {
        "parameter": "reasoning_effort", "modes": ["low", "max", "ultra"], "default": "max",
    }
    assert refreshed["data"][0]["airelays"]["discovery_source"] == "upstream_catalog"
    await providers.ensure_openai_model_supported("gpt-6-astra", "admission")
    assert backend.calls == 2


@pytest.mark.asyncio
async def test_claude_discovery_resolves_aliases_and_accepts_concrete_ids(tmp_path, monkeypatch):
    runtime = ClaudeCliRuntime(settings(tmp_path))

    async def catalog():
        return [
            {"value": "fable", "resolvedModel": "claude-fable-5-1", "supportedEffortLevels": ["low", "max"]},
            {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
            {"value": "new-alias", "resolvedModel": "claude-new-6", "supportedEffortLevels": ["high"]},
            None, {"value": None},
        ]

    monkeypatch.setattr(runtime, "_discover_models", catalog)
    await runtime.refresh_models()
    records = {m["id"]: m["airelays"] for m in runtime.list_models()}
    assert records["claude:fable"]["resolved_model"] == "claude-fable-5-1"
    assert records["claude:fable"]["upstream_model"] == "fable"
    assert records["claude-fable-5-1"]["upstream_model"] == "claude-fable-5-1"
    assert records["claude:haiku"]["reasoning"]["modes"] == []
    assert runtime.resolve_model("claude:new-alias").upstream_id == "new-alias"
    assert runtime.resolve_model("claude-fable-5-1").upstream_id == "claude-fable-5-1"
    with pytest.raises(ProviderError, match="omit reasoning_effort"):
        runtime._prepare_chat_request({"model": "claude:haiku", "messages": [{"role": "user", "content": "Hi"}], "reasoning_effort": "high"})


@pytest.mark.asyncio
async def test_claude_discovery_single_flight_expiry_failure_and_removal(tmp_path, monkeypatch):
    runtime = ClaudeCliRuntime(settings(tmp_path))
    calls = 0

    async def catalog():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if calls == 2:
            raise FileNotFoundError("CLI unavailable")
        return [{"value": "fable", "resolvedModel": f"claude-fable-{calls}"}]

    monkeypatch.setattr(runtime, "_discover_models", catalog)
    await asyncio.gather(*(runtime.refresh_models() for _ in range(10)))
    assert calls == 1
    runtime._models_fetched_at -= 301
    await runtime.refresh_models()
    assert runtime.resolve_model("claude-fable-1") is not None
    assert runtime._models_error == "CLI unavailable"
    await runtime.refresh_models()
    assert calls == 2  # failures are cached as well
    await runtime.refresh_models(force=True)
    assert runtime.resolve_model("claude-fable-1") is None
    assert runtime.resolve_model("claude-fable-3") is not None
    assert runtime._models_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "cancel", "malformed"])
async def test_claude_initialize_protocol_is_bounded_and_reaps_process(tmp_path, monkeypatch, outcome):
    runtime = ClaudeCliRuntime(settings(tmp_path, claude_timeout_seconds=0.01, claude_models=()))
    writes = []
    spawned = {}

    class Process:
        returncode = None
        killed = False
        waited = False

        def __init__(self):
            self.stdin = self
            self.stdout = self

        def write(self, data):
            writes.append(json.loads(data))

        async def drain(self):
            pass

        async def readline(self):
            if outcome == "timeout":
                await asyncio.sleep(1)
            if outcome == "cancel":
                raise asyncio.CancelledError
            if outcome == "malformed":
                return b"not json\n"
            return json.dumps({"type": "control_response", "response": {
                "request_id": "models", "subtype": "success", "response": {"models": [{"value": "fable"}]},
            }}).encode() + b"\n"

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True

    process = Process()

    async def spawn(*args, **kwargs):
        spawned.update(command=args, **kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if outcome == "success":
        assert await discover_claude_models(runtime) == [{"value": "fable"}]
    else:
        error = {"timeout": TimeoutError, "cancel": asyncio.CancelledError, "malformed": ValueError}[outcome]
        with pytest.raises(error):
            await discover_claude_models(runtime)
    assert process.killed and process.waited
    assert writes == [{"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}]
    assert "--no-session-persistence" in spawned["command"]
    assert "--strict-mcp-config" in spawned["command"]
    assert spawned["command"][spawned["command"].index("--tools") + 1] == ""
