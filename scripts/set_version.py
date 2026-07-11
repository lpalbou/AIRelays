#!/usr/bin/env python3
"""Sets the AIRelays version everywhere it is declared, in one command.

Usage: python scripts/set_version.py 0.6.0

Canonical sources (everything else derives at build/run time):
- relay:   src/airelay/__init__.py  __version__   (pyproject reads it via
           [tool.hatch.version]; /healthz, the landing page, and CLI titles
           read the attribute at runtime)
- desktop: desktop/src-tauri/Cargo.toml  [package] version   (tauri.conf.json
           inherits it; the window title, tray tooltip, and dashboard read
           the compiled package info at runtime)
- desktop/package.json mirrors the desktop version for npm tooling.

Lockfiles (Cargo.lock, package-lock.json) are refreshed when the matching
tool is available. The desktop release workflow's version guard enforces
that all of these agree with the desktop-vX.Y.Z tag before building.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def set_python(version: str) -> None:
    path = ROOT / "src" / "airelay" / "__init__.py"
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^__version__ = "[^"]+"', f'__version__ = "{version}"', text, flags=re.M
    )
    if count != 1:
        fail(f"expected exactly one __version__ assignment in {path}")
    path.write_text(updated, encoding="utf-8")
    print(f"  {path.relative_to(ROOT)} -> {version}")


def set_cargo(version: str) -> None:
    path = ROOT / "desktop" / "src-tauri" / "Cargo.toml"
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^version = "[^"]+"', f'version = "{version}"', text, count=1, flags=re.M
    )
    if count != 1:
        fail(f"could not update [package] version in {path}")
    path.write_text(updated, encoding="utf-8")
    print(f"  {path.relative_to(ROOT)} -> {version}")


def set_package_json(version: str) -> None:
    path = ROOT / "desktop" / "package.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = version
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"  {path.relative_to(ROOT)} -> {version}")


def refresh_locks() -> None:
    if shutil.which("cargo"):
        subprocess.run(
            ["cargo", "update", "-q", "-p", "airelays-desktop"],
            cwd=ROOT / "desktop" / "src-tauri",
            check=True,
        )
        print("  desktop/src-tauri/Cargo.lock refreshed")
    else:
        print("  WARNING: cargo not found; refresh Cargo.lock manually")
    if shutil.which("npm"):
        subprocess.run(
            ["npm", "install", "--package-lock-only", "--silent"],
            cwd=ROOT / "desktop",
            check=True,
        )
        print("  desktop/package-lock.json refreshed")
    else:
        print("  WARNING: npm not found; refresh package-lock.json manually")


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: python scripts/set_version.py X.Y.Z[-suffix]")
    version = sys.argv[1].strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.]+)?", version):
        fail(f"{version!r} does not look like a version")
    print(f"Setting AIRelays version to {version}:")
    set_python(version)
    set_cargo(version)
    set_package_json(version)
    refresh_locks()
    print("Done. Remember the CHANGELOG entry before releasing.")


if __name__ == "__main__":
    main()
