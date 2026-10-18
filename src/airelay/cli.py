from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn

from airelay import __version__
from airelay.accounts import (
    ACCOUNTS_DIRNAME,
    OpenAiAccountPool,
    discover_slots,
    find_slot,
    keyring_era_sign_ins,
    load_manifest,
    resolve_slot,
    save_manifest,
    slug_for_account,
)
from airelay.app import create_app
from airelay.auth import AuthenticationError, AuthManager, AuthRecord, AuthStorage
from airelay.backend import ChatGptCodexBackend
from airelay.config import APP_NAME, Settings
from airelay.config import BALANCE_STRATEGIES
from airelay.providers import ClaudeCliRuntime, ProviderRegistry
from airelay.claude_auth import (
    account_identity, discover_profiles, enroll_profile,
    forget_profile, new_profile, resolve_profile, suspend_profile,
)
from airelay.private_io import InterProcessLock, LockUnsupported, ensure_private_dir, write_private
from airelay.terminal import accent, bad, bold, good, muted, warn


_FIELD_WIDTH = 18


class _NullTrafficLogger:
    def write(self, entry: dict[str, Any]) -> None:
        del entry


def _serve_command(settings: Settings) -> str:
    command = f"airelays serve --host {settings.host} --port {settings.port}"
    if not settings.require_bearer_auth:
        command += " --no-auth"
    return command


def _client_usage_payload(settings: Settings, include_token: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "base_url": settings.client_base_url(),
        "models_url": f"{settings.client_base_url()}/models",
        "requires_bearer_auth": settings.require_bearer_auth,
    }
    if settings.require_bearer_auth:
        payload.update(
            {
                "authorization_header": "Authorization: Bearer <AIRelays token>",
                "token_file": str(settings.bearer_token_file),
                "reveal_token_command": "airelays token show",
                "rotate_token_command": "airelays token rotate",
            }
        )
        if include_token:
            token = settings.resolve_bearer_token()
            if token:
                payload["relay_token"] = token
    else:
        payload["api_key_note"] = (
            "No relay token is required. If your client insists on an api_key value, "
            "any non-empty placeholder string is acceptable. Upstream ChatGPT login "
            "from `airelays login` is still required."
        )
    return payload


def _base_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_sources(
        Path(args.config).expanduser() if getattr(args, "config", None) else None
    )
    if getattr(args, "host", None):
        settings.host = args.host
    if getattr(args, "port", None) is not None:
        settings.port = args.port
    if getattr(args, "balance", None):
        settings.openai_balance = args.balance
    original_data_dir = settings.data_dir
    if getattr(args, "data_dir", None):
        settings.data_dir = Path(args.data_dir).expanduser()
        if not getattr(args, "logs_dir", None) and settings.logs_dir == original_data_dir / "logs":
            settings.logs_dir = settings.data_dir / "logs"
        if (
            not getattr(args, "bearer_token_file", None)
            and settings.bearer_token_file == original_data_dir / "relay-token"
        ):
            settings.bearer_token_file = settings.data_dir / "relay-token"
        if settings.claude_oauth_token_file == original_data_dir / "claude-token":
            settings.claude_oauth_token_file = settings.data_dir / "claude-token"
    if getattr(args, "logs_dir", None):
        settings.logs_dir = Path(args.logs_dir).expanduser()
    if getattr(args, "auth_storage", None) in ("auto", "keyring"):
        # Accepted for old scripts; sign-ins are always private files.
        settings.legacy_auth_storage = args.auth_storage
    if getattr(args, "bearer_token_file", None):
        settings.bearer_token_file = Path(args.bearer_token_file).expanduser()
    if getattr(args, "no_auth", False):
        settings.require_bearer_auth = False
        settings.auto_generate_bearer_token = False
    return settings


def _auth_manager(settings: Settings) -> AuthManager:
    # With multiple enrolled accounts, "the" manager is the first (primary)
    # one; with none or one, this is exactly today's legacy-root manager.
    slots = discover_slots(settings)
    root = slots[0].storage_root if slots else settings.data_dir
    return AuthManager(
        root,
        settings.auth_storage_mode,
        settings.issuer_base_url,
        client_id=settings.client_id,
    )


def _account_pool(settings: Settings) -> OpenAiAccountPool | None:
    slots = discover_slots(settings)
    if len(slots) <= 1:
        return None
    return OpenAiAccountPool(settings, _NullTrafficLogger(), slots=slots)  # type: ignore[arg-type]


def _provider_registry(settings: Settings, manager: AuthManager) -> ProviderRegistry:
    return ProviderRegistry(settings, openai_auth=manager, account_pool=_account_pool(settings))


def _status_payload(settings: Settings, manager: AuthManager) -> dict[str, object]:
    provider_statuses = _provider_registry(settings, manager).provider_statuses()
    auth_status = dict(provider_statuses.get("openai", {}))
    claude_status = dict(provider_statuses.get("claude", {}))
    next_steps: list[str] = []
    any_provider_ready = any(
        bool(status.get("ready_for_requests"))
        for status in provider_statuses.values()
        if status.get("enabled")
    )
    token_ready = bool(settings.resolve_bearer_token()) or not settings.require_bearer_auth
    if settings.require_bearer_auth and not token_ready:
        next_steps.append("airelays init")
    if not any_provider_ready:
        if settings.enable_openai_provider and not auth_status.get("ready_for_requests"):
            next_steps.append(_login_hint())
        if settings.enable_claude and not claude_status.get("ready_for_requests"):
            next_steps.append("airelays claude login")
            next_steps.append("airelays claude set-token")
    if any_provider_ready and token_ready:
        next_steps.append(_serve_command(settings))
    return {
        "relay": settings.summary(),
        "auth": auth_status,
        "providers": provider_statuses,
        "client": _client_usage_payload(settings),
        "next_steps": next_steps,
    }


def _login_payload(settings: Settings, record: AuthRecord) -> dict[str, object]:
    provider_statuses = _provider_registry(settings, _auth_manager(settings)).provider_statuses()
    return {
        "authenticated": record.authenticated,
        "email": record.email,
        "plan_type": record.plan_type,
        "account_id": record.account_id,
        "bearer_token_present": bool(settings.resolve_bearer_token()),
        "bearer_token_file": str(settings.bearer_token_file),
        "providers": provider_statuses,
        "client": _client_usage_payload(settings),
        "next_step": (
            "airelays init"
            if settings.require_bearer_auth and not settings.resolve_bearer_token()
            else _serve_command(settings)
        ),
    }


def _init_payload(
    settings: Settings,
    created_config: bool,
    token_created: bool,
    token_to_show: str | None,
) -> dict[str, object]:
    next_steps: list[str] = []
    if settings.enable_openai_provider:
        next_steps.append(_login_hint())
    if settings.enable_claude:
        next_steps.append("airelays claude login")
        next_steps.append("airelays claude set-token")
    next_steps.append(_serve_command(settings))
    return {
        "app_name": APP_NAME,
        "config_path": str(settings.config_path),
        "config_created": created_config,
        "bearer_token_file": str(settings.bearer_token_file),
        "bearer_token_created": token_created,
        "bearer_token_source": settings.bearer_token_source(),
        "relay_token": token_to_show,
        "client": _client_usage_payload(settings, include_token=bool(token_to_show)),
        "next_steps": next_steps,
    }


def _token_rotate_payload(settings: Settings, token: str) -> dict[str, object]:
    return {
        "bearer_token_file": str(settings.bearer_token_file),
        "bearer_token_source": settings.bearer_token_source(),
        "relay_token": token,
        "client": _client_usage_payload(settings, include_token=True),
    }


def _token_show_payload(settings: Settings) -> dict[str, object]:
    token = settings.resolve_bearer_token()
    next_steps: list[str] = []
    if not token and settings.require_bearer_auth:
        next_steps.extend(["airelays init", "airelays token rotate"])
    return {
        "bearer_token_present": bool(token),
        "bearer_token_file": str(settings.bearer_token_file),
        "bearer_token_source": settings.bearer_token_source(),
        "relay_token": token,
        "client": _client_usage_payload(settings, include_token=True),
        "next_steps": next_steps,
    }


def _check(
    name: str,
    status: str,
    message: str,
    *,
    next_steps: list[str] | None = None,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "status": status,
        "message": message,
    }
    if next_steps:
        payload["next_steps"] = next_steps
    if data:
        payload["data"] = data
    return payload


def _add_next_steps(target: list[str], steps: list[str] | None) -> None:
    if not steps:
        return
    for step in steps:
        if step not in target:
            target.append(step)


def _next_steps_from_check(check: dict[str, object]) -> list[str] | None:
    steps = check.get("next_steps")
    return steps if isinstance(steps, list) else None


def _model_slugs(payload: dict[str, Any]) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    slugs: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and slug:
            slugs.append(slug)
    return slugs


def _doctor_response_payload(model: str) -> dict[str, object]:
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Reply with exactly: ok",
                    }
                ],
            }
        ],
        "instructions": "Reply with exactly: ok.",
        "store": False,
        "tools": [],
        "stream": True,
    }


