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
    pub status_code: Option<i64>,
    pub last_phase: String,
    pub event_count: usize,
    pub details: String,
}

/// Parses the most recent log files and groups records by request id,
/// newest first.
pub fn recent_requests() -> Vec<RequestSummary> {
    let mut files = log_files();
    files.sort_by_key(|(modified, _)| std::cmp::Reverse(*modified));

    let mut grouped: HashMap<String, Vec<Value>> = HashMap::new();
    for (_, path) in files.into_iter().take(3) {
        let Some(content) = read_tail(&path, 512 * 1024) else {
            continue;
        };
        for line in content.lines().rev().take(400) {
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
            grouped.entry(request_id.to_string()).or_default().push(record);
        }
    }

    let mut summaries: Vec<RequestSummary> = grouped
        .into_iter()
        .map(|(id, mut records)| {
            records.sort_by(|a, b| timestamp_of(a).cmp(&timestamp_of(b)));
            summarize(id, records)
        })
        // The dashboard's own status polling is monitoring noise, not
        // user traffic.
        .filter(|summary| summary.path != "/v1/relay/status")
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

fn log_files() -> Vec<(std::time::SystemTime, PathBuf)> {
    let Ok(entries) = std::fs::read_dir(AppSettings::logs_dir()) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|entry| {
            entry
                .path()
                .extension()
                .map(|extension| extension == "log")
                .unwrap_or(false)
        })
        .filter_map(|entry| {
            let modified = entry.metadata().ok()?.modified().ok()?;
            Some((modified, entry.path()))
        })
        .collect()
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

fn summarize(id: String, records: Vec<Value>) -> RequestSummary {
    let status_code = records
        .iter()
        .rev()
        .find_map(|record| record.get("status_code").and_then(Value::as_i64));
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
        last_phase: string_field(&records, "phase", "unknown"),
        event_count: records.len(),
        status_code,
        details,
        id,
    }
}
