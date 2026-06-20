from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import uvicorn

from airelay.app import create_app
from airelay.auth import AuthManager, AuthRecord
from airelay.config import APP_NAME, Settings
from airelay.terminal import accent, bad, bold, good, muted, warn


_FIELD_WIDTH = 18


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
                payload["client_api_key"] = token
                payload["openai_api_key_env"] = f"OPENAI_API_KEY={token}"
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
    if getattr(args, "logs_dir", None):
        settings.logs_dir = Path(args.logs_dir).expanduser()
    if getattr(args, "auth_storage", None):
        settings.auth_storage_mode = args.auth_storage
    if getattr(args, "bearer_token_file", None):
        settings.bearer_token_file = Path(args.bearer_token_file).expanduser()
    if getattr(args, "no_auth", False):
        settings.require_bearer_auth = False
        settings.auto_generate_bearer_token = False
    return settings


def _auth_manager(settings: Settings) -> AuthManager:
    return AuthManager(
        settings.data_dir,
        settings.auth_storage_mode,
        settings.issuer_base_url,
        client_id=settings.client_id,
    )


def _status_payload(settings: Settings, manager: AuthManager) -> dict[str, object]:
    auth_status = manager.status()
    next_steps: list[str] = []
    token_ready = bool(settings.resolve_bearer_token()) or not settings.require_bearer_auth
    if settings.require_bearer_auth and not token_ready:
        next_steps.append("airelays init")
    if not auth_status.get("ready_for_requests"):
        next_steps.append("airelays login")
    if auth_status.get("ready_for_requests") and token_ready:
        next_steps.append(_serve_command(settings))
    return {
        "relay": settings.summary(),
        "auth": auth_status,
        "client": _client_usage_payload(settings),
        "next_steps": next_steps,
    }


