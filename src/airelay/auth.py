from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
import keyring
from keyring.errors import KeyringError

from airelay.config import OPENAI_SUBSCRIPTION_CLIENT_ID


TOKEN_REFRESH_INTERVAL_DAYS = 8
KEYRING_SERVICE = "AIRelays Auth"
LEGACY_KEYRING_SERVICES = ("AIRelay Auth",)
DEFAULT_ORIGINATOR = "codex_cli_rs"
BROWSER_CALLBACK_PORT = 1455
BROWSER_LOGIN_SCOPE = "openid profile email offline_access"


class AuthenticationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "upstream_auth_error") -> None:
        super().__init__(message)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _compute_store_key(storage_root: Path) -> str:
    try:
        canonical = storage_root.expanduser().resolve()
    except OSError:
        canonical = storage_root.expanduser()
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return f"cli|{digest[:16]}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def _generate_state() -> str:
    return _b64url(secrets.token_bytes(32))


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}


def _browser_redirect_uri(port: int) -> str:
    # Match the currently accepted upstream login flow exactly. Hydra rejects
    # the authorize request when this callback host drifts from localhost.
    return f"http://localhost:{port}/auth/callback"


def _encode_query(query: dict[str, str]) -> str:
    return "&".join(f"{key}={quote(value, safe='')}" for key, value in query.items())


def _build_browser_authorize_url(
    issuer_base_url: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    workspace_id: str | None = None,
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": BROWSER_LOGIN_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": DEFAULT_ORIGINATOR,
    }
    if workspace_id:
        query["allowed_workspace_id"] = workspace_id
    return f"{issuer_base_url}/oauth/authorize?{_encode_query(query)}"


def parse_id_token(token: str) -> dict[str, Any]:
    claims = _decode_jwt_claims(token)
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    return {
        "raw_jwt": token,
        "email": claims.get("email"),
        "chatgpt_plan_type": auth_claims.get("chatgpt_plan_type"),
        "chatgpt_account_id": auth_claims.get("chatgpt_account_id"),
        "claims": claims,
    }


@dataclass(slots=True)
class AuthRecord:
    raw: dict[str, Any]

    @property
    def tokens(self) -> dict[str, Any] | None:
        value = self.raw.get("tokens")
        return value if isinstance(value, dict) else None

    @property
    def id_token(self) -> str | None:
        tokens = self.tokens or {}
        value = tokens.get("id_token")
        if isinstance(value, dict):
            return value.get("raw_jwt")
        return value if isinstance(value, str) else None

    @property
    def access_token(self) -> str | None:
        tokens = self.tokens or {}
        value = tokens.get("access_token")
        return value if isinstance(value, str) and value else None

    @property
    def refresh_token(self) -> str | None:
        tokens = self.tokens or {}
        value = tokens.get("refresh_token")
        return value if isinstance(value, str) and value else None

    @property
    def account_id(self) -> str | None:
        tokens = self.tokens or {}
        value = tokens.get("account_id")
        if isinstance(value, str) and value:
            return value
        id_token = self.id_token
        if not id_token:
            return None
        return parse_id_token(id_token).get("chatgpt_account_id")

    @property
    def email(self) -> str | None:
        id_token = self.id_token
        if not id_token:
            return None
        return parse_id_token(id_token).get("email")

    @property
    def plan_type(self) -> str | None:
        id_token = self.id_token
        if not id_token:
            return None
        return parse_id_token(id_token).get("chatgpt_plan_type")

    @property
    def bound_account_id(self) -> str | None:
        value = self.raw.get("bound_account_id")
        if isinstance(value, str) and value:
            return value
        return self.account_id

    @property
    def last_refresh(self) -> datetime | None:
        value = self.raw.get("last_refresh")
        return _parse_timestamp(value if isinstance(value, str) else None)

    @property
    def authenticated(self) -> bool:
        return bool(self.tokens and (self.access_token or self.refresh_token))

    def account_matches_binding(self) -> bool:
        if not self.bound_account_id:
            return bool(self.account_id)
        return self.account_id == self.bound_account_id


