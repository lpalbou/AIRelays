//! The machine's public (internet-facing) address, shown as the "Internet"
//! row of the dashboard's Connect card and in the tray's Copy Address menu.
//!
//! Privacy: discovering it means asking a public echo service, an outbound
//! call the user has not opted into unless they chose "Anyone on the
//! internet". Every entry point here therefore checks
//! `effective_network_scope() == "any"` first and never contacts a service
//! otherwise; leaving that scope clears the cached address.
//!
//! Callers never block: `endpoint` reads the cache and, when it is missing
//! or stale, starts one background lookup whose result lands in
//! `AppState::public_ip` (the status loop then notices the changed tray
//! address list and rebuilds the menu).

use crate::relay::robust_lock;
use crate::settings::AppSettings;
use crate::state::AppState;
use std::net::IpAddr;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

/// Plain-text echo services, tried in order. Both answer over HTTPS with
/// the caller's IPv4 address and nothing else.
const ECHO_SERVICES: [&str; 2] = ["https://api.ipify.org", "https://checkip.amazonaws.com"];
/// Per-request timeout: a slow service must not hold the lookup long.
const LOOKUP_TIMEOUT: Duration = Duration::from_secs(5);
/// A found address is re-checked this often (home IPs change rarely).
const FOUND_TTL: Duration = Duration::from_secs(10 * 60);
/// A failed lookup (offline, services blocked) is retried sooner.
const FAILED_TTL: Duration = Duration::from_secs(60);

/// What the UI shows for the internet row: the endpoint once known, and
/// whether the first lookup is still running. `(None, false)` outside the
/// "any" scope or after a failed lookup.
pub(crate) fn endpoint(app: &AppHandle, settings: &AppSettings) -> (Option<String>, bool) {
    let state = app.state::<AppState>();
    if settings.effective_network_scope() != "any" {
        // Covers every path that leaves "any" (scope buttons, Settings
        // save, host change): a stale address is never shown later.
        *robust_lock(&state.public_ip) = None;
        return (None, false);
    }
    let cached = robust_lock(&state.public_ip).clone();
    let fresh = match &cached {
        Some((at, Some(_))) => at.elapsed() < FOUND_TTL,
        Some((at, None)) => at.elapsed() < FAILED_TTL,
        None => false,
    };
    if !fresh {
        start_lookup(app);
    }
    match cached {
        Some((_, ip)) => (ip.map(|ip| format_endpoint(&ip, settings.port)), false),
        None => (None, true),
    }
}

/// Drops any cached address and, when the scope is "any", looks it up
/// again right away. Called when the user changes who can connect.
pub(crate) fn reset(app: &AppHandle) {
    let state = app.state::<AppState>();
    *robust_lock(&state.public_ip) = None;
    let scope_is_any = robust_lock(&state.settings).effective_network_scope() == "any";
    if scope_is_any {
        start_lookup(app);
    }
}

/// Starts one background lookup unless one is already in flight.
fn start_lookup(app: &AppHandle) {
    let state = app.state::<AppState>();
    if state.public_ip_lookup_running.swap(true, Ordering::SeqCst) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let found = lookup().await;
        let state = app.state::<AppState>();
        // The scope may have left "any" while the request was in flight.
        let scope_is_any = robust_lock(&state.settings).effective_network_scope() == "any";
        if scope_is_any {
            *robust_lock(&state.public_ip) = Some((Instant::now(), found));
        }
        state.public_ip_lookup_running.store(false, Ordering::SeqCst);
    });
}

/// Asks each echo service in turn; the first IPv4 answer wins. An IPv6
/// answer is kept only as a fallback when no service returned IPv4.
async fn lookup() -> Option<String> {
    let client = reqwest::Client::builder()
        .timeout(LOOKUP_TIMEOUT)
        .user_agent(concat!("airelays-desktop/", env!("CARGO_PKG_VERSION")))
        .build()
        .ok()?;
    let mut ipv6: Option<IpAddr> = None;
    for service in ECHO_SERVICES {
        let Ok(response) = client.get(service).send().await else { continue };
        if !response.status().is_success() {
            continue;
        }
        let Ok(body) = response.text().await else { continue };
        match parse_ip(&body) {
            Some(ip @ IpAddr::V4(_)) => return Some(ip.to_string()),
            Some(ip) => ipv6 = ipv6.or(Some(ip)),
            None => {}
        }
    }
    ipv6.map(|ip| ip.to_string())
}

/// A service answer is accepted only when it is exactly one IP address.
fn parse_ip(body: &str) -> Option<IpAddr> {
    body.trim().parse::<IpAddr>().ok()
}

fn format_endpoint(ip: &str, port: u16) -> String {
    if ip.contains(':') {
        format!("http://[{ip}]:{port}/v1")
    } else {
        format!("http://{ip}:{port}/v1")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_answers_are_trimmed_and_validated() {
        assert_eq!(parse_ip("203.0.113.10\n"), "203.0.113.10".parse().ok());
        assert_eq!(parse_ip("  2001:db8::1 "), "2001:db8::1".parse().ok());
        assert_eq!(parse_ip("<html>blocked</html>"), None);
        assert_eq!(parse_ip(""), None);
        assert_eq!(parse_ip("203.0.113.10 198.51.100.1"), None);
    }

    #[test]
    fn endpoints_bracket_ipv6_literals() {
        assert_eq!(format_endpoint("203.0.113.10", 8317), "http://203.0.113.10:8317/v1");
        assert_eq!(format_endpoint("2001:db8::1", 8317), "http://[2001:db8::1]:8317/v1");
    }
}