def _login_payload(settings: Settings, record: AuthRecord) -> dict[str, object]:
    return {
        "authenticated": record.authenticated,
        "email": record.email,
        "plan_type": record.plan_type,
        "account_id": record.account_id,
        "has_openai_api_key": bool(record.openai_api_key),
        "bearer_token_present": bool(settings.resolve_bearer_token()),
        "bearer_token_file": str(settings.bearer_token_file),
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
    return {
        "app_name": APP_NAME,
        "config_path": str(settings.config_path),
        "config_created": created_config,
        "bearer_token_file": str(settings.bearer_token_file),
        "bearer_token_created": token_created,
        "bearer_token_source": settings.bearer_token_source(),
        "client_api_key": token_to_show,
        "client": _client_usage_payload(settings, include_token=bool(token_to_show)),
        "next_steps": [
            "airelays login",
            _serve_command(settings),
        ],
    }


def _token_rotate_payload(settings: Settings, token: str) -> dict[str, object]:
    return {
        "bearer_token_file": str(settings.bearer_token_file),
        "bearer_token_source": settings.bearer_token_source(),
        "client_api_key": token,
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
        "client_api_key": token,
        "client": _client_usage_payload(settings, include_token=True),
        "next_steps": next_steps,
    }


def _json_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _print_title(title: str) -> None:
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
            _print_field("Relay token", client.get("client_api_key"))
            _print_command("Export key", str(client.get("openai_api_key_env")))
        else:
            _print_command("Reveal token", str(client.get("reveal_token_command")))
        _print_command("Rotate token", str(client.get("rotate_token_command")))
    else:
        _print_field("Access mode", "open", kind="warn")
        _print_field("Client key", "optional placeholder only", kind="warn")
        _print_field("SDK note", client.get("api_key_note"))


def _print_login_prompt(url: str) -> None:
    _print_title("AIRelays Login")
    print("Open this URL in the browser profile you want to use:")
    print(f"  {url}")


def _print_device_prompt(verification_url: str, user_code: str) -> None:
    _print_title("AIRelays Device Login")
    _print_multiline("Open this URL", [verification_url])
    _print_multiline("Enter this code", [user_code])


def _print_login_summary(payload: dict[str, object]) -> None:
    print()
    _print_section("Upstream Session")
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
    _print_client_section(client)
    next_step = payload.get("next_step")
    if next_step:
        _print_steps([str(next_step)])


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
    _print_client_section(client, include_token=bool(payload.get("client_api_key")))
    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_status_summary(payload: dict[str, object]) -> None:
    relay = dict(payload.get("relay", {}))
    auth = dict(payload.get("auth", {}))
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

    _print_section("Upstream Session")
    _print_bool("Ready", bool(auth.get("ready_for_requests")))
    _print_bool("Authenticated", bool(auth.get("authenticated")))
    _print_bool("Account bound", bool(auth.get("account_bound")))
    _print_field("Email", auth.get("email"))
    _print_field("Plan", auth.get("plan_type"))
    _print_field("Account ID", auth.get("account_id"))
    _print_field("Last refresh", auth.get("last_refresh"))
    _print_field("Storage mode", auth.get("storage_mode"))
    _print_field("Auth store", auth.get("auth_store_path"))

    _print_client_section(client)
    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_logout_summary(deleted: bool) -> None:
    _print_title("AIRelays Logout")
    if deleted:
        _print_field("Upstream auth", "deleted", kind="good")
    else:
        _print_field("Upstream auth", "already empty", kind="warn")
    _print_steps(["airelays login"])


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
    _print_client_section(client, include_token=bool(payload.get("client_api_key")))
    _print_steps([str(step) for step in payload.get("next_steps", [])])


def _print_serve_banner(settings: Settings, auth_ready: bool) -> None:
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
    _print_section("Upstream Session")
    _print_bool("ChatGPT login", auth_ready, true_text="ready", false_text="missing")
    if not auth_ready:
        if not settings.require_bearer_auth:
            _print_field(
                "Open mode note",
                "Local relay token is disabled, but upstream ChatGPT login is still required.",
                kind="warn",
            )
        _print_command("Next command", "airelays login")
    print()


async def _run_login(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    manager = _auth_manager(settings)
    if args.device:
        record = await manager.device_login(
            client_id=settings.client_id,
            timeout_seconds=settings.login_timeout_seconds,
            workspace_id=args.workspace_id,
            on_device_code=_print_device_prompt,
        )
    else:
        record = await manager.browser_login(
            client_id=settings.client_id,
            open_browser=not args.no_browser and settings.browser_open,
            timeout_seconds=settings.login_timeout_seconds,
            workspace_id=args.workspace_id,
            on_authorize_url=_print_login_prompt,
        )
    payload = _login_payload(settings, record)
    _print_login_summary(payload)


def _run_init(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
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


def _run_logout(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    manager = _auth_manager(settings)
    deleted = manager.logout()
    if _json_requested(args):
        _emit_json({"deleted": deleted})
        return
    _print_logout_summary(deleted)


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


def _run_serve(args: argparse.Namespace) -> None:
    settings = _base_settings(args)
    if settings.require_bearer_auth and not settings.resolve_bearer_token():
        if settings.auto_generate_bearer_token:
            settings.ensure_bearer_token()
        else:
            raise SystemExit(
                "Bearer authentication is enabled, but no relay token is configured. "
                "Run `airelays init` or set AIRELAYS_BEARER_TOKEN."
            )
    auth_ready = bool(_auth_manager(settings).status().get("ready_for_requests"))
    _print_serve_banner(settings, auth_ready)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", help="Path to the AIRelays config file")
    shared.add_argument("--data-dir", help="Local AIRelays state directory")
    shared.add_argument("--logs-dir", help="Traffic log directory")
    shared.add_argument(
        "--auth-storage",
        choices=("auto", "file", "keyring"),
        help="Where to load and store reused upstream auth",
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
        "--no-auth",
        action="store_true",
        help="Disable AIRelays local bearer auth for this server process; upstream ChatGPT login is still required",
    )
    serve.set_defaults(func=_run_serve)

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
        help="Write config with local bearer auth disabled and skip relay-token creation; upstream ChatGPT login is still required",
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
    login.add_argument("--device", action="store_true", help="Use device-code login")
    login.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser")
    login.add_argument("--workspace-id", help="Restrict login to a specific ChatGPT workspace id")
    login.set_defaults(async_func=_run_login)

    status = subparsers.add_parser(
        "status",
        parents=[shared],
        help="Show relay and upstream-auth status",
    )
    _add_json_argument(status)
    status.set_defaults(func=_run_status)

    logout = subparsers.add_parser(
        "logout",
        parents=[shared],
        help="Delete stored upstream auth",
    )
    _add_json_argument(logout)
    logout.set_defaults(func=_run_logout)

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
    if hasattr(args, "async_func"):
        asyncio.run(args.async_func(args))
        return
    args.func(args)
