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

## Pull Request Expectations

- include tests for behavioral changes
- update docs when API behavior changes
- update ADRs when a durable engineering rule changes