class AuthStorage:
    def __init__(self, storage_root: Path, mode: str) -> None:
        self.storage_root = storage_root.expanduser()
        self.mode = mode

    @property
    def auth_path(self) -> Path:
        return self.storage_root / "auth.json"

    def load(self) -> dict[str, Any] | None:
        if self.mode == "file":
            return self._load_file()
        if self.mode == "keyring":
            return self._load_keyring()
        try:
            keyring_payload = self._load_keyring()
        except RuntimeError:
            keyring_payload = None
        return keyring_payload or self._load_file()

    def save(self, payload: dict[str, Any]) -> None:
        if self.mode == "file":
            self._save_file(payload)
            return
        if self.mode == "keyring":
            self._save_keyring(payload)
            return
        try:
            self._save_keyring(payload)
            self._delete_file()
        except RuntimeError:
            self._save_file(payload)

    def delete(self) -> bool:
        if self.mode == "file":
            return self._delete_file()
        if self.mode == "keyring":
            return self._delete_keyring()
        deleted_keyring = self._delete_keyring()
        deleted_file = self._delete_file()
        return deleted_keyring or deleted_file

    def _load_file(self) -> dict[str, Any] | None:
        if not self.auth_path.exists():
            return None
        try:
            return json.loads(self.auth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Stored AIRelays auth at {self.auth_path} is not valid JSON.") from exc

    def _save_file(self, payload: dict[str, Any]) -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.auth_path.parent,
            prefix=f".{self.auth_path.name}.",
            delete=False,
        ) as handle:
            handle.write(serialized)
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.auth_path)

    def _delete_file(self) -> bool:
        if not self.auth_path.exists():
            return False
        self.auth_path.unlink()
        return True

    def _load_keyring(self) -> dict[str, Any] | None:
        username = _compute_store_key(self.storage_root)
        try:
            serialized = keyring.get_password(KEYRING_SERVICE, username)
        except KeyringError as exc:
            raise RuntimeError(str(exc)) from exc
        if not serialized:
            serialized = self._migrate_legacy_keyring_payload(username)
            if not serialized:
                return None
        return json.loads(serialized)

    def _save_keyring(self, payload: dict[str, Any]) -> None:
        username = _compute_store_key(self.storage_root)
        serialized = json.dumps(payload, ensure_ascii=True)
        try:
            keyring.set_password(KEYRING_SERVICE, username, serialized)
        except KeyringError as exc:
            raise RuntimeError(str(exc)) from exc
        self._delete_legacy_keyring(username)

    def _delete_keyring(self) -> bool:
        username = _compute_store_key(self.storage_root)
        try:
            existing = keyring.get_password(KEYRING_SERVICE, username)
            deleted_current = False
            if existing is not None:
                keyring.delete_password(KEYRING_SERVICE, username)
                deleted_current = True
            deleted_legacy = self._delete_legacy_keyring(username)
            return deleted_current or deleted_legacy
        except KeyringError:
            return False

    def _migrate_legacy_keyring_payload(self, username: str) -> str | None:
        for service in LEGACY_KEYRING_SERVICES:
            try:
                serialized = keyring.get_password(service, username)
            except KeyringError:
                continue
            if not serialized:
                continue
            try:
                keyring.set_password(KEYRING_SERVICE, username, serialized)
            except KeyringError:
                return serialized
            self._delete_legacy_keyring(username, services=(service,))
            return serialized
        return None

    def _delete_legacy_keyring(
        self,
        username: str,
        *,
        services: tuple[str, ...] = LEGACY_KEYRING_SERVICES,
    ) -> bool:
        deleted = False
        for service in services:
            try:
                existing = keyring.get_password(service, username)
                if existing is None:
                    continue
                keyring.delete_password(service, username)
                deleted = True
            except KeyringError:
                continue
        return deleted


class LoginCallbackServer:
    def __init__(self, port: int = BROWSER_CALLBACK_PORT) -> None:
        self.state: str | None = None
        self.code: str | None = None
        self.error: str | None = None
        self._event = threading.Event()
        self.requested_port = port

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                if parsed.path == "/auth/callback":
                    outer.state = params.get("state", [None])[0]
                    outer.code = params.get("code", [None])[0]
                    outer.error = params.get("error", [None])[0]
                    body = (
                        "<html><body><h1>Login completed</h1>"
                        "<p>You can return to the terminal.</p></body></html>"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body.encode("utf-8"))
                    outer._event.set()
                    return
                body = "Not Found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        self._servers = self._bind_servers(Handler, IPv6ThreadingHTTPServer, port)
        self.port = self._servers[0].server_address[1]
        self._threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in self._servers
        ]

    def _bind_servers(
        self,
        handler: type[BaseHTTPRequestHandler],
        ipv6_server_cls: type[ThreadingHTTPServer],
        port: int,
    ) -> list[ThreadingHTTPServer]:
        errors: list[OSError] = []

        for primary, secondary in (
            (lambda: ipv6_server_cls(("::1", port), handler), lambda bound_port: ThreadingHTTPServer(("127.0.0.1", bound_port), handler)),
            (lambda: ThreadingHTTPServer(("127.0.0.1", port), handler), lambda bound_port: ipv6_server_cls(("::1", bound_port), handler)),
        ):
            try:
                first = primary()
            except OSError as exc:
                errors.append(exc)
                continue

            servers = [first]
            port = first.server_address[1]
            try:
                servers.append(secondary(port))
            except OSError:
                # A single localhost-family listener is still useful, and on some
                # systems the primary IPv6 listener already accepts IPv4.
                pass
            return servers

        raise OSError(
            f"Unable to bind the AIRelays login callback server on localhost:{port}."
        ) from errors[-1]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def wait(self, timeout_seconds: float) -> tuple[str | None, str | None]:
        self._event.wait(timeout_seconds)
        return self.code, self.error

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
        for server in self._servers:
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)


