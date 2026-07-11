//! App settings: persisted JSON mirroring the relay's config surface.
//!
//! The desktop app owns a settings file in the platform config directory and
//! renders the relay's `config.toml` from it before every launch, exactly
//! like the macOS menu bar app does.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Default relay port: IANA-unregistered and not a common tool default,
/// chosen to avoid the dev-server collisions that 8080 invites.
pub const DEFAULT_RELAY_PORT: u16 = 8317;

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct AppSettings {
    /// Optional override for the relay launch command. Empty means
    /// "resolve automatically" (embedded runtime, then PATH).
    pub relay_command_override: String,
    /// OpenAI sign-in method: "browser" (local browser flow) or "device"
    /// (code you approve from any other device — headless-friendly).
    pub login_method: String,
    /// Start the relay automatically when the app opens (unless a relay is
    /// already answering on the configured address).
    pub start_relay_on_launch: bool,
    /// Respawn a crashed relay with capped backoff. Never applies to a
    /// relay the user stopped deliberately.
    pub auto_restart_relay: bool,
    pub host: String,
    pub port: u16,
    pub require_bearer_auth: bool,
    pub auto_generate_bearer_token: bool,
    pub auth_storage_mode: String,
    pub browser_open: bool,
    pub login_timeout_seconds: u64,
    pub upstream_base_url: String,
    pub issuer_base_url: String,
    pub client_id: String,
    pub client_version: String,
    pub request_timeout_seconds: f64,
    pub rate_limit_per_minute: u32,
    pub rate_limit_burst: u32,
    pub concurrent_requests_per_ip: u32,
    pub failed_auth_window_seconds: u32,
    pub failed_auth_max_attempts: u32,
    pub failed_auth_block_seconds: u32,
    pub trust_x_forwarded_for: bool,
    pub max_upload_bytes: u64,
    pub max_total_upload_bytes: u64,
    pub enable_openai_provider: bool,
    pub models_cache_ttl_seconds: f64,
    /// Multi-account routing: "round_robin" balances charge across healthy
    /// accounts (relay default); "ordered" drains the first account first.
    pub openai_balance: String,
    // The serde alias keeps settings files written while the Claude runtime
    // carried the "experimental" label loading unchanged.
    #[serde(alias = "enableClaudeExperimental")]
    pub enable_claude: bool,
    pub claude_bin: String,
    pub claude_timeout_seconds: f64,
    pub claude_max_concurrent_requests: u32,
    pub claude_strip_api_key_env: bool,
    pub claude_models_csv: String,
    pub extra_serve_args: String,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            relay_command_override: String::new(),
            login_method: "browser".into(),
            start_relay_on_launch: true,
            auto_restart_relay: true,
            // All interfaces by default so private-network devices can
            // connect out of the box; loopback-only is one click away.
            host: "0.0.0.0".into(),
            port: DEFAULT_RELAY_PORT,
            require_bearer_auth: true,
            // Without auto-generation, a fresh install in protected mode has
            // no token and the relay refuses to start — first run must work.
            auto_generate_bearer_token: true,
            auth_storage_mode: "auto".into(),
            // Login is unusable for most users without the browser opening.
            browser_open: true,
            login_timeout_seconds: 900,
            upstream_base_url: "https://chatgpt.com/backend-api/codex".into(),
            issuer_base_url: "https://auth.openai.com".into(),
            client_id: "app_EMoamEEZ73f0CkXaXp7hrann".into(),
            client_version: "0.124.0".into(),
            request_timeout_seconds: 120.0,
            rate_limit_per_minute: 120,
            rate_limit_burst: 40,
            concurrent_requests_per_ip: 50,
            failed_auth_window_seconds: 300,
            failed_auth_max_attempts: 8,
            failed_auth_block_seconds: 900,
            trust_x_forwarded_for: false,
            max_upload_bytes: 32 * 1024 * 1024,
            max_total_upload_bytes: 256 * 1024 * 1024,
            enable_openai_provider: true,
            models_cache_ttl_seconds: 300.0,
            openai_balance: "round_robin".into(),
            enable_claude: true,
            claude_bin: "claude".into(),
            claude_timeout_seconds: 600.0,
            claude_max_concurrent_requests: 2,
            claude_strip_api_key_env: true,
            claude_models_csv: "claude:sonnet, claude:opus, claude:haiku, claude:fable".into(),
            extra_serve_args: String::new(),
        }
    }
}

