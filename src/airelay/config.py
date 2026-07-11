from __future__ import annotations

import hashlib
import os
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_AUTH_ISSUER = "https://auth.openai.com"
OPENAI_SUBSCRIPTION_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
MIN_CHATGPT_CLIENT_VERSION = "0.124.0"
APP_NAME = "AIRelays"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "airelays" / "config.toml"
DEFAULT_DATA_DIR = Path.home() / ".airelays"
LEGACY_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "airelay" / "config.toml"
LEGACY_DEFAULT_DATA_DIR = Path.home() / ".airelay"
DEFAULT_CLAUDE_MODELS = (
    "claude:sonnet",
    "claude:opus",
    "claude:haiku",
    "claude:fable",
)


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
    return None


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _float(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def _cfg(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for segment in path:
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _normalized_balance(value: Any) -> str:
    """Balancing strategy, normalized and validated. A typo must fail loudly
    at startup rather than silently selecting a different routing policy."""
    if value is None:
        return "round_robin"
    text = str(value).strip().lower().replace("-", "_")
    if text in {"round_robin", "ordered"}:
        return text
    raise ValueError(
        f"Invalid [providers.openai] balance value {value!r}: "
        "use \"round_robin\" or \"ordered\"."
    )


def _path(value: Any, default: Path) -> Path:
    if value is None:
        return default.expanduser()
    return Path(str(value)).expanduser()


def _str_list(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        items = tuple(segment.strip() for segment in value.split(",") if segment.strip())
        return items or default
    if isinstance(value, (list, tuple)):
        items = tuple(str(segment).strip() for segment in value if str(segment).strip())
        return items or default
    return default


def _preferred_default_path(current: Path, legacy: Path) -> Path:
    expanded_current = current.expanduser()
    expanded_legacy = legacy.expanduser()
    if expanded_current.exists():
        return expanded_current
    if expanded_legacy.exists():
        return expanded_legacy
    return expanded_current


def _token_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8080
    config_path: Path = DEFAULT_CONFIG_PATH
    auth_storage_mode: str = "auto"
    data_dir: Path = DEFAULT_DATA_DIR
    logs_dir: Path = DEFAULT_DATA_DIR / "logs"
    upstream_base_url: str = CHATGPT_CODEX_BASE_URL
    issuer_base_url: str = OPENAI_AUTH_ISSUER
    client_id: str = OPENAI_SUBSCRIPTION_CLIENT_ID
    client_version: str = MIN_CHATGPT_CLIENT_VERSION
    request_timeout_seconds: float = 120.0
    login_timeout_seconds: float = 900.0
    browser_open: bool = False
    require_bearer_auth: bool = True
    bearer_token: str | None = None
    bearer_token_env_override: bool = False
    bearer_token_file: Path = DEFAULT_DATA_DIR / "relay-token"
    # Claude Code OAuth token stored by `airelays claude set-token`. A file
    # survives service managers (systemd/docker) where shell exports do not.
    claude_oauth_token_file: Path = DEFAULT_DATA_DIR / "claude-token"
    auto_generate_bearer_token: bool = False
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 40
    concurrent_requests_per_ip: int = 50
    failed_auth_window_seconds: int = 300
    failed_auth_max_attempts: int = 8
    failed_auth_block_seconds: int = 900
    trust_x_forwarded_for: bool = False
    max_upload_bytes: int = 32 * 1024 * 1024
    max_total_upload_bytes: int = 256 * 1024 * 1024
    # Log every raw upstream SSE line to the traffic log. Invaluable for
    # deep protocol debugging but enormous under load (a single streamed
    # response is hundreds of lines), so it is opt-in. The per-request
    # summary records (request, usage, response, errors) are always logged.
    log_stream_lines: bool = False
    enable_openai_provider: bool = True
    models_cache_ttl_seconds: float = 300.0
    # Multiple own accounts: "round_robin" (default) spreads requests across
    # healthy accounts so charge is always balanced; "ordered" uses the first
    # account until it hits its usage limit, then continues with the next.
    openai_balance: str = "round_robin"
    openai_account_cooldown_seconds: int = 300
    enable_claude: bool = True
    claude_bin: str = "claude"
    claude_timeout_seconds: float = 600.0
    claude_max_concurrent_requests: int = 2
    claude_strip_api_key_env: bool = True
    claude_models: tuple[str, ...] = DEFAULT_CLAUDE_MODELS

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.from_sources()

    @classmethod
    def from_sources(cls, config_path: Path | None = None) -> "Settings":
        configured_path = _path(
            config_path
            or _env("AIRELAYS_CONFIG", "AIRELAY_CONFIG", "OPENAI_ENDPOINT_CONFIG"),
            _preferred_default_path(DEFAULT_CONFIG_PATH, LEGACY_DEFAULT_CONFIG_PATH),
        )
        payload: dict[str, Any] = {}
        if configured_path.exists():
            with configured_path.open("rb") as handle:
                loaded = tomllib.load(handle)
            if isinstance(loaded, dict):
                payload = loaded

        data_dir = _path(
            _env("AIRELAYS_DATA_DIR", "AIRELAY_DATA_DIR", "OPENAI_ENDPOINT_DATA_DIR")
            or _cfg(payload, "paths", "data_dir"),
            _preferred_default_path(DEFAULT_DATA_DIR, LEGACY_DEFAULT_DATA_DIR),
        )
        logs_dir = _path(
            _env("AIRELAYS_LOGS_DIR", "AIRELAY_LOGS_DIR", "OPENAI_ENDPOINT_LOGS_DIR")
            or _cfg(payload, "paths", "logs_dir"),
            data_dir / "logs",
        )
        bearer_token_file = _path(
            _env("AIRELAYS_BEARER_TOKEN_FILE", "AIRELAY_BEARER_TOKEN_FILE")
            or _cfg(payload, "security", "bearer_token_file"),
            data_dir / "relay-token",
        )
        claude_oauth_token_file = _path(
            _env("AIRELAYS_CLAUDE_OAUTH_TOKEN_FILE")
            or _cfg(payload, "providers", "claude", "oauth_token_file"),
            data_dir / "claude-token",
        )

        env_bearer_token = _env("AIRELAYS_BEARER_TOKEN", "AIRELAY_BEARER_TOKEN")

        return cls(
            host=str(
                _env("AIRELAYS_HOST", "AIRELAY_HOST", "OPENAI_ENDPOINT_HOST")
                or _cfg(payload, "server", "host")
                or "127.0.0.1"
            ),
            port=_int(
                _env("AIRELAYS_PORT", "AIRELAY_PORT", "OPENAI_ENDPOINT_PORT")
                or _cfg(payload, "server", "port"),
                8080,
            ),
            config_path=configured_path,
            auth_storage_mode=str(
                _env("AIRELAYS_AUTH_STORAGE", "AIRELAY_AUTH_STORAGE", "OPENAI_ENDPOINT_AUTH_STORAGE")
                or _cfg(payload, "auth", "storage")
                or "auto"
            ).lower(),
            data_dir=data_dir,
            logs_dir=logs_dir,
            upstream_base_url=str(
                _env(
                    "AIRELAYS_UPSTREAM_BASE_URL",
                    "AIRELAY_UPSTREAM_BASE_URL",
                    "OPENAI_ENDPOINT_UPSTREAM_BASE_URL",
                )
                or _cfg(payload, "upstream", "base_url")
                or CHATGPT_CODEX_BASE_URL
            ).rstrip("/"),
            issuer_base_url=str(
                _env(
                    "AIRELAYS_ISSUER_BASE_URL",
                    "AIRELAY_ISSUER_BASE_URL",
                    "OPENAI_ENDPOINT_ISSUER_BASE_URL",
                )
                or _cfg(payload, "upstream", "issuer_base_url")
                or OPENAI_AUTH_ISSUER
            ).rstrip("/"),
            client_id=str(
                _env("AIRELAYS_CLIENT_ID", "AIRELAY_CLIENT_ID", "OPENAI_ENDPOINT_CLIENT_ID")
                or _cfg(payload, "upstream", "client_id")
                or OPENAI_SUBSCRIPTION_CLIENT_ID
            ),
            client_version=str(
                _env(
                    "AIRELAYS_CLIENT_VERSION",
                    "AIRELAY_CLIENT_VERSION",
                    "OPENAI_ENDPOINT_CLIENT_VERSION",
                )
                or _cfg(payload, "upstream", "client_version")
                or MIN_CHATGPT_CLIENT_VERSION
            ),
            request_timeout_seconds=_float(
                _env(
                    "AIRELAYS_REQUEST_TIMEOUT_SECONDS",
                    "AIRELAY_REQUEST_TIMEOUT_SECONDS",
                    "OPENAI_ENDPOINT_REQUEST_TIMEOUT_SECONDS",
                )
                or _cfg(payload, "upstream", "request_timeout_seconds"),
                120.0,
            ),
            login_timeout_seconds=_float(
                _env(
                    "AIRELAYS_LOGIN_TIMEOUT_SECONDS",
                    "AIRELAY_LOGIN_TIMEOUT_SECONDS",
                    "OPENAI_ENDPOINT_LOGIN_TIMEOUT_SECONDS",
                )
                or _cfg(payload, "auth", "login_timeout_seconds"),
                900.0,
            ),
            browser_open=_bool(
                _env("AIRELAYS_BROWSER_OPEN", "AIRELAY_BROWSER_OPEN", "OPENAI_ENDPOINT_BROWSER_OPEN")
                or _cfg(payload, "auth", "browser_open"),
                False,
            ),
            require_bearer_auth=_bool(
                _env("AIRELAYS_REQUIRE_BEARER_AUTH", "AIRELAY_REQUIRE_BEARER_AUTH")
                or _cfg(payload, "security", "require_bearer_auth"),
                True,
            ),
            bearer_token=env_bearer_token,
            bearer_token_env_override=env_bearer_token is not None,
            bearer_token_file=bearer_token_file,
            claude_oauth_token_file=claude_oauth_token_file,
            auto_generate_bearer_token=_bool(
                _env("AIRELAYS_AUTO_GENERATE_BEARER_TOKEN", "AIRELAY_AUTO_GENERATE_BEARER_TOKEN")
                or _cfg(payload, "security", "auto_generate_bearer_token"),
                False,
            ),
            rate_limit_per_minute=_int(
                _env("AIRELAYS_RATE_LIMIT_PER_MINUTE", "AIRELAY_RATE_LIMIT_PER_MINUTE")
                or _cfg(payload, "security", "rate_limit_per_minute"),
                120,
            ),
            rate_limit_burst=_int(
                _env("AIRELAYS_RATE_LIMIT_BURST", "AIRELAY_RATE_LIMIT_BURST")
                or _cfg(payload, "security", "rate_limit_burst"),
                40,
            ),
            concurrent_requests_per_ip=_int(
                _env("AIRELAYS_CONCURRENT_REQUESTS_PER_IP", "AIRELAY_CONCURRENT_REQUESTS_PER_IP")
                or _cfg(payload, "security", "concurrent_requests_per_ip"),
                50,
            ),
            failed_auth_window_seconds=_int(
                _env("AIRELAYS_FAILED_AUTH_WINDOW_SECONDS", "AIRELAY_FAILED_AUTH_WINDOW_SECONDS")
                or _cfg(payload, "security", "failed_auth_window_seconds"),
                300,
            ),
            failed_auth_max_attempts=_int(
                _env("AIRELAYS_FAILED_AUTH_MAX_ATTEMPTS", "AIRELAY_FAILED_AUTH_MAX_ATTEMPTS")
                or _cfg(payload, "security", "failed_auth_max_attempts"),
                8,
            ),
            failed_auth_block_seconds=_int(
                _env("AIRELAYS_FAILED_AUTH_BLOCK_SECONDS", "AIRELAY_FAILED_AUTH_BLOCK_SECONDS")
                or _cfg(payload, "security", "failed_auth_block_seconds"),
                900,
            ),
            trust_x_forwarded_for=_bool(
                _env("AIRELAYS_TRUST_X_FORWARDED_FOR", "AIRELAY_TRUST_X_FORWARDED_FOR")
                or _cfg(payload, "security", "trust_x_forwarded_for"),
                False,
            ),
            max_upload_bytes=_int(
                _env("AIRELAYS_MAX_UPLOAD_BYTES", "AIRELAY_MAX_UPLOAD_BYTES")
                or _cfg(payload, "uploads", "max_upload_bytes"),
                32 * 1024 * 1024,
            ),
            max_total_upload_bytes=_int(
                _env("AIRELAYS_MAX_TOTAL_UPLOAD_BYTES", "AIRELAY_MAX_TOTAL_UPLOAD_BYTES")
                or _cfg(payload, "uploads", "max_total_upload_bytes"),
                256 * 1024 * 1024,
            ),
            log_stream_lines=_bool(
                _env("AIRELAYS_LOG_STREAM_LINES")
                or _cfg(payload, "logging", "stream_lines"),
                False,
            ),
            enable_openai_provider=_bool(
                _env("AIRELAYS_ENABLE_OPENAI", "AIRELAY_ENABLE_OPENAI")
                or _cfg(payload, "providers", "openai", "enabled"),
                True,
            ),
            models_cache_ttl_seconds=_float(
                _env(
                    "AIRELAYS_MODELS_CACHE_TTL_SECONDS",
                    "AIRELAYS_OPENAI_MODELS_CACHE_TTL_SECONDS",
                    "AIRELAY_MODELS_CACHE_TTL_SECONDS",
                    "AIRELAY_OPENAI_MODELS_CACHE_TTL_SECONDS",
                )
                or _cfg(payload, "providers", "openai", "models_cache_ttl_seconds"),
                300.0,
            ),
            openai_balance=_normalized_balance(
                _env("AIRELAYS_OPENAI_BALANCE")
                or _cfg(payload, "providers", "openai", "balance")
            ),
            openai_account_cooldown_seconds=_int(
                _env("AIRELAYS_OPENAI_ACCOUNT_COOLDOWN_SECONDS")
                or _cfg(payload, "providers", "openai", "account_cooldown_seconds"),
                300,
            ),
            enable_claude=_bool(
                _env(
                    "AIRELAYS_ENABLE_CLAUDE",
                    # Legacy names from when the Claude runtime carried the
                    # "experimental" label; still honored so existing
                    # environments keep working.
                    "AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL",
                    "AIRELAY_ENABLE_CLAUDE_EXPERIMENTAL",
                )
                or _cfg(payload, "providers", "claude", "enabled"),
                True,
            ),
            claude_bin=str(
                _env("AIRELAYS_CLAUDE_BIN", "AIRELAY_CLAUDE_BIN")
                or _cfg(payload, "providers", "claude", "bin")
                or "claude"
            ),
            claude_timeout_seconds=_float(
                _env(
                    "AIRELAYS_CLAUDE_TIMEOUT_SECONDS",
                    "AIRELAY_CLAUDE_TIMEOUT_SECONDS",
                )
                or _cfg(payload, "providers", "claude", "timeout_seconds"),
                600.0,
            ),
            claude_max_concurrent_requests=_int(
                _env(
                    "AIRELAYS_CLAUDE_MAX_CONCURRENT_REQUESTS",
                    "AIRELAY_CLAUDE_MAX_CONCURRENT_REQUESTS",
                )
                or _cfg(payload, "providers", "claude", "max_concurrent_requests"),
                2,
            ),
            claude_strip_api_key_env=_bool(
                _env(
                    "AIRELAYS_CLAUDE_STRIP_API_KEY_ENV",
                    "AIRELAY_CLAUDE_STRIP_API_KEY_ENV",
                )
                or _cfg(payload, "providers", "claude", "strip_api_key_env"),
                True,
            ),
            claude_models=_str_list(
                _env("AIRELAYS_CLAUDE_MODELS", "AIRELAY_CLAUDE_MODELS")
                or _cfg(payload, "providers", "claude", "models"),
                DEFAULT_CLAUDE_MODELS,
            ),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def resolve_bearer_token(self) -> str | None:
        if self.bearer_token_env_override and self.bearer_token:
            return self.bearer_token
        if self.bearer_token:
            return self.bearer_token
        if self.bearer_token_file.exists():
            token = self.bearer_token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        return None

    def bearer_token_source(self) -> str | None:
        if self.bearer_token_env_override and self.bearer_token:
            return "env"
        if self.bearer_token:
            return "memory"
        if self.bearer_token_file.exists():
            token = self.bearer_token_file.read_text(encoding="utf-8").strip()
            if token:
                return "file"
        return None

    def ensure_bearer_token(self) -> str | None:
        if not self.require_bearer_auth:
            return None
        token = self.resolve_bearer_token()
        if token:
            return token
        if not self.auto_generate_bearer_token:
            raise RuntimeError(
                "Bearer authentication is enabled, but no relay token is configured. "
                "Run `airelays init` or set AIRELAYS_BEARER_TOKEN."
            )
        token = secrets.token_urlsafe(32)
        self.write_bearer_token(token)
        return token

    def resolve_claude_oauth_token(self) -> str | None:
        """Token stored via `airelays claude set-token`; env keeps working
        as a fallback for existing setups, but the file wins because it is
        explicit configuration."""
        if self.claude_oauth_token_file.exists():
            token = self.claude_oauth_token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        return None

    def write_claude_oauth_token(self, token: str) -> None:
        self.ensure_directories()
        self.claude_oauth_token_file.parent.mkdir(parents=True, exist_ok=True)
        self.claude_oauth_token_file.write_text(f"{token}\n", encoding="utf-8")
        os.chmod(self.claude_oauth_token_file, 0o600)

    def claude_oauth_token_source(self) -> str:
        if self.resolve_claude_oauth_token():
            return "file"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "env"
        return "none"

    def write_bearer_token(self, token: str) -> None:
        self.ensure_directories()
        self.bearer_token_file.parent.mkdir(parents=True, exist_ok=True)
        self.bearer_token_file.write_text(f"{token}\n", encoding="utf-8")
        os.chmod(self.bearer_token_file, 0o600)
        if self.bearer_token_env_override or self.bearer_token is not None:
            self.bearer_token = token

    def rotate_bearer_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self.write_bearer_token(token)
        return token

    def ensure_runtime_state(self) -> str | None:
        self.ensure_directories()
        return self.ensure_bearer_token()

    def is_loopback_host(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    def validate_provider_guardrails(self) -> None:
        if not self.enable_claude:
            return
        if not self.is_loopback_host():
            raise RuntimeError(
                "The Claude runtime is restricted to loopback listeners. "
                "Use `127.0.0.1`, `localhost`, or `::1`."
            )
        if self.trust_x_forwarded_for:
            raise RuntimeError(
                "The Claude runtime does not allow `trust_x_forwarded_for`."
            )

    def write_config_file(self, force: bool = False) -> bool:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists() and not force:
            return False
        self.config_path.write_text(self.render_config_toml(), encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        return True

    def auth_file(self) -> Path:
        return self.data_dir / "auth.json"

    def render_config_toml(self) -> str:
        return f"""[server]
host = "{self.host}"
port = {self.port}

[paths]
data_dir = "{self.data_dir}"
logs_dir = "{self.logs_dir}"

[auth]
storage = "{self.auth_storage_mode}"
browser_open = {str(self.browser_open).lower()}
login_timeout_seconds = {int(self.login_timeout_seconds)}

[upstream]
base_url = "{self.upstream_base_url}"
issuer_base_url = "{self.issuer_base_url}"
client_id = "{self.client_id}"
client_version = "{self.client_version}"
request_timeout_seconds = {self.request_timeout_seconds}

[security]
require_bearer_auth = {str(self.require_bearer_auth).lower()}
bearer_token_file = "{self.bearer_token_file}"
auto_generate_bearer_token = {str(self.auto_generate_bearer_token).lower()}
rate_limit_per_minute = {self.rate_limit_per_minute}
rate_limit_burst = {self.rate_limit_burst}
concurrent_requests_per_ip = {self.concurrent_requests_per_ip}
failed_auth_window_seconds = {self.failed_auth_window_seconds}
failed_auth_max_attempts = {self.failed_auth_max_attempts}
failed_auth_block_seconds = {self.failed_auth_block_seconds}
trust_x_forwarded_for = {str(self.trust_x_forwarded_for).lower()}

[uploads]
max_upload_bytes = {self.max_upload_bytes}
max_total_upload_bytes = {self.max_total_upload_bytes}

[logging]
stream_lines = {str(self.log_stream_lines).lower()}

[providers.openai]
enabled = {str(self.enable_openai_provider).lower()}
models_cache_ttl_seconds = {self.models_cache_ttl_seconds}
balance = "{self.openai_balance}"
account_cooldown_seconds = {self.openai_account_cooldown_seconds}

[providers.claude]
enabled = {str(self.enable_claude).lower()}
oauth_token_file = "{self.claude_oauth_token_file}"
bin = "{self.claude_bin}"
timeout_seconds = {self.claude_timeout_seconds}
max_concurrent_requests = {self.claude_max_concurrent_requests}
strip_api_key_env = {str(self.claude_strip_api_key_env).lower()}
models = [{", ".join(f'"{model}"' for model in self.claude_models)}]
"""

    def client_base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def summary(self) -> dict[str, Any]:
        return {
            "app_name": APP_NAME,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "host": self.host,
            "port": self.port,
            "auth_storage_mode": self.auth_storage_mode,
            "data_dir": str(self.data_dir),
            "auth_file": str(self.auth_file()),
            "logs_dir": str(self.logs_dir),
            "browser_open": self.browser_open,
            "require_bearer_auth": self.require_bearer_auth,
            "bearer_token_file": str(self.bearer_token_file),
            "bearer_token_present": bool(self.resolve_bearer_token()),
            "bearer_token_source": self.bearer_token_source(),
            "bearer_token_fingerprint": _token_fingerprint(self.resolve_bearer_token()),
            "auto_generate_bearer_token": self.auto_generate_bearer_token,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_burst": self.rate_limit_burst,
            "concurrent_requests_per_ip": self.concurrent_requests_per_ip,
            "failed_auth_window_seconds": self.failed_auth_window_seconds,
            "failed_auth_max_attempts": self.failed_auth_max_attempts,
            "failed_auth_block_seconds": self.failed_auth_block_seconds,
            "trust_x_forwarded_for": self.trust_x_forwarded_for,
            "max_upload_bytes": self.max_upload_bytes,
            "max_total_upload_bytes": self.max_total_upload_bytes,
            "client_base_url": self.client_base_url(),
            "providers": {
                "openai": {
                    "enabled": self.enable_openai_provider,
                    "models_cache_ttl_seconds": self.models_cache_ttl_seconds,
                    "balance": self.openai_balance,
                    "account_cooldown_seconds": self.openai_account_cooldown_seconds,
                },
                "claude": {
                    "enabled": self.enable_claude,
                    "bin": self.claude_bin,
                    "timeout_seconds": self.claude_timeout_seconds,
                    "max_concurrent_requests": self.claude_max_concurrent_requests,
                    "strip_api_key_env": self.claude_strip_api_key_env,
                    "models": list(self.claude_models),
                    # Presence + fingerprint only; the token itself must
                    # never appear in status output or logs.
                    "oauth_token_source": self.claude_oauth_token_source(),
                    "oauth_token_fingerprint": _token_fingerprint(self.resolve_claude_oauth_token()),
                },
            },
        }