class AuthManager:
    def __init__(
        self,
        storage_root: Path,
        storage_mode: str,
        issuer_base_url: str,
        client_id: str = OPENAI_SUBSCRIPTION_CLIENT_ID,
    ) -> None:
        self.storage_root = storage_root.expanduser()
        self.storage = AuthStorage(self.storage_root, storage_mode)
        self.issuer_base_url = issuer_base_url.rstrip("/")
        self.client_id = client_id
        self._refresh_lock = asyncio.Lock()

    def load(self) -> AuthRecord | None:
        payload = self.storage.load()
        if payload is None:
            return None
        return AuthRecord(payload)

    def status(self) -> dict[str, Any]:
        record = self.load()
        if record is None:
            return {
                "authenticated": False,
                "credentials_present": False,
                "account_bound": False,
                "ready_for_requests": False,
                "email": None,
                "plan_type": None,
                "account_id": None,
                "bound_account_id": None,
                "last_refresh": None,
                "auth_store_path": str(self.storage.auth_path),
                "keyring_service": KEYRING_SERVICE,
                "storage_mode": self.storage.mode,
            }
        account_bound = record.account_matches_binding()
        return {
            "authenticated": record.authenticated,
            "credentials_present": bool(record.tokens),
            "account_bound": account_bound,
            "ready_for_requests": record.authenticated and account_bound,
            "email": record.email,
            "plan_type": record.plan_type,
            "account_id": record.account_id,
            "bound_account_id": record.bound_account_id,
            "last_refresh": record.last_refresh.isoformat() if record.last_refresh else None,
            "auth_store_path": str(self.storage.auth_path),
            "keyring_service": KEYRING_SERVICE,
            "storage_mode": self.storage.mode,
        }

    def logout(self) -> bool:
        return self.storage.delete()

    async def ensure_fresh_tokens(self) -> AuthRecord:
        record = self.load()
        if record is None or not record.tokens:
            raise AuthenticationError(
                "No ChatGPT login found. Run `airelays login` first.",
                code="upstream_auth_missing",
            )
        if not record.access_token:
            if not record.refresh_token:
                raise AuthenticationError(
                    "Stored auth does not include an access token or refresh token.",
                    code="upstream_auth_incomplete",
                )
            record = await self.refresh_tokens(force=True)
        last_refresh = record.last_refresh
        if last_refresh is None or last_refresh < _utcnow() - timedelta(days=TOKEN_REFRESH_INTERVAL_DAYS):
            record = await self.refresh_tokens(force=False)
        if not record.access_token:
            raise AuthenticationError(
                "Stored auth does not include a usable access token.",
                code="upstream_auth_incomplete",
            )
        if not record.account_matches_binding():
            raise AuthenticationError(
                "Stored AIRelays auth is bound to a different upstream account than the active token.",
                code="upstream_auth_account_mismatch",
            )
        return record

    async def refresh_tokens(self, force: bool = True) -> AuthRecord:
        async with self._refresh_lock:
            record = self.load()
            if record is None or not record.refresh_token:
                raise AuthenticationError(
                    "Stored auth does not include a refresh token.",
                    code="upstream_auth_incomplete",
                )
            last_refresh = record.last_refresh
            if (
                not force
                and last_refresh is not None
                and last_refresh >= _utcnow() - timedelta(
                days=TOKEN_REFRESH_INTERVAL_DAYS
                )
            ):
                return record
            payload = {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": record.refresh_token,
                "scope": "openid profile email",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.issuer_base_url}/oauth/token",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            if response.status_code >= 400:
                raise AuthenticationError(
                    f"Token refresh failed: {response.status_code} {response.text}",
                    code="upstream_auth_refresh_failed",
                )
            data = response.json()
            refreshed = self._build_auth_payload(
                id_token=data.get("id_token") or record.id_token,
                access_token=data.get("access_token") or record.access_token,
                refresh_token=data.get("refresh_token") or record.refresh_token,
                bound_account_id=record.bound_account_id,
            )
            self.storage.save(refreshed)
            return AuthRecord(refreshed)

    async def browser_login(
        self,
        client_id: str,
        open_browser: bool,
        timeout_seconds: float,
        workspace_id: str | None = None,
        on_authorize_url: Callable[[str], None] | None = None,
    ) -> AuthRecord:
        code_verifier, code_challenge = _generate_pkce_pair()
        state = _generate_state()
        try:
            callback = LoginCallbackServer()
        except OSError as exc:
            raise AuthenticationError(
                "AIRelays could not bind localhost:1455 for browser login. "
                "Another login flow may already be using that port. Retry later or use "
                "`airelays login --device`."
            ) from exc
        callback.start()
        redirect_uri = _browser_redirect_uri(callback.port)
        auth_url = _build_browser_authorize_url(
            issuer_base_url=self.issuer_base_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            workspace_id=workspace_id,
        )
        if open_browser:
            webbrowser.open(auth_url)
        if on_authorize_url is None:
            print(f"Open this URL to continue login:\n{auth_url}")
        else:
            on_authorize_url(auth_url)
        try:
            code, error = await asyncio.to_thread(callback.wait, timeout_seconds)
        finally:
            callback.close()
        if error:
            raise AuthenticationError(f"Login failed: {error}")
        if not code or callback.state != state:
            raise AuthenticationError(
                "Login did not complete or returned an invalid state value."
            )
        tokens = await self._exchange_code_for_tokens(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code=code,
            code_verifier=code_verifier,
        )
        account_id = parse_id_token(tokens["id_token"]).get("chatgpt_account_id")
        if workspace_id and account_id != workspace_id:
            raise AuthenticationError(f"Login is restricted to workspace id {workspace_id}.")
        payload = self._build_auth_payload(
            id_token=tokens["id_token"],
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            bound_account_id=account_id,
        )
        self.storage.save(payload)
        return AuthRecord(payload)

    async def device_login(
        self,
        client_id: str,
        timeout_seconds: float,
        workspace_id: str | None = None,
        on_device_code: Callable[[str, str], None] | None = None,
    ) -> AuthRecord:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.issuer_base_url}/api/accounts/deviceauth/usercode",
                json={"client_id": client_id},
            )
            if response.status_code == 404:
                raise AuthenticationError("Device-code login is not enabled for this server.")
            response.raise_for_status()
            usercode = response.json()
            verification_url = f"{self.issuer_base_url}/codex/device"
            if on_device_code is None:
                print(f"Open {verification_url} and enter code: {usercode['user_code']}")
            else:
                on_device_code(verification_url, usercode["user_code"])
            deadline = time.monotonic() + timeout_seconds
            interval = int(usercode.get("interval") or 5)
            code_payload: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                poll = await client.post(
                    f"{self.issuer_base_url}/api/accounts/deviceauth/token",
                    json={
                        "device_auth_id": usercode["device_auth_id"],
                        "user_code": usercode["user_code"],
                    },
                )
                if poll.status_code in {403, 404}:
                    await asyncio.sleep(interval)
                    continue
                poll.raise_for_status()
                code_payload = poll.json()
                break
            if not code_payload:
                raise AuthenticationError("Device-code login timed out after 15 minutes.")
            tokens = await self._exchange_code_for_tokens(
                client_id=client_id,
                redirect_uri=f"{self.issuer_base_url}/deviceauth/callback",
                code=code_payload["authorization_code"],
                code_verifier=code_payload["code_verifier"],
            )
        account_id = parse_id_token(tokens["id_token"]).get("chatgpt_account_id")
        if workspace_id and account_id != workspace_id:
            raise AuthenticationError(f"Login is restricted to workspace id {workspace_id}.")
        payload = self._build_auth_payload(
            id_token=tokens["id_token"],
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            bound_account_id=account_id,
        )
        self.storage.save(payload)
        return AuthRecord(payload)

    async def _exchange_code_for_tokens(
        self,
        client_id: str,
        redirect_uri: str,
        code: str,
        code_verifier: str,
    ) -> dict[str, str]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.issuer_base_url}/oauth/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            raise AuthenticationError(
                f"Code exchange failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        return {
            "id_token": payload["id_token"],
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
        }

    def _build_auth_payload(
        self,
        id_token: str | None,
        access_token: str | None,
        refresh_token: str | None,
        bound_account_id: str | None = None,
    ) -> dict[str, Any]:
        if not id_token or not access_token or not refresh_token:
            raise AuthenticationError("Missing token fields while building auth payload.")
        parsed = parse_id_token(id_token)
        parsed_account_id = parsed.get("chatgpt_account_id")
        return {
            "bound_account_id": bound_account_id or parsed_account_id,
            "tokens": {
                "id_token": id_token,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": parsed_account_id,
            },
            "last_refresh": _utcnow().isoformat(),
        }