impl AppSettings {
    pub fn is_loopback_host(&self) -> bool {
        matches!(self.host.as_str(), "127.0.0.1" | "localhost" | "::1")
    }

    /// Host the app itself dials; wildcard binds are not dialable.
    pub fn client_host(&self) -> &str {
        match self.host.as_str() {
            "0.0.0.0" | "::" => "127.0.0.1",
            other => other,
        }
    }

    pub fn base_url(&self) -> String {
        format!("http://{}:{}/v1", self.client_host(), self.port)
    }

    /// The relay enforces guardrails for the Claude runtime (loopback-only
    /// listener, no X-Forwarded-For trust); the rendered config must
    /// respect them or serve refuses to start.
    pub fn claude_effectively_enabled(&self) -> bool {
        self.enable_claude && self.is_loopback_host() && !self.trust_x_forwarded_for
    }

    /// Rejects values that would render an invalid or dangerous config.
    /// Returns a user-readable message for the first problem found.
    pub fn validate(&self) -> Result<(), String> {
        if self.port == 0 {
            return Err("Port must be between 1 and 65535.".into());
        }
        let host_ok = self.host.parse::<std::net::IpAddr>().is_ok()
            || self.host == "localhost";
        if !host_ok {
            return Err(format!("Host '{}' is not a valid IP address.", self.host));
        }
        if !matches!(self.auth_storage_mode.as_str(), "auto" | "file" | "keyring") {
            return Err("Auth storage must be auto, file, or keyring.".into());
        }
        if !matches!(self.openai_balance.as_str(), "round_robin" | "ordered") {
            return Err("OpenAI account balancing must be round_robin or ordered.".into());
        }
        for (label, value) in [
            ("Upstream base URL", &self.upstream_base_url),
            ("Issuer base URL", &self.issuer_base_url),
        ] {
            if !value.starts_with("http://") && !value.starts_with("https://") {
                return Err(format!("{label} must start with http:// or https://."));
            }
        }
        Ok(())
    }

    pub fn claude_models(&self) -> Vec<String> {
        self.claude_models_csv
            .split(',')
            .map(|m| m.trim().to_string())
            .filter(|m| !m.is_empty())
            .collect()
    }

    pub fn home_dir() -> PathBuf {
        dirs_home().unwrap_or_else(|| PathBuf::from("."))
    }

    pub fn relay_config_path() -> PathBuf {
        Self::home_dir().join(".config").join("airelays").join("config.toml")
    }

    pub fn data_dir() -> PathBuf {
        Self::home_dir().join(".airelays")
    }

    pub fn logs_dir() -> PathBuf {
        Self::data_dir().join("logs")
    }

    pub fn bearer_token_file() -> PathBuf {
        Self::data_dir().join("relay-token")
    }

