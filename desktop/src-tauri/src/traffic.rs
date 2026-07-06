//! Request-oriented view over the relay's hourly JSONL traffic logs.

use crate::settings::AppSettings;
use serde::Serialize;
use serde_json::Value;
use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Serialize)]
pub struct RequestSummary {
    pub id: String,
    pub last_seen: String,
    pub method: String,
    pub path: String,
    pub provider: String,
    pub model: String,
    /// Which enrolled account served the request (multi-account installs).
    pub account: String,
    pub status_code: Option<i64>,
    pub last_phase: String,
    pub event_count: usize,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub details: String,
}

/// Parses the most recent log files and groups records by request id,
/// newest first.
pub fn recent_requests() -> Vec<RequestSummary> {
    recent_requests_in(&AppSettings::logs_dir())
}

pub fn recent_requests_in(logs_dir: &PathBuf) -> Vec<RequestSummary> {
    let mut files = Vec::new();
    collect_logs(logs_dir, &mut files, 0);
    files.sort_by_key(|(modified, _)| std::cmp::Reverse(*modified));

    let mut grouped: HashMap<String, Vec<Value>> = HashMap::new();
    for (_, path) in files.into_iter().take(3) {
        let Some(content) = read_tail(&path, 2 * 1024 * 1024) else {
            continue;
        };
        // Keep a budget of real (non-monitoring) records. Older log files
        // written before monitoring endpoints were excluded contain tens of
        // thousands of status-poll lines; skipping them here means those old
        // floods no longer evict real requests from the window.
        let mut kept = 0usize;
        for line in content.lines().rev() {
            if kept >= 600 {
                break;
            }
            let Ok(record) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let request_id = record
                .get("request_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if request_id.is_empty() || request_id == "startup" {
                continue;
            }
            if is_monitoring_record(&record) {
                continue;
            }
            kept += 1;
            grouped.entry(request_id.to_string()).or_default().push(record);
        }
    }

    let mut summaries: Vec<RequestSummary> = grouped
        .into_iter()
        .map(|(id, mut records)| {
            records.sort_by(|a, b| timestamp_of(a).cmp(&timestamp_of(b)));
            summarize(id, records)
        })
        // Drop monitoring rows and orphaned groups whose inbound request was
        // evicted (no real route), which would otherwise show as junk "/".
        .filter(|summary| !summary.path.ends_with("/relay/status"))
        .filter(|summary| !summary.path.ends_with("/subscription/status"))
        .filter(|summary| summary.path != "/")
        .collect();
    summaries.sort_by(|a, b| b.last_seen.cmp(&a.last_seen));
    summaries.truncate(200);
    summaries
}

/// Reads only the trailing `max_bytes` of a log file: hourly JSONL files
/// can grow to many megabytes and only the recent records matter here.
fn read_tail(path: &PathBuf, max_bytes: u64) -> Option<String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = std::fs::File::open(path).ok()?;
    let len = file.metadata().ok()?.len();
    let start = len.saturating_sub(max_bytes);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).ok()?;
    // Lossy conversion: the seek may have landed mid-character.
    let mut buffer = String::from_utf8_lossy(&bytes).into_owned();
    if start > 0 {
        // Drop the first, likely partial, line.
        if let Some(newline) = buffer.find('\n') {
            buffer.drain(..=newline);
        }
    }
    Some(buffer)
}

/// The relay writes hourly logs nested as logs/YYYY/MM/DD-HH.log, so the
/// directory must be walked recursively — a flat read only sees the year
/// directory and finds no .log files (the original "No requests yet" bug).
fn collect_logs(dir: &PathBuf, out: &mut Vec<(std::time::SystemTime, PathBuf)>, depth: usize) {
    if depth > 4 {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_logs(&path, out, depth + 1);
        } else if path.extension().map(|e| e == "log").unwrap_or(false) {
            if let Ok(modified) = entry.metadata().and_then(|m| m.modified()) {
                out.push((modified, path));
            }
        }
    }
}

/// True for records belonging to a monitoring endpoint (status/health
/// polls the desktop makes continuously), by path when present.
fn is_monitoring_record(record: &Value) -> bool {
    match record.get("path").and_then(Value::as_str) {
        Some(path) => {
            path.ends_with("/relay/status")
                || path.ends_with("/subscription/status")
                || path.ends_with("/account/rate_limits")
                || path.ends_with("/healthz")
        }
        None => false,
    }
}

