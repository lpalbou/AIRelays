//! App settings: persisted JSON mirroring the relay's config surface.
//!
//! The desktop app owns a settings file in the platform config directory and
//! renders the relay's `config.toml` from it before every launch, exactly
//! like the macOS menu bar app does.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::{Path, PathBuf};

/// Default relay port: IANA-unregistered and not a common tool default,
/// chosen to avoid the dev-server collisions that 8080 invites.
pub const DEFAULT_RELAY_PORT: u16 = 8317;

/// The only credential store (ADR 0008). Rendered explicitly so a config
/// file shared with a CLI or service relay never keeps an older
/// `auto`/`keyring` value.
const AUTH_STORAGE: &str = "file";

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
    // OpenAI sign-ins are always stored as private files (ADR 0008), so
    // there is no storage setting. Settings files written by older versions
    // still carry `authStorageMode` ("auto"/"file"/"keyring"); serde ignores
    // the unknown key and the rendered config always says `storage = "file"`.
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
    /// Multi-account routing: "balanced" equalizes quota consumption
    /// (relay default); "weighted" splits requests in proportion to remaining
    /// capacity; "round_robin" sends equal request counts; "ping_pong" takes
    /// turns in order; "ordered" drains the first account first.
    pub openai_balance: String,
    /// Extra OpenAI model ids to advertise beyond the upstream catalog
    /// (which lags what the backend actually serves), comma-separated.
    pub openai_extra_models_csv: String,
    /// Automatic retries for failed upstream LLM calls; 0 disables.
    pub openai_retry_attempts: u32,
    /// Wait before each retry, comma-separated seconds. A schedule shorter
    /// than the attempt count repeats its last delay.
    pub openai_retry_backoff_csv: String,
    // The serde alias keeps settings files written while the Claude runtime
    // carried the "experimental" label loading unchanged.
    #[serde(alias = "enableClaudeExperimental")]
    pub enable_claude: bool,
    pub claude_bin: String,
    pub claude_timeout_seconds: f64,
    pub claude_max_concurrent_requests: u32,
    pub claude_balance: String,
    pub claude_account_cooldown_seconds: u32,
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
            // Login is unusable for most users without the browser opening.
            browser_open: true,
            login_timeout_seconds: 900,
            upstream_base_url: "https://chatgpt.com/backend-api/codex".into(),
            issuer_base_url: "https://auth.openai.com".into(),
            client_id: "app_EMoamEEZ73f0CkXaXp7hrann".into(),
            client_version: "auto".into(),
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
            openai_balance: "balanced".into(),
            openai_extra_models_csv: String::new(),
            openai_retry_attempts: 3,
            openai_retry_backoff_csv: "5, 20, 60".into(),
            enable_claude: true,
            claude_bin: "claude".into(),
            claude_timeout_seconds: 600.0,
            claude_max_concurrent_requests: 2,
            claude_balance: "balanced".into(),
            claude_account_cooldown_seconds: 300,
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

    /// The relay refuses the Claude runtime together with X-Forwarded-For
    /// trust; the rendered config must respect that or serve refuses to
    /// start. Claude runs on whichever listener the user chose.
    pub fn claude_effectively_enabled(&self) -> bool {
        self.enable_claude && !self.trust_x_forwarded_for
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
        if !matches!(self.openai_balance.as_str(), "balanced" | "weighted" | "round_robin" | "ping_pong" | "ordered") {
            return Err("OpenAI account balancing must be balanced, weighted, round_robin, ping_pong, or ordered.".into());
        }
        if !matches!(self.claude_balance.as_str(), "balanced" | "weighted" | "round_robin" | "ping_pong" | "ordered") {
            return Err("Claude account balancing must be balanced, weighted, round_robin, ping_pong, or ordered.".into());
        }
        if self.claude_max_concurrent_requests == 0 || self.claude_account_cooldown_seconds == 0 {
            return Err("Claude concurrency and account cooldown must be at least 1.".into());
        }
        if self.openai_retry_attempts > 10 {
            return Err("OpenAI retry attempts must be between 0 and 10.".into());
        }
        if self.openai_retry_backoff_seconds().is_none() {
            return Err(
                "OpenAI retry backoff must be comma-separated non-negative seconds (e.g. 5, 20, 60)."
                    .into(),
            );
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
        Self::csv_models(&self.claude_models_csv)
    }

    pub fn openai_extra_models(&self) -> Vec<String> {
        Self::csv_models(&self.openai_extra_models_csv)
    }

    /// Parsed backoff schedule; None when any entry is not a non-negative
    /// number. An empty field means "use the relay default schedule".
    pub fn openai_retry_backoff_seconds(&self) -> Option<Vec<f64>> {
        let mut delays = Vec::new();
        for segment in self.openai_retry_backoff_csv.split(',') {
            let trimmed = segment.trim();
            if trimmed.is_empty() {
                continue;
            }
            match trimmed.parse::<f64>() {
                Ok(value) if value.is_finite() && value >= 0.0 => delays.push(value),
                _ => return None,
            }
        }
        Some(delays)
    }

    fn csv_models(csv: &str) -> Vec<String> {
        csv.split(',')
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
                storage: AUTH_STORAGE,
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
                    retry_attempts: self.openai_retry_attempts,
                    // validate() gates every render path (save and launch),
                    // so this branch is defense-in-depth against a future
                    // caller skipping validation — never a silent repair
                    // users would observe.
                    retry_backoff_seconds: self
                        .openai_retry_backoff_seconds()
                        .unwrap_or_else(|| vec![5.0, 20.0, 60.0]),
                    extra_models: self.openai_extra_models(),
                },
                claude: ClaudeSection {
                    enabled: self.claude_effectively_enabled(),
                    bin: &self.claude_bin,
                    timeout_seconds: self.claude_timeout_seconds,
                    max_concurrent_requests: self.claude_max_concurrent_requests,
                    balance: &self.claude_balance,
                    account_cooldown_seconds: self.claude_account_cooldown_seconds,
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
    retry_attempts: u32,
    retry_backoff_seconds: Vec<f64>,
    extra_models: Vec<String>,
}

#[derive(Serialize)]
struct ClaudeSection<'a> {
    enabled: bool,
    bin: &'a str,
    timeout_seconds: f64,
    max_concurrent_requests: u32,
    balance: &'a str,
    account_cooldown_seconds: u32,
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

// MARK: owner-only files (ADR 0008), mirroring src/airelay/private_io.py

/// Creates `path` owner-only (0700) on Unix. Missing parents get default
/// permissions (they are shared locations such as `~/.config`), like
/// `mkdir -p`. An existing directory is tightened when this user owns it; a
/// mounted volume owned by someone else is left alone. Windows relies on the
/// user profile's ACL.
pub fn ensure_private_dir(path: &Path) -> std::io::Result<()> {
    if let Some(parent) = path.parent().filter(|parent| !parent.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
        match std::fs::DirBuilder::new().mode(0o700).create(path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists && path.is_dir() => {
                if let Ok(metadata) = std::fs::metadata(path) {
                    // SAFETY: getuid has no preconditions and cannot fail.
                    let uid = unsafe { libc::getuid() };
                    if metadata.uid() == uid && metadata.mode() & 0o077 != 0 {
                        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
                    }
                }
            }
            Err(error) => return Err(error),
        }
    }
    #[cfg(not(unix))]
    std::fs::create_dir_all(path)?;
    Ok(())
}

/// Atomically replaces `path` with `data`, readable only by the owner. The
/// temporary file is created 0600 (Unix) in the target directory, so the
/// secret is never visible with default permissions, and a crash never
/// leaves a truncated file behind.
pub fn write_private(path: &Path, data: &[u8]) -> std::io::Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    ensure_private_dir(parent)?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut attempt = 0u32;
    let (temporary, mut file) = loop {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.subsec_nanos())
            .unwrap_or_default();
        let temporary = parent.join(format!(".{name}.{}.{nanos}.tmp", std::process::id()));
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&temporary) {
            Ok(file) => break (temporary, file),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists && attempt < 8 => {
                attempt += 1;
            }
            Err(error) => return Err(error),
        }
    };
    let written = file.write_all(data).and_then(|()| file.sync_all());
    drop(file);
    if let Err(error) = written.and_then(|()| replace_file(&temporary, path)) {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
    }
    #[cfg(unix)]
    if let Ok(directory) = std::fs::File::open(parent) {
        let _ = directory.sync_all();
    }
    Ok(())
}