    /// Renders the relay `config.toml`, mirroring the CLI's config surface.
    /// Serialized through the `toml` crate so arbitrary string values
    /// (quotes, backslashes, Windows paths) are always escaped correctly.
    pub fn render_config_toml(&self) -> Result<String, String> {
        let config = RelayConfigFile {
            server: ServerSection { host: &self.host, port: self.port },
            paths: PathsSection {
                data_dir: path_str(Self::data_dir()),
                logs_dir: path_str(Self::logs_dir()),
            },
            auth: AuthSection {
                storage: &self.auth_storage_mode,
                browser_open: self.browser_open,
                login_timeout_seconds: self.login_timeout_seconds,
            },
            upstream: UpstreamSection {
                base_url: &self.upstream_base_url,
                issuer_base_url: &self.issuer_base_url,
                client_id: &self.client_id,
                client_version: &self.client_version,
                request_timeout_seconds: self.request_timeout_seconds,
            },
            security: SecuritySection {
                require_bearer_auth: self.require_bearer_auth,
                bearer_token_file: path_str(Self::bearer_token_file()),
                auto_generate_bearer_token: self.auto_generate_bearer_token,
                rate_limit_per_minute: self.rate_limit_per_minute,
                rate_limit_burst: self.rate_limit_burst,
                concurrent_requests_per_ip: self.concurrent_requests_per_ip,
                failed_auth_window_seconds: self.failed_auth_window_seconds,
                failed_auth_max_attempts: self.failed_auth_max_attempts,
                failed_auth_block_seconds: self.failed_auth_block_seconds,
                trust_x_forwarded_for: self.trust_x_forwarded_for,
            },
            uploads: UploadsSection {
                max_upload_bytes: self.max_upload_bytes,
                max_total_upload_bytes: self.max_total_upload_bytes,
            },
            providers: ProvidersSection {
                openai: OpenAiSection {
                    enabled: self.enable_openai_provider,
                    models_cache_ttl_seconds: self.models_cache_ttl_seconds,
                    balance: &self.openai_balance,
                },
                claude: ClaudeSection {
                    enabled: self.claude_effectively_enabled(),
                    bin: &self.claude_bin,
                    timeout_seconds: self.claude_timeout_seconds,
                    max_concurrent_requests: self.claude_max_concurrent_requests,
                    strip_api_key_env: self.claude_strip_api_key_env,
                    models: self.claude_models(),
                },
            },
        };
        toml::to_string_pretty(&config).map_err(|error| format!("Cannot render config: {error}"))
    }
}

// Typed mirror of the relay's config.toml schema (see the Python side in
// src/airelay/config.py). Field names must match the relay's parser.

#[derive(Serialize)]
struct RelayConfigFile<'a> {
    server: ServerSection<'a>,
    paths: PathsSection,
    auth: AuthSection<'a>,
    upstream: UpstreamSection<'a>,
    security: SecuritySection,
    uploads: UploadsSection,
    providers: ProvidersSection<'a>,
}

#[derive(Serialize)]
struct ServerSection<'a> {
    host: &'a str,
    port: u16,
}

#[derive(Serialize)]
struct PathsSection {
    data_dir: String,
    logs_dir: String,
}

#[derive(Serialize)]
struct AuthSection<'a> {
    storage: &'a str,
    browser_open: bool,
    login_timeout_seconds: u64,
}

#[derive(Serialize)]
struct UpstreamSection<'a> {
    base_url: &'a str,
    issuer_base_url: &'a str,
    client_id: &'a str,
    client_version: &'a str,
    request_timeout_seconds: f64,
}

#[derive(Serialize)]
struct SecuritySection {
    require_bearer_auth: bool,
    bearer_token_file: String,
    auto_generate_bearer_token: bool,
    rate_limit_per_minute: u32,
    rate_limit_burst: u32,
    concurrent_requests_per_ip: u32,
    failed_auth_window_seconds: u32,
    failed_auth_max_attempts: u32,
    failed_auth_block_seconds: u32,
    trust_x_forwarded_for: bool,
}

#[derive(Serialize)]
struct UploadsSection {
    max_upload_bytes: u64,
    max_total_upload_bytes: u64,
}

#[derive(Serialize)]
struct ProvidersSection<'a> {
    openai: OpenAiSection<'a>,
    claude: ClaudeSection<'a>,
}

#[derive(Serialize)]
struct OpenAiSection<'a> {
    enabled: bool,
    models_cache_ttl_seconds: f64,
    balance: &'a str,
}

#[derive(Serialize)]
struct ClaudeSection<'a> {
    enabled: bool,
    bin: &'a str,
    timeout_seconds: f64,
    max_concurrent_requests: u32,
    strip_api_key_env: bool,
    models: Vec<String>,
}

/// TOML paths use forward slashes even on Windows; Python's pathlib
/// accepts them and it keeps the file readable.
fn path_str(path: PathBuf) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}
