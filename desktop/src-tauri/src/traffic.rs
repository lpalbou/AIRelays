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
        // The tail must be generous: logs written with per-line stream
        // logging enabled are dominated by hundreds of `upstream_stream_line`
        // records per request, so a small tail covers only the last minute
        // of each hourly file and the view looks frozen at HH:59.
        let Some(content) = read_tail(&path, 16 * 1024 * 1024) else {
            continue;
        };
        // Keep a budget of real records. Monitoring polls and per-line
        // stream records are skipped *before* the budget, so floods of
        // either can no longer evict real requests from the window.
        let mut kept = 0usize;
        let mut scanned = 0usize;
        for line in content.lines().rev() {
            // Two independent budgets: `kept` bounds the useful records,
            // `scanned` bounds CPU when the tail is dominated by skipped
            // chatter (per-line stream logs) that never fills `kept`.
            scanned += 1;
            if kept >= 2500 || scanned >= 80_000 {
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
            if is_monitoring_record(&record) || is_stream_chatter(&record) {
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

/// True for per-line/per-chunk stream records: hundreds per streamed
/// response, useful only for deep protocol debugging in the raw files.
/// The summary records carry everything the table and detail pane show.
fn is_stream_chatter(record: &Value) -> bool {
    matches!(
        record.get("phase").and_then(Value::as_str),
        Some("upstream_stream_line")
            | Some("outbound_stream_chunk")
            | Some("provider_stream_line")
            | Some("upstream_stream_chunk")
    )
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

/// Truncates every long string in a record for the detail pane: request
/// bodies can be tens of kilobytes each, and details ship to the webview
/// for all 200 rows on every poll. The raw log files stay complete.
fn clip_long_strings(value: &mut Value, max_chars: usize) {
    match value {
        Value::String(text) => {
            if text.chars().count() > max_chars {
                let kept: String = text.chars().take(max_chars).collect();
                let dropped = text.chars().count() - max_chars;
                *text = format!("{kept}… (+{dropped} chars truncated; full record in the log file)");
            }
        }
        Value::Array(items) => {
            for item in items {
                clip_long_strings(item, max_chars);
            }
        }
        Value::Object(map) => {
            for (_, item) in map.iter_mut() {
                clip_long_strings(item, max_chars);
            }
        }
        _ => {}
    }
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
        .map(|record| {
            let mut clipped = record.clone();
            clip_long_strings(&mut clipped, 4000);
            serde_json::to_string_pretty(&clipped).unwrap_or_default()
        })
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
    fn requests_survive_stream_line_flood() {
        let dir = std::env::temp_dir().join(format!("airelays-stream-flood-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);

        // Ten real requests, each followed by a heavy stream-line flood —
        // the shape of a busy relay with per-line logging enabled, which
        // made the Traffic view show only requests from the last minute of
        // each hourly file.
        let mut lines = Vec::new();
        for request in 0..10 {
            lines.push(format!(
                r#"{{"request_id":"req-{request}","phase":"inbound_request","method":"POST","path":"/v1/chat/completions","model":"gpt-5.5","logged_at":"2026-07-06T15:{:02}:00Z"}}"#,
                request
            ));
            for line_index in 0..500 {
                lines.push(format!(
                    r#"{{"request_id":"req-{request}","phase":"upstream_stream_line","line":"data: {{\"chunk\":{line_index},\"padding\":\"{}\"}}","logged_at":"2026-07-06T15:{:02}:01Z"}}"#,
                    "x".repeat(200),
                    request
                ));
            }
            lines.push(format!(
                r#"{{"request_id":"req-{request}","phase":"outbound_response","status_code":200,"usage":{{"input_tokens":10,"output_tokens":20}},"logged_at":"2026-07-06T15:{:02}:02Z"}}"#,
                request
            ));
        }
        write_log(&dir, &lines);

        let summaries = recent_requests_in(&dir);
        std::fs::remove_dir_all(&dir).ok();

        // Every request must be visible with its full summary, not only the
        // ones whose inbound record happened to land near the end of file.
        assert_eq!(summaries.len(), 10, "all requests must survive the flood");
        assert!(summaries.iter().all(|s| s.path == "/v1/chat/completions"));
        assert!(summaries.iter().all(|s| s.status_code == Some(200)));
        // Stream chatter is excluded from the grouped records entirely.
        assert!(summaries.iter().all(|s| !s.details.contains("upstream_stream_line")));
    }

    #[test]
    fn detail_pane_clips_huge_bodies() {
        let mut record = serde_json::json!({
            "request_id": "req-big",
            "phase": "upstream_request",
            "body": {"text": "y".repeat(50_000)},
        });
        clip_long_strings(&mut record, 4000);
        let text = record["body"]["text"].as_str().unwrap();
        assert!(text.len() < 5000);
        assert!(text.contains("truncated"));
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