fn replace_file(source: &Path, target: &Path) -> std::io::Result<()> {
    // Windows refuses to replace a file another process (antivirus, an
    // indexer, a reader) briefly holds open: ERROR_ACCESS_DENIED (5) or
    // ERROR_SHARING_VIOLATION (32). Those holds are short.
    let mut attempt = 0u64;
    loop {
        match std::fs::rename(source, target) {
            Ok(()) => return Ok(()),
            Err(error)
                if cfg!(windows)
                    && matches!(error.raw_os_error(), Some(5) | Some(32))
                    && attempt < 9 =>
            {
                attempt += 1;
                std::thread::sleep(std::time::Duration::from_millis(50 * attempt));
            }
            Err(error) => return Err(error),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_dir(label: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "airelays-settings-test-{label}-{}-{nanos}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn legacy_storage_modes_load_and_render_file_storage() {
        for legacy in ["auto", "keyring", "file"] {
            let json = format!(r#"{{"authStorageMode": "{legacy}", "port": 9000}}"#);
            let settings: AppSettings = serde_json::from_str(&json).unwrap();
            assert_eq!(settings.port, 9000);
            settings.validate().unwrap();
            let rendered: toml::Value = toml::from_str(&settings.render_config_toml().unwrap()).unwrap();
            assert_eq!(rendered["auth"]["storage"].as_str(), Some("file"));
            let saved = serde_json::to_value(&settings).unwrap();
            assert!(saved.get("authStorageMode").is_none());
        }
    }

    #[test]
    fn write_private_replaces_contents_atomically() {
        let dir = scratch_dir("write");
        let target = dir.join("nested").join("relay-token");
        write_private(&target, b"first\n").unwrap();
        write_private(&target, b"second\n").unwrap();
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "second\n");
        let leftovers: Vec<_> = std::fs::read_dir(target.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn private_files_and_directories_are_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = scratch_dir("modes");
        let data_dir = dir.join("data");
        std::fs::create_dir(&data_dir).unwrap();
        std::fs::set_permissions(&data_dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        let token = data_dir.join("claude-token");
        write_private(&token, b"secret\n").unwrap();
        let mode = |path: &Path| std::fs::metadata(path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(&data_dir), 0o700, "an existing directory we own is tightened");
        assert_eq!(mode(&token), 0o600);
        let fresh = dir.join("fresh").join("config");
        ensure_private_dir(&fresh).unwrap();
        assert_eq!(mode(&fresh), 0o700);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
