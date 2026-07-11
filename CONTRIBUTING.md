# Contributing

## Development Setup

Contributor setup from a local checkout:

```bash
python -m pip install -e .[dev]
pytest -q
python -m compileall src tests
```

## Project Rules

- preserve the explicit compatibility boundary
- do not silently widen support claims
- do not add truncation or hidden coercion for files, modalities, or token accounting
- prefer explicit errors over compatibility theater
- keep AIRelay auth storage independent from Codex or any other external tool state

## Versioning

The version is declared in exactly two canonical places; everything else
derives from them at build or run time:

- relay: `__version__` in `src/airelay/__init__.py` (`pyproject.toml` reads
  it via hatch's dynamic version; `/healthz`, the landing page, and CLI
  titles read the attribute at runtime)
- desktop app: `[package] version` in `desktop/src-tauri/Cargo.toml`
  (`tauri.conf.json` inherits it; the window title, tray tooltip, and
  dashboard show the compiled package version at runtime)

Bump every component in one command:

```bash
python scripts/set_version.py 0.6.0
```

The release workflows enforce that the tag, the relay version, and the
desktop manifests agree before anything publishes.

## Pull Request Expectations

- include tests for behavioral changes
- update docs when API behavior changes
- update ADRs when a durable engineering rule changes