async def _doctor_payload(settings: Settings, *, skip_response: bool = False) -> dict[str, object]:
    manager = _auth_manager(settings)
    checks: list[dict[str, object]] = []
    next_steps: list[str] = []
    selected_model: str | None = None

    try:
        settings.validate_provider_guardrails()
    except RuntimeError as exc:
        check = _check(
            "config",
            "fail",
            str(exc),
            next_steps=["Fix the provider settings in the AIRelays config file."],
            data={"config_path": str(settings.config_path)},
        )
    else:
        config_exists = settings.config_path.exists()
        check = _check(
            "config",
            "pass" if config_exists else "warn",
            "Configuration file found." if config_exists else "Using built-in defaults; no config file exists.",
            next_steps=None if config_exists else ["airelays init"],
            data={
                "config_path": str(settings.config_path),
                "config_exists": config_exists,
            },
        )
    checks.append(check)
    _add_next_steps(next_steps, _next_steps_from_check(check))

    token = settings.resolve_bearer_token()
    if settings.require_bearer_auth and not token:
        check = _check(
            "relay_token",
            "fail",
            "Relay bearer auth is enabled, but no relay token is configured.",
            next_steps=["airelays init", "airelays token rotate"],
            data={"bearer_token_file": str(settings.bearer_token_file)},
        )
    elif settings.require_bearer_auth:
        check = _check(
            "relay_token",
            "pass",
            "Relay bearer token is present.",
            data={
                "bearer_token_file": str(settings.bearer_token_file),
                "bearer_token_source": settings.bearer_token_source() or "",
            },
        )
    else:
        check = _check(
            "relay_token",
            "skip",
            "Relay bearer auth is disabled for this configuration.",
        )
    checks.append(check)
    _add_next_steps(next_steps, _next_steps_from_check(check))

    provider_statuses = _provider_registry(settings, manager).provider_statuses()
    openai_status = dict(provider_statuses.get("openai", {}))
    account_entries = openai_status.get("accounts")
    if not settings.enable_openai_provider:
        check = _check(
            "openai_auth",
            "skip",
            "OpenAI subscription runtime is disabled.",
        )
        checks.append(check)
        _add_next_steps(next_steps, _next_steps_from_check(check))
    elif isinstance(account_entries, list) and len(account_entries) > 1:
        # One auth check per enrolled account: a healthy primary must not
        # mask a dead refresh token on the standby.
        for entry in account_entries:
            entry = dict(entry)
            label = entry.get("email") or entry.get("slug") or "account"
            if entry.get("ready_for_requests"):
                check = _check(
                    f"openai_auth {label}",
                    "pass",
                    "OpenAI subscription login is ready.",
                    data={"account_id": entry.get("account_id") or ""},
                )
            else:
                check = _check(
                    f"openai_auth {label}",
                    "fail",
                    "This account's login is missing, incomplete, or account-mismatched.",
                    next_steps=[_login_hint()],
                )
            checks.append(check)
            _add_next_steps(next_steps, _next_steps_from_check(check))
    else:
        if openai_status.get("ready_for_requests"):
            check = _check(
                "openai_auth",
                "pass",
                "OpenAI subscription login is ready.",
                data={
                    "email": openai_status.get("email") or "",
                    "account_id": openai_status.get("account_id") or "",
                    "storage_mode": openai_status.get("storage_mode") or "",
                },
            )
        else:
            check = _check(
                "openai_auth",
                "fail",
                "OpenAI subscription login is missing, incomplete, or account-mismatched.",
                next_steps=[_login_hint()],
                data={
                    "authenticated": bool(openai_status.get("authenticated")),
                    "credentials_present": bool(openai_status.get("credentials_present")),
                    "account_bound": bool(openai_status.get("account_bound")),
                },
            )
        checks.append(check)
        _add_next_steps(next_steps, _next_steps_from_check(check))

    backend: ChatGptCodexBackend | None = None
    models_available = False
    if not settings.enable_openai_provider:
        checks.append(_check("openai_models", "skip", "OpenAI subscription runtime is disabled."))
    elif not openai_status.get("ready_for_requests"):
        checks.append(_check("openai_models", "skip", "OpenAI model probe requires a ready OpenAI login."))
    else:
        # Prefer the account pool so the probe reflects real request routing
        # and failover — a single-backend probe on the first account fails
        # even when the pool would succeed by using another account.
        pool = _account_pool(settings)
        backend = pool or ChatGptCodexBackend(settings, manager, _NullTrafficLogger())  # type: ignore[arg-type]
        try:
            models_payload = await backend.list_models("doctor_models")
            slugs = _model_slugs(models_payload)
            selected_model = slugs[0] if slugs else None
            models_available = bool(selected_model)
            checks.append(
                _check(
                    "openai_models",
                    "pass" if models_available else "fail",
                    (
                        f"Upstream /models returned {len(slugs)} model(s)."
                        if models_available
                        else "Upstream /models returned no usable model slugs."
                    ),
                    data={
                        "model_count": len(slugs),
                        "selected_model": selected_model or "",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            check = _check(
                "openai_models",
                "fail",
                f"OpenAI upstream /models probe failed: {exc}",
                next_steps=[_login_hint()],
            )
            checks.append(check)
            _add_next_steps(next_steps, _next_steps_from_check(check))

    if skip_response:
        checks.append(
            _check("openai_response", "skip", "Response smoke probe skipped by --skip-response.")
        )
    elif not settings.enable_openai_provider:
        checks.append(
            _check("openai_response", "skip", "OpenAI subscription runtime is disabled.")
        )
    elif not openai_status.get("ready_for_requests"):
        checks.append(
            _check(
                "openai_response",
                "skip",
                "Response smoke probe requires a ready OpenAI login.",
            )
        )
    elif not models_available or selected_model is None:
        checks.append(
            _check(
                "openai_response",
                "skip",
                "Response smoke probe requires a usable model from /models.",
            )
        )
    else:
        if backend is None:
            backend = _account_pool(settings) or ChatGptCodexBackend(  # type: ignore[arg-type]
                settings, manager, _NullTrafficLogger()
            )
        try:
            response_payload = await backend.collect_response(
                _doctor_response_payload(selected_model),
                "doctor_response",
                None,
            )
            checks.append(
                _check(
                    "openai_response",
                    "pass",
                    "Tiny /responses smoke request completed.",
                    data={
                        "response_id": response_payload.get("id") or "",
                        "status": response_payload.get("status") or "",
                        "model": response_payload.get("model") or selected_model,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            check = _check(
                "openai_response",
                "fail",
                f"OpenAI /responses smoke request failed: {exc}",
                next_steps=["Check `airelays status` and retry after upstream limits reset if needed."],
            )
            checks.append(check)
            _add_next_steps(next_steps, _next_steps_from_check(check))
    if backend is not None:
        await backend.close()
    keyring_era = keyring_era_sign_ins(settings) if settings.enable_openai_provider else []
    if keyring_era:
        check = _check(
            "openai_keyring_sign_ins",
            "warn",
            "OpenAI sign-ins saved in the OS keychain by an AIRelays version before 0.16.1 "
            "are no longer read. Sign in again to use them.",
            next_steps=[_login_hint()],
            data={"accounts": ", ".join(keyring_era)},
        )
        checks.append(check)
        _add_next_steps(next_steps, _next_steps_from_check(check))

    claude_status = dict(provider_statuses.get("claude", {}))
    if not settings.enable_claude:
        checks.append(_check("claude", "skip", "The Claude runtime is disabled."))
    elif claude_status.get("ready_for_requests"):
        checks.append(
            _check(
                "claude",
                "pass",
                "The Claude runtime is ready.",
                data={
                    "cli_version": claude_status.get("cli_version") or "",
                    "cli_path": claude_status.get("cli_path") or "",
                    "auth_method": claude_status.get("auth_method") or "",
                    "email": claude_status.get("email") or "",
                },
            )
        )
    else:
        check = _check(
            "claude",
            "fail",
            "The Claude runtime is enabled but not ready.",
            next_steps=["airelays claude login", "airelays claude set-token"],
            data={
                "cli_version": claude_status.get("cli_version") or "",
                "cli_path": claude_status.get("cli_path") or "",
                "auth_method": claude_status.get("auth_method") or "",
            },
        )
        checks.append(check)
        _add_next_steps(next_steps, _next_steps_from_check(check))
    if settings.enable_claude and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        checks.append(_check(
            "claude_env_token",
            "warn",
            "CLAUDE_CODE_OAUTH_TOKEN is set but ignored: AIRelays only uses its own Claude accounts.",
            next_steps=["airelays claude set-token"],
        ))

    failed = [check for check in checks if check.get("status") == "fail"]
    warned = [check for check in checks if check.get("status") == "warn"]
    passed = [check for check in checks if check.get("status") == "pass"]
    skipped = [check for check in checks if check.get("status") == "skip"]
    return {
        "ok": not failed,
        "summary": {
            "passed": len(passed),
            "warnings": len(warned),
            "failed": len(failed),
            "skipped": len(skipped),
        },
        "checks": checks,
        "next_steps": next_steps,
    }


def _json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _print_title(title: str) -> None:
    # Brand-prefixed titles always carry the running version, so terminal
    # output shows exactly which relay produced it.
    if title == APP_NAME or title.startswith(f"{APP_NAME} "):
        title = f"{APP_NAME} {__version__}{title[len(APP_NAME):]}"
    print(accent(title))


def _print_section(title: str) -> None:
    print()
    print(bold(title))


def _print_field(label: str, value: object, *, kind: str = "plain") -> None:
    if value is None or value == "":
        rendered = muted("not set")
    else:
        text = str(value)
        if kind == "good":
            rendered = good(text)
        elif kind == "warn":
            rendered = warn(text)
        elif kind == "bad":
            rendered = bad(text)
        else:
            rendered = text
    print(f"  {muted(f'{label}:'.ljust(_FIELD_WIDTH))} {rendered}")


def _print_bool(label: str, value: bool, *, true_text: str = "yes", false_text: str = "no") -> None:
    if value:
        _print_field(label, true_text, kind="good")
    else:
        _print_field(label, false_text, kind="bad")


def _print_command(label: str, command: str) -> None:
    _print_field(label, command)


def _print_multiline(label: str, lines: list[str]) -> None:
    print(f"  {muted(f'{label}:'.ljust(_FIELD_WIDTH))}")
    for line in lines:
        print(f"    {line}")


def _print_steps(steps: list[str]) -> None:
    if not steps:
        return
    _print_section("Next Steps")
    for index, step in enumerate(steps, start=1):
        print(f"  {accent(f'{index}.')} {step}")


def _print_client_section(client: dict[str, object], *, include_token: bool = False) -> None:
    _print_section("Client Setup")
    _print_field("Base URL", client.get("base_url"))
    _print_field("Models URL", client.get("models_url"))
    requires_auth = bool(client.get("requires_bearer_auth"))
    _print_bool("Bearer auth", requires_auth, true_text="required", false_text="disabled")
    if requires_auth:
        _print_field("Token file", client.get("token_file"))
        _print_field("Auth header", client.get("authorization_header"))
        if include_token:
            _print_field("Relay token", client.get("relay_token"))
        else:
            _print_command("Reveal token", str(client.get("reveal_token_command")))
        _print_command("Rotate token", str(client.get("rotate_token_command")))
    else:
        _print_field("Access mode", "open", kind="warn")
        _print_field("Client key", "optional placeholder only", kind="warn")
        _print_field("SDK note", client.get("api_key_note"))


def _is_headless_environment() -> bool:
    """Best-effort detection of a machine without a usable local browser.

    SSH sessions count as headless even when X forwarding sets DISPLAY —
    a forwarded browser is rare and the device flow works there anyway.
    Misfires are safe: the device flow also works on a desktop.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def _login_hint() -> str:
    return "airelays login --device" if _is_headless_environment() else "airelays login"


def _print_login_prompt(url: str) -> None:
    _print_title("AIRelays Login")
    print("Open this URL in a browser ON THIS MACHINE (pick the profile you want):")
    print(f"  {url}")
    print()
    print("  The URL will NOT work from another computer: after sign-in it")
    print("  redirects to localhost:1455 on the machine running the browser.")
    print("  From a remote machine, either run `airelays login --device`, or")
    print("  tunnel first: ssh -L 1455:localhost:1455 user@this-server")


def _print_device_prompt(verification_url: str, user_code: str) -> None:
    _print_title("AIRelays Device Login")
    print("Sign in from a browser on ANY device (phone or laptop):")
    print()
    print(f"  1. Open:            {verification_url}")
    print(f"  2. Enter this code: {bold(user_code)}")
    print()


_last_waiting_notice = 0.0


def _print_device_waiting(remaining_seconds: float) -> None:
    """Heartbeat during device-code polling so the terminal never looks hung."""
    global _last_waiting_notice
    import time as _time

    now = _time.monotonic()
    if now - _last_waiting_notice < 15:
        return
    _last_waiting_notice = now
    minutes, seconds = divmod(int(remaining_seconds), 60)
    print(f"  Waiting for approval... (expires in {minutes}:{seconds:02d}, Ctrl-C to cancel)")


def _print_login_summary(payload: dict[str, object]) -> None:
    print()
    _print_section("OpenAI Session")
    _print_bool("Authenticated", bool(payload.get("authenticated")))
    _print_field("Email", payload.get("email"))
    _print_field("Plan", payload.get("plan_type"))
    _print_field("Account ID", payload.get("account_id"))
    client = dict(payload.get("client", {}))
    if bool(client.get("requires_bearer_auth")):
        _print_bool(
            "Relay token",
            bool(payload.get("bearer_token_present")),
            true_text="present",
            false_text="missing",
        )
        _print_field("Token file", payload.get("bearer_token_file"))
    else:
        _print_field("Bearer auth", "disabled", kind="warn")
    note = payload.get("added_account_note")
    if note:
        print()
        print(f"  {good(str(note))}")
    _print_client_section(client)
    steps = payload.get("next_steps")
    if isinstance(steps, list) and steps:
        _print_steps([str(step) for step in steps])
    elif payload.get("next_step"):
        _print_steps([str(payload["next_step"])])


def _print_init_summary(payload: dict[str, object]) -> None:
    _print_title("AIRelays Init")
    _print_section("Relay")
    client = dict(payload.get("client", {}))
    requires_auth = bool(client.get("requires_bearer_auth"))
    config_status = "created" if payload.get("config_created") else "existing"
    _print_field("App", payload.get("app_name"))
    _print_field("Config file", f"{payload.get('config_path')} ({config_status})")
    if requires_auth:
        token_status = "created" if payload.get("bearer_token_created") else "existing"
        _print_field("Relay token", token_status, kind="good")
        _print_field("Token source", payload.get("bearer_token_source"))
        _print_field("Token file", payload.get("bearer_token_file"))
    else:
        _print_field("Bearer auth", "disabled", kind="warn")
    _print_field("Client base URL", client.get("base_url"))
    _print_client_section(client, include_token=bool(payload.get("relay_token")))
    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_status_summary(payload: dict[str, object]) -> None:
    relay = dict(payload.get("relay", {}))
    auth = dict(payload.get("auth", {}))
    providers = dict(payload.get("providers", {}))
    client = dict(payload.get("client", {}))

    _print_title("AIRelays Status")
    _print_section("Relay")
    _print_field("Config file", relay.get("config_path"))
    _print_bool("Config exists", bool(relay.get("config_exists")))
    _print_field("Base URL", relay.get("client_base_url"))
    _print_field("Data dir", relay.get("data_dir"))
    _print_field("Logs dir", relay.get("logs_dir"))
    _print_bool(
        "Bearer auth",
        bool(relay.get("require_bearer_auth")),
        true_text="enabled",
        false_text="disabled",
    )
    if bool(relay.get("require_bearer_auth")):
        _print_bool(
            "Relay token",
            bool(relay.get("bearer_token_present")),
            true_text="present",
            false_text="missing",
        )
        _print_field("Token source", relay.get("bearer_token_source"))
        _print_field("Token file", relay.get("bearer_token_file"))
    else:
        _print_field("Relay token", "not required", kind="warn")
    _print_field("Rate limit", f"{relay.get('rate_limit_per_minute')}/min + burst {relay.get('rate_limit_burst')}")
    _print_field("Concurrent/IP", relay.get("concurrent_requests_per_ip"))

    openai_provider = dict(providers.get("openai", {}))
    account_entries = openai_provider.get("accounts")
    if isinstance(account_entries, list) and len(account_entries) > 1:
        _print_section("OpenAI Accounts")
        _print_field("Enabled", "yes" if openai_provider.get("enabled") else "no")
        for index, entry in enumerate(account_entries):
            entry = dict(entry)
            _print_field(f"Account {index + 1}", entry.get("email") or entry.get("slug"))
            _print_bool("  Ready", bool(entry.get("ready_for_requests")))
            _print_field("  Plan", entry.get("plan_type"))
            if entry.get("limited"):
                _print_field(
                    "  Limited",
                    f"resets in {entry.get('limited_for_seconds', '?')}s",
                    kind="warn",
                )
            _print_field("  Auth store", entry.get("auth_store_path"))
    else:
        _print_section("OpenAI Session")
        _print_field("Enabled", "yes" if openai_provider.get("enabled") else "no")
        if openai_provider.get("enabled"):
            _print_bool("Ready", bool(auth.get("ready_for_requests")))
            _print_bool("Authenticated", bool(auth.get("authenticated")))
            _print_bool("Account bound", bool(auth.get("account_bound")))
            _print_field("Email", auth.get("email"))
            _print_field("Plan", auth.get("plan_type"))
            _print_field("Account ID", auth.get("account_id"))
            _print_field("Last refresh", auth.get("last_refresh"))
            _print_field("Storage mode", auth.get("storage_mode"))
            _print_field("Auth store", auth.get("auth_store_path"))

    _print_section("Providers")
    _print_field("OpenAI enabled", "yes" if openai_provider.get("enabled") else "no")
    _print_field(
        "OpenAI ready",
        "yes" if openai_provider.get("ready_for_requests") else "no",
        kind="good" if openai_provider.get("ready_for_requests") else "warn",
    )
    claude_provider = dict(providers.get("claude", {}))
    _print_field("Claude enabled", "yes" if claude_provider.get("enabled") else "no")
    if claude_provider.get("enabled"):
        _print_field(
            "Claude ready",
            "yes" if claude_provider.get("ready_for_requests") else "no",
            kind="good" if claude_provider.get("ready_for_requests") else "warn",
        )
        _print_field("Claude CLI", claude_provider.get("cli_version"))
        _print_field("Claude CLI path", claude_provider.get("cli_path"))
        _print_field("Claude email", claude_provider.get("email"))
        _print_field("Claude auth", claude_provider.get("auth_method"))

    _print_client_section(client)
    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_doctor_summary(payload: dict[str, object]) -> None:
    _print_title("AIRelays Doctor")
    summary = dict(payload.get("summary", {}))
    _print_section("Summary")
    _print_bool("Ready", bool(payload.get("ok")))
    _print_field("Passed", summary.get("passed", 0), kind="good")
    _print_field("Warnings", summary.get("warnings", 0), kind="warn")
    _print_field("Failed", summary.get("failed", 0), kind="bad" if summary.get("failed") else "plain")
    _print_field("Skipped", summary.get("skipped", 0))

    _print_section("Checks")
    for item in payload.get("checks", []):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", ""))
        if status == "pass":
            kind = "good"
        elif status == "fail":
            kind = "bad"
        elif status == "warn":
            kind = "warn"
        else:
            kind = "plain"
        _print_field(str(item.get("name", "check")), status, kind=kind)
        message = item.get("message")
        if message:
            print(f"    {message}")

    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_logout_summary(
    deleted: bool, signed_out: list[str] | None = None, remaining: list[str] | None = None
) -> None:
    _print_title("AIRelays Logout")
    if not deleted:
        _print_field("OpenAI session", "already empty", kind="warn")
        _print_steps([_login_hint()])
        return
    _print_field("Signed out", ", ".join(signed_out or []) or "OpenAI session", kind="good")
    if remaining:
        # Remaining accounts still serve requests; do NOT tell the user to
        # log in again.
        if len(remaining) == 1:
            _print_field("Remaining", f"{remaining[0]} (now serves all requests)")
        else:
            _print_field("Remaining", ", ".join(remaining))
    else:
        _print_steps([_login_hint()])


def _print_token_rotate_summary(payload: dict[str, object]) -> None:
    _print_title("AIRelays Token Rotation")
    _print_section("Relay")
    _print_field("Token file", payload.get("bearer_token_file"))
    _print_field("Token source", payload.get("bearer_token_source"))
    _print_client_section(dict(payload.get("client", {})), include_token=True)
    _print_steps(["Update any clients that still use the old relay token."])


def _print_token_show_summary(payload: dict[str, object]) -> None:
    _print_title("AIRelays Token")
    _print_section("Relay")
    client = dict(payload.get("client", {}))
    if bool(client.get("requires_bearer_auth")):
        _print_bool(
            "Relay token",
            bool(payload.get("bearer_token_present")),
            true_text="present",
            false_text="missing",
        )
    else:
        _print_field("Relay token", "not required", kind="warn")
    _print_field("Token source", payload.get("bearer_token_source"))
    _print_field("Token file", payload.get("bearer_token_file"))
    _print_client_section(client, include_token=bool(payload.get("relay_token")))
    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_serve_banner(settings: Settings, provider_statuses: dict[str, object]) -> None:
    _print_title("AIRelays Server")
    _print_section("Listener")
    _print_field("Base URL", settings.client_base_url())
    _print_field("Host", settings.host)
    _print_field("Port", settings.port)
    if settings.require_bearer_auth:
        _print_section("Client Auth")
        _print_field("Bearer auth", "required", kind="good")
        _print_field("Token source", settings.bearer_token_source() or "missing")
        _print_field("Token file", settings.bearer_token_file)
        _print_field("Auth header", "Authorization: Bearer <AIRelays token>")
        _print_command("Reveal token", "airelays token show")
    else:
        _print_section("Client Auth")
        _print_field("Bearer auth", "disabled", kind="warn")
        _print_field("Access mode", "open", kind="warn")
        _print_field("Client key", "optional placeholder only", kind="warn")
    _print_section("Providers")
    openai_provider = dict(provider_statuses.get("openai", {}))
    _print_field("OpenAI", "enabled" if openai_provider.get("enabled") else "disabled")
    if openai_provider.get("enabled"):
        _print_field(
            "OpenAI ready",
            "yes" if openai_provider.get("ready_for_requests") else "no",
            kind="good" if openai_provider.get("ready_for_requests") else "warn",
        )
        _print_field(
            "ChatGPT login",
            "ready" if openai_provider.get("ready_for_requests") else "missing",
            kind="good" if openai_provider.get("ready_for_requests") else "warn",
        )
    claude_provider = dict(provider_statuses.get("claude", {}))
    _print_field("Claude", "enabled" if claude_provider.get("enabled") else "disabled")
    if claude_provider.get("enabled"):
        _print_field("Claude mode", "local CLI adapter")
        _print_field(
            "Claude ready",
            "yes" if claude_provider.get("ready_for_requests") else "no",
            kind="good" if claude_provider.get("ready_for_requests") else "warn",
        )
        _print_field("Claude CLI", claude_provider.get("cli_version"))
        _print_field("Claude CLI path", claude_provider.get("cli_path"))
    if openai_provider.get("enabled") and not openai_provider.get("ready_for_requests"):
        _print_command("OpenAI login", _login_hint())
    if claude_provider.get("enabled") and not claude_provider.get("ready_for_requests"):
        _print_command("Claude login", "airelays claude login")
        _print_command("Claude headless", "airelays claude set-token")
    print()


async def _run_login(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    if not settings.enable_openai_provider:
        raise SystemExit(
            "The OpenAI subscription runtime is disabled for this AIRelays process. "
            "Enable `[providers.openai].enabled` to use `airelays login`."
        )
    existing_slots = discover_slots(settings)
    replace_slot = None
    if getattr(args, "replace", None):
        replace_slot = find_slot(existing_slots, args.replace)
        if replace_slot is None:
            known = ", ".join(slot.label for slot in existing_slots) or "none"
            raise SystemExit(f"Unknown account `{args.replace}`. Known accounts: {known}.")

    # Login lands in a staging slot first; only after the account identity
    # is known do we decide where it belongs. This is what prevents a second
    # login from silently destroying the first account's credentials.
    staging_root = settings.data_dir / ACCOUNTS_DIRNAME / f".staging-{os.getpid()}"
    staging = AuthManager(
        staging_root,
        "file",
        settings.issuer_base_url,
        client_id=settings.client_id,
    )
    # Headless machines default to the device flow: the browser flow's
    # redirect lands on localhost:1455 of the machine running the browser,
    # which on a server is the wrong machine entirely. Explicit flags win.
    use_device = args.device
    if not use_device and not args.browser and _is_headless_environment():
        use_device = True
        print("No local browser detected (SSH session or no display).")
        print("Using device-code login. Force the browser flow with: airelays login --browser")
        print()

    try:
        if use_device:
            record = await staging.device_login(
                client_id=settings.client_id,
                timeout_seconds=settings.login_timeout_seconds,
                workspace_id=args.workspace_id,
                on_device_code=_print_device_prompt,
                on_waiting=_print_device_waiting,
            )
        else:
            record = await staging.browser_login(
                client_id=settings.client_id,
                open_browser=not args.no_browser and settings.browser_open,
                timeout_seconds=settings.login_timeout_seconds,
                workspace_id=args.workspace_id,
                on_authorize_url=_print_login_prompt,
            )
        raw_payload = staging.storage.load() or dict(record.raw)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    added_as: int | None = None
    if replace_slot is not None:
        target_root = replace_slot.storage_root
    else:
        same = next(
            (
                slot
                for slot in existing_slots
                if slot.account_id and record.account_id and slot.account_id == record.account_id
            ),
            None,
        )
        if same is not None:
            # Same subscription signed in again: refresh in place.
            target_root = same.storage_root
        elif not existing_slots:
            # First login keeps the legacy layout so older relay versions
            # and existing tooling continue to work untouched.
            target_root = settings.data_dir
        else:
            slug = slug_for_account(record.account_id, record.email)
            target_root = settings.data_dir / ACCOUNTS_DIRNAME / slug
            added_as = len(existing_slots) + 1

    ensure_private_dir(target_root)
    AuthStorage(target_root, settings.auth_storage_mode).save(raw_payload)

    payload = _login_payload(settings, record)
    if added_as is not None:
        first = existing_slots[0].label
        this_label = record.email or "this account"
        payload["added_account_note"] = (
            f"Added {this_label} as account #{added_as}. {first} is used first; "
            f"{this_label} takes over when it hits its usage limit. A running relay "
            "picks this up within seconds."
        )
        # Always give the undo path so a mistaken add has a printed way out.
        payload["next_steps"] = [
            "airelays accounts",
            f"airelays logout {record.email or ''}".strip(),
        ]
    _print_login_summary(payload)


def _run_init(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    try:
        settings.validate_provider_guardrails()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    created_config = settings.write_config_file(force=args.force)
    token = settings.resolve_bearer_token()
    token_created = False
    if settings.require_bearer_auth and not token:
        token = settings.rotate_bearer_token()
        token_created = True
    token_to_show = token if token_created or args.show_token else None
    payload = _init_payload(settings, created_config, token_created, token_to_show)
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_init_summary(payload)


def _run_status(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    manager = _auth_manager(settings)
    payload = _status_payload(settings, manager)
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_status_summary(payload)


async def _run_doctor(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    payload = await _doctor_payload(settings, skip_response=args.skip_response)
    if _json_requested(args):
        _emit_json(payload)
    else:
        _print_doctor_summary(payload)
    if not payload.get("ok"):
        raise SystemExit(1)


def _run_logout(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    slots = discover_slots(settings)
    selector = getattr(args, "account", None)
    logout_all = getattr(args, "all", False)

    if len(slots) > 1 and not selector and not logout_all:
        names = "\n".join(f"  {slot.label}" for slot in slots)
        raise SystemExit(
            f"You have {len(slots)} OpenAI accounts:\n{names}\n"
            "Run `airelays logout <email>` or `airelays logout --all`."
        )

    targets = slots
    if selector:
        slot, error = resolve_slot(slots, selector)
        if slot is None:
            raise SystemExit(error)
        targets = [slot]

    signed_out = [slot.label for slot in targets]
    deleted = False
    for slot in targets:
        storage = AuthStorage(slot.storage_root, settings.auth_storage_mode)
        deleted = storage.delete() or deleted
        # Extra account slots are directories we created; remove the shell.
        if slot.storage_root != settings.data_dir:
            shutil.rmtree(slot.storage_root, ignore_errors=True)
    remaining = [s.label for s in discover_slots(settings)]
    if _json_requested(args):
        _emit_json({"deleted": deleted, "signed_out": signed_out, "remaining": remaining})
        return
    _print_logout_summary(deleted, signed_out, remaining)


def _run_accounts(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    slots = discover_slots(settings)
    action = getattr(args, "accounts_action", "list") or "list"

    if action == "remove":
        # Deprecated alias of `logout <email>`; kept working, not advertised.
        slot, error = resolve_slot(slots, args.account)
        if slot is None:
            raise SystemExit(error)
        AuthStorage(slot.storage_root, settings.auth_storage_mode).delete()
        if slot.storage_root != settings.data_dir:
            shutil.rmtree(slot.storage_root, ignore_errors=True)
        print(f"Signed out {slot.label}. (Tip: `airelays logout <email>` does the same.)")
        return

    if action == "refresh":
        # Bench state lives in the RUNNING relay's memory, so a hard refresh
        # must talk to it over HTTP; a local process cannot clear another
        # process's cooldowns.
        _hard_refresh_running_relay(settings)
        return

    if action == "resets":
        _show_reset_credits(settings, args)
        return

    if action == "redeem":
        _redeem_reset_credit(settings, args)
        return

    if action == "order":
        resolved = []
        for needle in args.accounts:
            slot, error = resolve_slot(slots, needle)
            if slot is None:
                raise SystemExit(error)
            resolved.append(slot)
        missing = [slot for slot in slots if slot not in resolved]
        order = [slot.slug for slot in resolved + missing]
        manifest = load_manifest(settings.data_dir)
        manifest["order"] = order
        save_manifest(settings.data_dir, manifest)
        slots = discover_slots(settings)
        print("Account order updated.")

    payload = {
        "accounts": [
            {
                "position": index + 1,
                "email": slot.email,
                "plan": slot.plan_type,
                "account_id": slot.account_id,
                "authenticated": slot.authenticated,
                "store": str(slot.storage_root),
                "slug": slot.slug,
            }
            for index, slot in enumerate(slots)
        ],
        "balance": settings.openai_balance,
    }
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_accounts_summary(payload)


def _hard_refresh_running_relay(settings: Settings) -> None:
    import httpx

    url = f"{settings.client_base_url().rstrip('/')}/relay/accounts/refresh"
    headers = {}
    if settings.require_bearer_auth:
        token = settings.resolve_bearer_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
    try:
        response = httpx.post(url, headers=headers, timeout=20.0)
    except httpx.HTTPError:
        raise SystemExit(
            "No running relay to refresh at "
            f"{settings.client_base_url()}. Account limits are per-process and "
            "are already cleared by starting or restarting the relay."
        )
    if response.status_code >= 400:
        raise SystemExit(f"Refresh failed ({response.status_code}): {response.text[:200]}")
    accounts = response.json().get("accounts", [])
    _print_title("AIRelays Accounts Refreshed")
    for entry in accounts:
        entry = dict(entry)
        label = entry.get("email") or entry.get("slug")
        if entry.get("limited"):
            secs = entry.get("limited_for_seconds")
            _print_field(label, f"still at limit (resets in ~{secs}s)", kind="warn")
        else:
            _print_field(label, "available", kind="good")
    print()


def _relay_headers(settings: Settings) -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.require_bearer_auth:
        token = settings.resolve_bearer_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
    return headers


def _no_running_relay(settings: Settings, what: str) -> SystemExit:
    return SystemExit(
        f"No running relay at {settings.client_base_url()} to {what}. "
        "Start AIRelays (or `airelays serve`) first."
    )


def _format_expiry(credit: dict[str, object]) -> str:
    seconds = credit.get("expires_in_seconds")
    at = credit.get("expires_at")
    if not isinstance(at, str):
        return "no expiry reported"
    day = at[:10]
    if isinstance(seconds, int):
        if seconds <= 0:
            return f"expired {day}"
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        left = f"{days}d {hours}h" if days else f"{hours}h"
        return f"expires {day} (in {left})"
    return f"expires {day}"


def _show_reset_credits(settings: Settings, args: argparse.Namespace) -> None:
    """Usage-limit reset credits per account, read through the running relay
    (which already holds the sign-ins and caches the personal endpoint)."""
    import httpx

    url = f"{settings.client_base_url().rstrip('/')}/relay/accounts/resets"
    params: dict[str, str] = {}
    if getattr(args, "account", None):
        params["account"] = args.account
    try:
        response = httpx.get(url, headers=_relay_headers(settings), params=params, timeout=30.0)
    except httpx.HTTPError:
        raise _no_running_relay(settings, "read reset credits from")
    if response.status_code >= 400:
        raise SystemExit(f"Reading reset credits failed ({response.status_code}): {response.text[:300]}")
    payload = response.json()
    if _json_requested(args):
        _emit_json(payload)
        return
    accounts = list(payload.get("accounts", []))
    _print_title("AIRelays Usage Resets")
    if not accounts:
        print("  No OpenAI accounts are signed in yet.")
        _print_steps([_login_hint()])
        return
    for entry in accounts:
        entry = dict(entry)
        label = entry.get("email") or entry.get("slug")
        _print_section(str(label))
        if "resets" not in entry:
            _print_field("Resets", f"unavailable: {entry.get('error')}", kind="warn")
            continue
        resets = dict(entry["resets"])
        count = resets.get("available_count") or 0
        applicable = resets.get("applicable_available_count")
        if count == 0:
            _print_field("Resets", "none available")
            continue
        now = (
            "usable now"
            if applicable
            else "held; nothing to reset right now" if applicable == 0 else "usability unknown"
        )
        _print_field("Resets", f"{count} available, {now}", kind="good" if applicable else "warn")
        for credit in resets.get("credits", []):
            credit = dict(credit)
            if credit.get("status") != "available":
                continue
            title = credit.get("title") or credit.get("reset_type") or "reset"
            granted = credit.get("granted_at")
            granted_text = f", granted {granted[:10]}" if isinstance(granted, str) else ""
            by = credit.get("granted_by")
            by_text = f" by {by}" if by else ""
            _print_field(str(credit.get("id")), f"{title}{by_text}{granted_text}; {_format_expiry(credit)}")
    print()
    print("  Redeem one with: airelays accounts redeem <email>")


def _redeem_reset_credit(settings: Settings, args: argparse.Namespace) -> None:
    """Spends one reset credit through the running relay. Irreversible, so
    it asks first unless --yes is given; the upstream's outcome is shown
    verbatim (a credit held while usage is under its limit answers
    `nothing_to_reset` and is not spent)."""
    import httpx

    body: dict[str, str] = {}
    if getattr(args, "account", None):
        body["account"] = args.account
    if getattr(args, "credit", None):
        body["credit_id"] = args.credit
    if not getattr(args, "yes", False):
        target = args.account or "the signed-in account"
        answer = input(f"Redeem one usage reset on {target} now? This cannot be undone. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing redeemed.")
            return
    url = f"{settings.client_base_url().rstrip('/')}/relay/accounts/resets/redeem"
    try:
        response = httpx.post(url, headers=_relay_headers(settings), json=body, timeout=60.0)
    except httpx.HTTPError:
        raise _no_running_relay(settings, "redeem through")
    if response.status_code >= 400:
        raise SystemExit(f"Redeem failed ({response.status_code}): {response.text[:300]}")
    payload = response.json()
    if _json_requested(args):
        _emit_json(payload)
        return
    outcome = payload.get("outcome")
    label = payload.get("email") or payload.get("slug")
    _print_title("AIRelays Usage Reset")
    messages = {
        "reset": ("Usage reset applied", "good"),
        "nothing_to_reset": ("Not spent: usage does not need a reset right now", "warn"),
        "no_credit": ("No reset credit available on this account", "warn"),
        "already_redeemed": ("That reset was already redeemed", "warn"),
    }
    text, kind = messages.get(str(outcome), (f"Upstream answered `{outcome}`", "warn"))
    _print_field(str(label), text, kind=kind)
    windows = payload.get("windows_reset")
    if outcome == "reset" and isinstance(windows, int):
        _print_field("Windows reset", windows)
    status = payload.get("status") or {}
    default = (status.get("rate_limits") or {}).get("default") or {}
    for slot in ("primary_window", "secondary_window"):
        window = default.get(slot)
        if isinstance(window, dict) and window.get("used_percent") is not None:
            _print_field(f"{window.get('window_label') or slot} used", f"{window['used_percent']:g}%")
    print()


def _print_accounts_summary(payload: dict[str, object]) -> None:
    accounts = list(payload.get("accounts", []))
    _print_title("AIRelays Accounts")
    if not accounts:
        print("  No OpenAI accounts are signed in yet.")
        _print_steps([_login_hint()])
        return
    multi = len(accounts) > 1
    for entry in accounts:
        entry = dict(entry)
        state = "ready" if entry.get("authenticated") else "signed out"
        position = entry.get("position")
        label = entry.get("email") or entry.get("slug")
        heading = f"{position}. {label}" if multi else str(label)
        if multi and position == 1 and payload.get("balance") == "ordered":
            heading += "  (used first)"
        _print_section(heading)
        _print_field("Plan", entry.get("plan"))
        _print_field("Status", state, kind="good" if state == "ready" else "warn")
        _print_field("Store", entry.get("store"))
    if multi:
        balance = payload.get("balance")
        print()
        if balance == "ordered":
            print("  Requests use account 1 first, then the next when it reaches")
            print("  its usage limit.")
        elif balance == "round_robin":
            print("  Requests are spread evenly across your accounts; an account")
            print("  at its usage limit is skipped until it resets.")
        elif balance == "ping_pong":
            print("  Requests take turns: each account once, in order, then again.")
        elif balance == "weighted":
            print("  Requests are split in proportion to each account's remaining")
            print("  capacity (50% left against 100% left serves 1 in 3).")
        else:
            print("  Requests are balanced by remaining capacity: the account with")
            print("  the most unused quota serves next, so consumption equalizes")
            print("  as a percentage of each plan. Limited accounts are skipped.")

    # A self-documenting hub: always show how to add, and how to undo/reorder
    # once there is more than one account. Emails come from the listing.
    first = accounts[0].get("email") or accounts[0].get("slug") if accounts else None
    last = accounts[-1].get("email") or accounts[-1].get("slug") if accounts else None
    print()
    _print_section("Manage")
    _print_command("Add another account", "airelays login")
    if multi:
        _print_command("Sign one out", f"airelays logout {last}")
        _print_command("Sign out everything", "airelays logout --all")
        _print_command("Change the order", f"airelays accounts order {last} {first}")
    else:
        _print_command("Sign out", "airelays logout")
    print()


def _relay_get(settings: Settings, path: str, *, timeout: float = 20.0):
    """One GET against the running relay with its bearer token."""
    import httpx

    url = f"{settings.client_base_url().rstrip('/')}{path}"
    headers = {}
    if settings.require_bearer_auth:
        token = settings.resolve_bearer_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
    try:
        return httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        raise SystemExit(
            f"No running relay at {settings.client_base_url()}. Start it with `airelays serve`."
        )


def _relay_put(settings: Settings, path: str, payload: dict, *, timeout: float = 20.0):
    """One PUT against the running relay with its bearer token."""
    import httpx

    url = f"{settings.client_base_url().rstrip('/')}{path}"
    headers = {}
    if settings.require_bearer_auth:
        token = settings.resolve_bearer_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
    try:
        return httpx.put(url, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError:
        raise SystemExit(
            f"No running relay at {settings.client_base_url()}. Start it with `airelays serve`."
        )


def _run_balance(args: argparse.Namespace) -> None:
    """Show or switch account balancing on the running relay, live."""
    settings = _base_settings(args)
    strategy = getattr(args, "strategy", None)
    if strategy:
        response = _relay_put(settings, "/relay/balance", {args.provider: strategy})
    else:
        response = _relay_get(settings, "/relay/balance")
    if response.status_code >= 400:
        raise SystemExit(f"Balance request failed ({response.status_code}): {_relay_error_detail(response)}")
    payload = response.json()
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_title("AIRelays Account Balancing")
    _print_field("OpenAI accounts", payload.get("openai"), kind="good" if strategy and args.provider == "openai" else "plain")
    _print_field("Claude accounts", payload.get("claude"), kind="good" if strategy and args.provider == "claude" else "plain")
    if strategy:
        print("  Applied to the running relay; the next request uses it. To keep it across")
        print("  restarts, set `balance` in the config file or AIRELAYS_OPENAI_BALANCE /")
        print("  AIRELAYS_CLAUDE_BALANCE.")


def _relay_error_detail(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return f"relay answered {response.status_code}"


def _run_usage(args: argparse.Namespace) -> None:
    """Subscription usage as the running relay reports it: every text
    window plus the image-generation quota, per account and provider."""
    settings = _base_settings(args)
    payload: dict[str, object] = {"object": "usage"}
    response = _relay_get(settings, "/subscription/status?all_accounts=true")
    payload["openai"] = (
        response.json() if response.status_code < 400 else {"error": _relay_error_detail(response)}
    )
    if settings.enable_claude:
        response = _relay_get(settings, "/subscription/status?provider=claude&all_accounts=true")
        payload["claude"] = (
            response.json() if response.status_code < 400 else {"error": _relay_error_detail(response)}
        )
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_usage_summary(payload)


def _format_duration(seconds: float) -> str:
    rest = max(0, int(seconds))
    parts: list[str] = []
    for size, label in ((86400, "d"), (3600, "h"), (60, "m")):
        if rest >= size:
            parts.append(f"{rest // size}{label}")
            rest %= size
        if len(parts) == 2:
            break
    return " ".join(parts) if parts else "<1m"


def _format_usage_window(window: dict[str, object]) -> tuple[str, str]:
    """("41% used · resets in 3h 2m", kind) for one normalized window."""
    used = window.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return "awaiting fresh data", "plain"
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        seconds: float | None = float(reset_at) - time.time()
    else:
        after = window.get("reset_after_seconds")
        seconds = float(after) if isinstance(after, (int, float)) and not isinstance(after, bool) else None
    text = f"{used:.0f}% used"
    if seconds is not None and seconds > 0:
        text += f" · resets in {_format_duration(seconds)}"
    kind = "bad" if used >= 100 else "warn" if used >= 80 else "good"
    return text, kind


def _usage_window_rows(status: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    """Every window a normalized status carries, labelled: the default
    text windows, named extra quotas, and the image-generation quota."""
    limits = status.get("rate_limits") if isinstance(status.get("rate_limits"), dict) else {}
    rows: list[tuple[str, dict[str, object]]] = []

    def label(prefix: str, window: dict[str, object]) -> str:
        window_label = window.get("window_label")
        return f"{prefix} {window_label}" if isinstance(window_label, str) and window_label else prefix

    default = limits.get("default") if isinstance(limits.get("default"), dict) else {}
    for slot in ("primary_window", "secondary_window"):
        window = default.get(slot)
        if isinstance(window, dict):
            rows.append((label("Requests", window), window))
    named: list[tuple[str, dict[str, object]]] = []
    if isinstance(limits.get("code_review"), dict):
        named.append(("Code review", limits["code_review"]))
    for extra in limits.get("additional") or []:
        if isinstance(extra, dict) and isinstance(extra.get("rate_limit"), dict):
            named.append((str(extra.get("limit_name") or extra.get("metered_feature") or "Other"), extra["rate_limit"]))
    if isinstance(limits.get("image_generation"), dict):
        named.append(("Images", limits["image_generation"]))
    for name, rate in named:
        for slot in ("primary_window", "secondary_window"):
            window = rate.get(slot)
            if isinstance(window, dict):
                rows.append((label(name, window), window))
    return rows


def _print_usage_status(status: dict[str, object]) -> None:
    rows = _usage_window_rows(status)
    if not rows:
        _print_field("Usage", "no windows reported")
    for name, window in rows:
        text, kind = _format_usage_window(window)
        _print_field(name, text, kind=kind)
    limits = status.get("rate_limits") if isinstance(status.get("rate_limits"), dict) else {}
    images = limits.get("image_generation") if isinstance(limits.get("image_generation"), dict) else None
    if images:
        relay = images.get("relay") if isinstance(images.get("relay"), dict) else {}
        count = relay.get("images")
        tokens = relay.get("image_tokens")
        if isinstance(count, int):
            detail = f"{count} image{'s' if count != 1 else ''} in the last {images.get('primary_window', {}).get('window_label') or 'window'}"
            if isinstance(tokens, int) and tokens:
                detail += f" ({tokens} image tokens)"
            _print_field("  via this relay", detail)
        source = images.get("source")
        observed = images.get("observed_at")
        if isinstance(source, str) and isinstance(observed, str):
            how = "probe" if source == "probe" else "last image response"
            _print_field("  reading", f"{how}, {observed}")


def _print_usage_summary(payload: dict[str, object]) -> None:
    _print_title("AIRelays Usage")
    openai = payload.get("openai")
    if isinstance(openai, dict):
        if openai.get("error"):
            _print_section("OpenAI")
            _print_field("Usage", str(openai["error"]), kind="warn")
        else:
            entries = openai.get("accounts")
            if not isinstance(entries, list):
                entries = [{"email": (openai.get("account") or {}).get("email"), "status": openai}]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                plan = (status or {}).get("account", {}).get("plan_type") if isinstance(status, dict) else None
                heading = f"OpenAI · {entry.get('email') or entry.get('slug') or 'account'}"
                if plan:
                    heading += f" ({plan})"
                _print_section(heading)
                if isinstance(status, dict):
                    _print_usage_status(status)
                else:
                    _print_field("Usage", str(entry.get("error") or "unavailable"), kind="warn")
    claude = payload.get("claude")
    if isinstance(claude, dict):
        if claude.get("error"):
            _print_section("Claude")
            _print_field("Usage", str(claude["error"]), kind="warn")
        else:
            for entry in claude.get("accounts") or []:
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                plan = (status or {}).get("account", {}).get("plan_type") if isinstance(status, dict) else None
                heading = f"Claude · {entry.get('email') or entry.get('slug') or 'account'}"
                if plan:
                    heading += f" ({plan})"
                _print_section(heading)
                if isinstance(status, dict):
                    _print_usage_status(status)
                elif entry.get("usage_unsupported"):
                    _print_field("Usage", "not reported for setup tokens")
                else:
                    _print_field("Usage", str(entry.get("error") or "unavailable"), kind="warn")
    print()
    print("  Images: the image-generation quota is a separate 24-hour upstream bucket;")
    print("  text windows do not include it.")
    print()


def _run_claude_set_token(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    _print_title("Claude Token")
    print("Paste a Claude Code OAuth token. You get this token from the")
    print("`claude` CLI itself — on any machine WITH a browser, run:")
    print()
    print("    claude setup-token")
    print()
    print("then copy the token it prints and paste it here.")
    print()
    # Read from stdin (or hidden prompt on a TTY), never argv: command-line
    # arguments leak into shell history and `ps` output.
    if sys.stdin.isatty():
        import getpass

        token = getpass.getpass("  Token: ").strip()
    else:
        token = sys.stdin.readline().strip()
    if not token:
        raise SystemExit("No token provided.")
    settings.write_claude_oauth_token(token)
    print()
    _print_field("Stored in", settings.claude_oauth_token_file, kind="good")
    print("  AIRelays passes it to the local `claude` CLI automatically;")
    print("  no environment variable export is needed.")
    print()


def _run_claude_login(args: argparse.Namespace) -> None:
    """Delegate sign-in and refresh-token storage to the unmodified CLI."""
    import subprocess

    settings = _base_settings(args)
    profiles = discover_profiles(settings)
    try:
        profile = resolve_profile(profiles, args.replace) if args.replace else new_profile(settings)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if not profile.managed:
        raise SystemExit("The setup-token account is replaced with `airelays claude set-token`.")
    expected = account_identity(profile.identity)
    if args.replace:
        suspend_profile(profile)
    runtime = ClaudeCliRuntime(settings, profile=profile)
    print("Sign in to your Claude subscription in the browser.", flush=True)
    try:
        result = runtime.run_cli(["auth", "login", "--claudeai"], interactive=True, capture=False)
    except OSError as error:
        raise SystemExit(f"Cannot launch Claude Code: {error}") from error
    if result.returncode != 0:
        raise SystemExit("Claude sign-in did not finish; no account was added.")
    status = ClaudeCliRuntime(settings, profile=profile)._run_status_command()
    if expected and account_identity(status) != expected:
        raise SystemExit("A different Claude account was selected. This profile will not serve requests; sign in again with the original account.")
    if profile.managed:
        duplicate = next((p for p in profiles if p.managed and p.slug != profile.slug
                          and account_identity(p.identity) == account_identity(status)), None)
        if duplicate:
            # Leave the original profile and its credentials untouched.
            try:
                cleanup = runtime.run_cli(["auth", "logout"], timeout=60)
                if cleanup.returncode != 0:
                    print(f"The unused profile at {profile.config_dir} could not be signed out; it is not enrolled.")
            except (OSError, subprocess.TimeoutExpired):
                print(f"The unused profile at {profile.config_dir} could not be signed out; it is not enrolled.")
            print(f"This account is already enrolled as {duplicate.slug}. Use `airelays claude login --replace {duplicate.slug}` to renew its sign-in.")
            return
        try:
            enroll_profile(profile, status)
        except ValueError as error:
            raise SystemExit(str(error)) from error
    print(f"Claude account ready: {status.get('email') or profile.slug} ({profile.slug}).")
    print("The running relay adds it to load balancing automatically.")


def _run_claude_accounts(args: argparse.Namespace) -> None:
    from airelay.claude_accounts import ClaudeAccountPool

    status = ClaudeAccountPool(_base_settings(args)).status()
    if args.json:
        print(json.dumps({"accounts": status["accounts"], "balance": status["balance"]}, indent=2))
        return
    _print_title("Claude Accounts")
    for account in status["accounts"]:
        _print_field(account["slug"], account.get("email") or "Identity unavailable")
        _print_field("Status", "ready" if account.get("ready_for_requests") else "sign-in required")
    if not status["accounts"]:
        print("No Claude accounts signed in.")
    print("\nAdd: airelays claude login")
    print("Renew: airelays claude login --replace ACCOUNT")
    print("Sign out: airelays claude logout ACCOUNT")


def _run_claude_logout(args: argparse.Namespace) -> None:
    """Sign out relay-owned accounts only; Claude Code's own sign-in is
    never touched (ADR 0007)."""
    import subprocess

    settings = _base_settings(args)
    profiles = discover_profiles(settings)
    needle = getattr(args, "account", None)
    if needle:
        try:
            selected = [resolve_profile(profiles, needle)]
        except ValueError as error:
            raise SystemExit(str(error)) from error
    elif not profiles:
        raise SystemExit("No Claude account is connected to AIRelays.")
    elif len(profiles) > 1 and not getattr(args, "all", False):
        raise SystemExit("Choose a Claude account to sign out: `airelays claude logout ACCOUNT` (or `--all`).")
    else:
        selected = profiles
    outcomes = []
    for profile in selected:
        if not profile.managed:
            # A setup token is relay configuration: deleting it is the sign-out.
            token_removed = settings.claude_oauth_token_file.exists()
            settings.claude_oauth_token_file.unlink(missing_ok=True)
            outcomes.append({"slug": profile.slug, "token_removed": token_removed,
                             "cli_signed_out": True, "cli_error": None})
            continue
        error = None
        try:
            result = ClaudeCliRuntime(settings, profile=profile).run_cli(["auth", "logout"], timeout=60)
            if result.returncode != 0:
                error = (result.stderr or result.stdout).strip() or f"Claude exited {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = str(exc)
        if error is None:
            forget_profile(profile)
        outcomes.append({"slug": profile.slug, "token_removed": False,
                         "cli_signed_out": error is None, "cli_error": error})
    if getattr(args, "json", False):
        print(json.dumps(outcomes[0] if len(outcomes) == 1 else {"accounts": outcomes}))
        return
    for outcome in outcomes:
        if outcome["cli_error"]:
            raise SystemExit(f"Claude sign-out incomplete ({outcome['slug']}): {outcome['cli_error']}")
        if outcome["token_removed"]:
            print("Removed the stored Claude setup token.")
        else:
            print(f"Signed out Claude account {outcome['slug']}.")


def _run_models(args: argparse.Namespace) -> None:
    """Lists model ids from the running relay — the same list the desktop
    Models tab shows, one source of truth for what the endpoint accepts."""
    import httpx

    settings = _base_settings(args)
    url = f"{settings.client_base_url().rstrip('/')}/models"
    headers = {}
    if settings.require_bearer_auth:
        token = settings.resolve_bearer_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(url, headers=headers, timeout=15.0)
    except httpx.HTTPError:
        raise SystemExit(
            f"No running relay at {settings.client_base_url()}. Start it with "
            "`airelays serve`, then retry."
        )
    if response.status_code >= 400:
        raise SystemExit(f"Model listing failed ({response.status_code}): {response.text[:200]}")
    payload = response.json()
    if _json_requested(args):
        _emit_json(payload)
        return
    models = payload.get("data", [])
    _print_title("Models")
    _print_field("Endpoint", settings.client_base_url())
    by_provider: dict[str, list[dict]] = {}
    for model in models:
        provider = (model.get("airelays") or {}).get("provider", "other")
        by_provider.setdefault(provider, []).append(model)
    display_names = {"openai": "OpenAI", "claude": "Claude"}
    for provider, entries in by_provider.items():
        _print_section(display_names.get(provider, provider))
        width = max((len(str(model.get("id"))) for model in entries), default=0)
        for model in entries:
            extension = model.get("airelays") or {}
            reasoning = extension.get("reasoning") or {}
            modes = reasoning.get("modes") or []
            suffix = f"  reasoning: {', '.join(modes)}" if modes else ""
            structured = extension.get("structured_output") or {}
            types = structured.get("types") or []
            if types:
                suffix += f"  structured: {', '.join(types)}"
            resolved = extension.get("resolved_model")
            if resolved and resolved != model.get("id"):
                suffix += f"  resolves to: {resolved}"
            print(f"  {str(model.get('id')).ljust(width)}{suffix}")
    print()
    print("  Use these ids as `model` in requests to the endpoint above.")
    print("  Set `reasoning_effort` to one of the listed reasoning modes.")
    print()


def _run_token_rotate(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    settings.ensure_directories()
    token = settings.rotate_bearer_token()
    payload = _token_rotate_payload(settings, token)
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_token_rotate_summary(payload)


def _run_token_show(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    payload = _token_show_payload(settings)
    if _json_requested(args):
        _emit_json(payload)
        return
    _print_token_show_summary(payload)


# Exit status of a relay refused because another one holds its data
# directory; the desktop app reports it instead of restarting the relay.
RELAY_ALREADY_RUNNING = 75  # EX_TEMPFAIL, unlike any crash status


def _hold_relay_lock(settings: Settings, wait: float = 5.0) -> InterProcessLock:
    """One relay per data directory: two relays sharing accounts, uploads,
    conversations and logs would race each other. The lock is held for the
    life of the process (the OS releases it if the process dies); the pid
    file is informational only. `wait` covers a relay that is still
    exiting, as when the desktop app restarts it."""
    lock = InterProcessLock(settings.data_dir / "relay.lock")
    try:
        lock.acquire(timeout=wait)
    except TimeoutError:
        try:
            owner = (settings.data_dir / "relay.pid").read_text().strip() or "unknown"
        except OSError:
            owner = "unknown"
        print(
            f"Another AIRelays relay is already running with the data directory {settings.data_dir} "
            f"(pid {owner}). Stop it first, or use a different --data-dir.",
            file=sys.stderr,
        )
        raise SystemExit(RELAY_ALREADY_RUNNING) from None
    except LockUnsupported:
        print(
            f"Warning: {settings.data_dir} does not support file locks; make sure no other relay uses it.",
            file=sys.stderr,
        )
        return lock
    write_private(settings.data_dir / "relay.pid", f"{os.getpid()}\n")
    return lock


def _run_serve(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    try:
        settings.validate_provider_guardrails()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    settings.ensure_directories()
    relay_lock = _hold_relay_lock(settings)
    try:
        if settings.require_bearer_auth and not settings.resolve_bearer_token():
            if settings.auto_generate_bearer_token:
                settings.ensure_bearer_token()
            else:
                raise SystemExit(
                    "Bearer authentication is enabled, but no relay token is configured. "
                    "Run `airelays init` or set AIRELAYS_BEARER_TOKEN."
                )
        provider_statuses = _provider_registry(settings, _auth_manager(settings)).provider_statuses()
        _print_serve_banner(settings, provider_statuses)
        app = create_app(settings)
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    finally:
        relay_lock.release()


def _run_logs(args: argparse.Namespace) -> None:
    """The same policy service as the API, usable even with the relay stopped."""
    from airelay.traffic import TrafficLogger

    settings = _base_settings(args)
    changes = {
        key: getattr(args, key)
        for key in ("retention_days", "max_total_mb", "max_file_mb")
        if getattr(args, key) is not None
    }
    try:
        traffic = TrafficLogger(settings.logs_dir, settings.log_policy())
        result = traffic.configure(changes) if changes else traffic.status()
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"Cannot configure log retention: {error}") from error
    if args.json:
        print(json.dumps(result, indent=2))
        return
    policy = result["policy"]
    days = policy["retention_days"]
    _print_title("AIRelays Traffic Logs")
    _print_field("Logs dir", result["logs_dir"])
    _print_field("Retention", f"{days} days" if days else "No age limit")
    _print_field("Disk limit", f"{policy['max_total_mb']} MiB (oldest files removed first)")
    _print_field("Rotate at", f"{policy['max_file_mb']} MiB or hourly")
    _print_field("Disk usage", f"{result['usage_bytes'] / (1024 * 1024):.1f} MiB, {result['file_count']} files")
    if changes:
        _print_field("Reclaimed", f"{result['deleted_bytes'] / (1024 * 1024):.1f} MiB")
        print("  Saved. Running relays sharing this log directory apply it within 60 seconds.")
    if result["last_error"]:
        _print_field("Cleanup error", result["last_error"], kind="warn")


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    # A nested parser must not replace options already supplied to its
    # parent with None. Shared options work at every command level.
    shared = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    shared.add_argument("--config", help="Path to the AIRelays config file")
    shared.add_argument("--data-dir", help="Local AIRelays state directory")
    shared.add_argument("--logs-dir", help="Traffic log directory")
    shared.add_argument(
        "--auth-storage",
        choices=("auto", "file", "keyring"),
        help="Deprecated: OpenAI sign-ins are always stored as private files",
    )
    shared.add_argument("--bearer-token-file", help="Path to the relay bearer token file")

    parser = argparse.ArgumentParser(
        description="AIRelays: subscription-backed OpenAI-compatible endpoint",
        parents=[shared],
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", parents=[shared], help="Run the local AIRelays server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument(
        "--balance",
        choices=list(BALANCE_STRATEGIES),
        default=None,
        help=(
            "How requests are spread across OpenAI accounts for this server process "
            "(overrides [providers.openai] balance and AIRELAYS_OPENAI_BALANCE)"
        ),
    )
    serve.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Disable AIRelays local bearer auth for this server process; upstream provider "
            "login is still required"
        ),
    )
    serve.set_defaults(func=_run_serve)

    logs = subparsers.add_parser(
        "logs", parents=[shared],
        help="Show traffic-log usage or save retention limits (applied without restart)",
    )
    _add_json_argument(logs)
    logs.add_argument("--retention-days", type=int, help="Keep up to N days (7 = week, 30 = month, 0 = no age limit)")
    logs.add_argument("--max-total-mb", type=int, help="Total traffic-log budget in MiB (default 1024)")
    logs.add_argument("--max-file-mb", type=int, help="Rotate files at this many MiB (default 50)")
    logs.set_defaults(func=_run_logs)

    balance = subparsers.add_parser(
        "balance",
        parents=[shared],
        help="Show or switch account balancing on the running relay (live, no restart)",
    )
    balance.add_argument("strategy", nargs="?", choices=list(BALANCE_STRATEGIES), help="New strategy; omit to show the current one")
    balance.add_argument("--provider", choices=["openai", "claude"], default="openai", help="Which account pool (default: openai)")
    _add_json_argument(balance)
    balance.set_defaults(func=_run_balance)

    init = subparsers.add_parser(
        "init",
        parents=[shared],
        help="Write a local config file and generate a relay bearer token",
    )
    _add_json_argument(init)
    init.add_argument("--force", action="store_true", help="Overwrite an existing config file")
    init.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "Write config with local bearer auth disabled and skip relay-token creation; "
            "upstream provider login is still required"
        ),
    )
    init.add_argument(
        "--show-token",
        action="store_true",
        help="Reveal the current relay token even when it was already present",
    )
    init.set_defaults(func=_run_init)

    login = subparsers.add_parser(
        "login",
        parents=[shared],
        help="Run the OpenAI subscription login flow using AIRelays-owned auth storage",
    )
    login.add_argument(
        "--device",
        action="store_true",
        help="Sign in from this terminal using a code you enter in a browser on any "
        "other device (use on SSH/headless servers); the default on headless machines",
    )
    login.add_argument(
        "--browser",
        action="store_true",
        help="Force the browser flow even when this machine looks headless",
    )
    login.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser")
    login.add_argument("--workspace-id", help="Restrict login to a specific ChatGPT workspace id")
    login.add_argument(
        "--replace",
        metavar="ACCOUNT",
        help="Overwrite the stored credentials of an existing account (email or prefix); "
        "without this, signing in with a new account adds it alongside the others",
    )
    login.set_defaults(async_func=_run_login)

    status = subparsers.add_parser(
        "status",
        parents=[shared],
        help="Show relay, OpenAI-session, and provider-runtime status",
    )
    _add_json_argument(status)
    status.set_defaults(func=_run_status)

    doctor = subparsers.add_parser(
        "doctor",
        parents=[shared],
        help="Run local setup checks plus live upstream model and response probes",
    )
    _add_json_argument(doctor)
    doctor.add_argument(
        "--skip-response",
        action="store_true",
        help="Skip the tiny OpenAI /responses smoke request",
    )
    doctor.set_defaults(async_func=_run_doctor)

    logout = subparsers.add_parser(
        "logout",
        parents=[shared],
        help="Delete AIRelays-owned OpenAI subscription auth",
    )
    logout.add_argument(
        "account",
        nargs="?",
        help="Which account to sign out (email or prefix); required with multiple accounts",
    )
    logout.add_argument("--all", action="store_true", help="Sign out every OpenAI account")
    _add_json_argument(logout)
    logout.set_defaults(func=_run_logout)

    accounts = subparsers.add_parser(
        "accounts",
        parents=[shared],
        help="List and manage your own OpenAI accounts (multiple subscriptions, one user)",
    )
    _add_json_argument(accounts)
    accounts.set_defaults(func=_run_accounts, accounts_action="list")
    accounts_subparsers = accounts.add_subparsers(dest="accounts_action")
    accounts_list = accounts_subparsers.add_parser(
        "list", parents=[shared], help="List signed-in accounts in balancing order"
    )
    _add_json_argument(accounts_list)
    accounts_list.set_defaults(func=_run_accounts, accounts_action="list")
    accounts_remove = accounts_subparsers.add_parser(
        "remove", parents=[shared], help="Remove one account's stored credentials"
    )
    accounts_remove.add_argument("account", help="Account email (or unambiguous prefix)")
    accounts_remove.set_defaults(func=_run_accounts, accounts_action="remove")
    accounts_order = accounts_subparsers.add_parser(
        "order", parents=[shared], help="Set which account is used first"
    )
    accounts_order.add_argument("accounts", nargs="+", help="Account emails, first = used first")
    _add_json_argument(accounts_order)
    accounts_order.set_defaults(func=_run_accounts, accounts_action="order")
    accounts_refresh = accounts_subparsers.add_parser(
        "refresh",
        parents=[shared],
        help="Clear usage-limit benches on the running relay and re-check capacity",
    )
    accounts_refresh.set_defaults(func=_run_accounts, accounts_action="refresh")
    accounts_resets = accounts_subparsers.add_parser(
        "resets",
        parents=[shared],
        help="Show usage-limit reset credits per account (what Codex calls 'Redeem reset')",
    )
    accounts_resets.add_argument("account", nargs="?", help="Account email (or unambiguous prefix); default: all")
    _add_json_argument(accounts_resets)
    accounts_resets.set_defaults(func=_run_accounts, accounts_action="resets")
    accounts_redeem = accounts_subparsers.add_parser(
        "redeem",
        parents=[shared],
        help="Redeem one usage-limit reset credit now, through the running relay",
    )
    accounts_redeem.add_argument("account", nargs="?", help="Account email (or unambiguous prefix); required with several accounts")
    accounts_redeem.add_argument("--credit", metavar="ID", help="Redeem this specific credit id (default: upstream picks)")
    accounts_redeem.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation")
    _add_json_argument(accounts_redeem)
    accounts_redeem.set_defaults(func=_run_accounts, accounts_action="redeem")

    claude_cmd = subparsers.add_parser(
        "claude",
        parents=[shared],
        help="Manage the local Claude runtime credentials",
    )
    claude_subparsers = claude_cmd.add_subparsers(dest="claude_command", required=True)
    claude_login = claude_subparsers.add_parser(
        "login", parents=[shared], help="Add a Claude subscription using an isolated Claude Code profile",
    )
    claude_login.add_argument("--replace", metavar="ACCOUNT", help="Renew one existing account (email or account id)")
    claude_login.set_defaults(func=_run_claude_login)
    claude_accounts = claude_subparsers.add_parser(
        "accounts", parents=[shared], help="List Claude accounts and their account ids",
    )
    _add_json_argument(claude_accounts)
    claude_accounts.set_defaults(func=_run_claude_accounts)
    claude_set_token = claude_subparsers.add_parser(
        "set-token",
        parents=[shared],
        help="Store a Claude Code OAuth token (from `claude setup-token`) for headless use",
    )
    claude_set_token.set_defaults(func=_run_claude_set_token)
    claude_logout = claude_subparsers.add_parser(
        "logout",
        parents=[shared],
        help="Sign out one Claude account without changing the others",
    )
    claude_logout_target = claude_logout.add_mutually_exclusive_group()
    claude_logout_target.add_argument("account", nargs="?", help="Account email or id; required with multiple accounts")
    claude_logout_target.add_argument("--all", action="store_true", help="Sign out every enrolled Claude account")
    _add_json_argument(claude_logout)
    claude_logout.set_defaults(func=_run_claude_logout)

    models = subparsers.add_parser(
        "models",
        parents=[shared],
        help="List the model ids the running relay accepts (all providers)",
    )
    _add_json_argument(models)
    models.set_defaults(func=_run_models)

    usage = subparsers.add_parser(
        "usage",
        parents=[shared],
        help="Show subscription usage from the running relay: text windows and the image-generation quota",
    )
    _add_json_argument(usage)
    usage.set_defaults(func=_run_usage)

    token = subparsers.add_parser(
        "token",
        parents=[shared],
        help="Manage the relay bearer token",
    )
    token_subparsers = token.add_subparsers(dest="token_command", required=True)
    show = token_subparsers.add_parser(
        "show",
        parents=[shared],
        help="Show the current relay token without changing it",
    )
    _add_json_argument(show)
    show.set_defaults(func=_run_token_show)
    rotate = token_subparsers.add_parser(
        "rotate",
        parents=[shared],
        help="Generate and persist a new relay token",
    )
    _add_json_argument(rotate)
    rotate.set_defaults(func=_run_token_rotate)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if hasattr(args, "async_func"):
            asyncio.run(args.async_func(args))
            return
        args.func(args)
    except AuthenticationError as exc:
        # Auth failures are user-facing conditions, not crashes: print the
        # message, never a traceback.
        raise SystemExit(str(exc)) from exc