fn timestamp_of(record: &Value) -> String {
    record
        .get("logged_at")
        .or_else(|| record.get("timestamp"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn string_field(records: &[Value], key: &str, fallback: &str) -> String {
    records
        .iter()
        .rev()
        .find_map(|record| record.get(key).and_then(Value::as_str))
        .unwrap_or(fallback)
        .to_string()
}

/// Extracts a token count from a usage object, tolerating both the
/// Responses shape (input_tokens/output_tokens) and the Chat shape
/// (prompt_tokens/completion_tokens).
fn usage_tokens(records: &[Value], keys: &[&str]) -> Option<i64> {
    records.iter().rev().find_map(|record| {
        let usage = record.get("usage")?;
        keys.iter().find_map(|key| usage.get(*key).and_then(Value::as_i64))
    })
}

fn summarize(id: String, records: Vec<Value>) -> RequestSummary {
    let status_code = records
        .iter()
        .rev()
        .find_map(|record| record.get("status_code").and_then(Value::as_i64));
    let input_tokens = usage_tokens(&records, &["input_tokens", "prompt_tokens"]);
    let output_tokens = usage_tokens(&records, &["output_tokens", "completion_tokens"]);
    let details = records
        .iter()
        .map(|record| serde_json::to_string_pretty(record).unwrap_or_default())
        .collect::<Vec<_>>()
        .join("\n\n");
    RequestSummary {
        last_seen: records.last().map(|r| timestamp_of(r)).unwrap_or_default(),
        method: string_field(&records, "method", "?"),
        path: string_field(&records, "path", "/"),
        provider: string_field(&records, "provider", "-"),
        model: string_field(&records, "model", "-"),
        account: string_field(&records, "account", ""),
        last_phase: string_field(&records, "phase", "unknown"),
        event_count: records.len(),
        input_tokens,
        output_tokens,
        status_code,
        details,
        id,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_log(dir: &PathBuf, lines: &[String]) {
        // Mirror the relay's nested layout: logs/2026/07/06-15.log
        let nested = dir.join("2026").join("07");
        std::fs::create_dir_all(&nested).unwrap();
        let mut file = std::fs::File::create(nested.join("06-15.log")).unwrap();
        for line in lines {
            writeln!(file, "{line}").unwrap();
        }
    }

    fn status_poll(i: usize) -> String {
        format!(
            r#"{{"request_id":"poll-{i}","phase":"inbound_request","method":"GET","path":"/v1/relay/status","logged_at":"2026-07-06T15:00:{:02}Z"}}"#,
            i % 60
        )
    }

    #[test]
    fn chat_request_survives_status_flood_and_reports_tokens() {
        let dir = std::env::temp_dir().join(format!("airelays-traffic-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        // A real chat request, then thousands of status polls after it — the
        // exact shape that produced "No requests yet".
        let mut lines = vec![
            r#"{"request_id":"req-CHAT","phase":"inbound_request","method":"POST","path":"/v1/chat/completions","model":"gpt-5.4-mini","logged_at":"2026-07-06T15:10:00Z"}"#.to_string(),
            r#"{"request_id":"req-CHAT","phase":"account_selected","account":"work@company.com","logged_at":"2026-07-06T15:10:01Z"}"#.to_string(),
            r#"{"request_id":"req-CHAT","phase":"outbound_response","status_code":200,"usage":{"input_tokens":13,"output_tokens":26},"logged_at":"2026-07-06T15:10:02Z"}"#.to_string(),
        ];
        for i in 0..3000 {
            lines.push(status_poll(i));
        }
        write_log(&dir, &lines);

        let summaries = recent_requests_in(&dir);
        std::fs::remove_dir_all(&dir).ok();

        let chat = summaries
            .iter()
            .find(|s| s.path == "/v1/chat/completions")
            .expect("chat request must be surfaced despite the status flood");
        assert_eq!(chat.status_code, Some(200));
        assert_eq!(chat.input_tokens, Some(13));
        assert_eq!(chat.output_tokens, Some(26));
        assert_eq!(chat.account, "work@company.com");
        // No monitoring rows leak through.
        assert!(summaries.iter().all(|s| !s.path.ends_with("/relay/status")));
    }

    #[test]
    fn empty_logs_dir_yields_no_rows() {
        let dir = std::env::temp_dir().join(format!("airelays-empty-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        assert!(recent_requests_in(&dir).is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }
}
