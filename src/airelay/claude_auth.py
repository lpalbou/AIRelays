"""Claude accounts AIRelays owns. Claude Code owns all profile credentials.

Only relay-owned profiles are accounts: browser sign-ins enrolled under
data_dir and, optionally, one stored setup token. The user's own Claude Code
sign-in is never used (ADR 0007).

Profile paths are stable: moving a directory would change its macOS Keychain
identity. Only a successful CLI sign-in publishes account.json to discovery.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from airelay.config import Settings


# The setup-token account keeps the slug the pre-0007 default account used,
# so existing API callers and `logout default` keep addressing the token.
TOKEN_SLUG = "default"


@dataclass(frozen=True)
class ClaudeProfile:
    slug: str
    root: Path
    identity: dict[str, Any] = field(default_factory=dict)
    # False only for the stored setup token: inference-only, no identity.
    managed: bool = True

    @property
    def config_dir(self) -> Path:
        return self.root / "config"


def profiles_dir(settings: Settings) -> Path:
    return settings.data_dir / "claude" / "accounts"


def token_profile(settings: Settings) -> ClaudeProfile:
    return ClaudeProfile(TOKEN_SLUG, (settings.data_dir / "claude" / "token").resolve(), managed=False)


def discover_profiles(settings: Settings) -> list[ClaudeProfile]:
    profiles = [token_profile(settings)] if settings.resolve_claude_oauth_token() else []
    directory = profiles_dir(settings)
    if not directory.is_dir():
        return profiles
    for root in sorted(directory.iterdir()):
        if root.is_symlink() or not root.is_dir() or not re.fullmatch(r"[a-z0-9-]+", root.name):
            continue
        try:
            identity = json.loads((root / "account.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(identity, dict) and identity.get("version") == 1:
            profiles.append(ClaudeProfile(root.name, root.resolve(), identity))
    return profiles


def new_profile(settings: Settings) -> ClaudeProfile:
    root = profiles_dir(settings).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True, mode=0o700)
    (root / "config").mkdir(mode=0o700)
    return ClaudeProfile(root.name, root)


def account_identity(status: dict[str, Any]) -> str | None:
    if status.get("account_id"):
        return str(status["account_id"])
    email = status.get("email")
    if isinstance(email, str) and email:
        return f"{email.casefold()}:{status.get('organization_id') or ''}"
    return None


def enroll_profile(profile: ClaudeProfile, status: dict[str, Any]) -> None:
    if not profile.managed:
        raise ValueError("The setup-token account is replaced with `airelays claude set-token`.")
    if not status.get("logged_in") or not account_identity(status):
        raise ValueError("Claude did not report a signed-in account identity; the profile was not added.")
    payload = {"version": 1, "revision": uuid.uuid4().hex, **{
        key: status.get(key)
        for key in ("email", "account_id", "organization_id", "subscription_type")
    }}
    _write_identity(profile, payload)


def suspend_profile(profile: ClaudeProfile) -> None:
    """Keep an enrolled identity visible but unroutable during/after failed reauth."""
    if profile.managed:
        _write_identity(profile, {**profile.identity, "revision": uuid.uuid4().hex, "reauth_required": True})


def _write_identity(profile: ClaudeProfile, payload: dict[str, Any]) -> None:
    temporary = profile.root / f"account-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(payload, handle)
            handle.write("\n")
        os.replace(temporary, profile.root / "account.json")
    finally:
        temporary.unlink(missing_ok=True)


def forget_profile(profile: ClaudeProfile) -> None:
    """Stop routing only after the CLI has successfully logged this profile out.

    Keep the directory in place; it can contain CLI-owned state, and its path
    is part of the credential-store identity.
    """
    if profile.managed:
        (profile.root / "account.json").unlink(missing_ok=True)


def resolve_profile(profiles: list[ClaudeProfile], needle: str) -> ClaudeProfile:
    matches = [p for p in profiles if needle == p.slug or needle == p.identity.get("email")]
    if not matches:
        matches = [p for p in profiles if str(p.identity.get("email") or "").startswith(needle)]
    if len(matches) != 1:
        raise ValueError(f"Claude account `{needle}` is {'ambiguous' if matches else 'unknown'}; use its account id from `airelays claude accounts`.")
    return matches[0]
