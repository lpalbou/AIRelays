# Bounded Traffic-Log Retention

## Context

AIRelays writes redacted traffic records as JSONL files. Files rotate hourly,
but a busy relay can create substantial disk usage unless logs also have a
retention policy. The relay can run from the CLI or through the desktop tray,
and the tray regenerates its relay configuration before launch.

## Decision

The relay owns traffic-log retention. The CLI, authenticated relay API, and
desktop Settings page use the same saved policy in the log directory.

The policy has three limits:

- `retention_days` limits file age;
- `max_total_mb` limits total managed traffic-log contents;
- `max_file_mb` rotates an active log file before it exceeds the configured
  size.

The defaults are 7 days, 1024 MiB total, and 50 MiB per file. Both age and
size limits apply, and the oldest managed files are deleted first. Cleanup runs
at startup, periodically while the relay is running, when a file rotates, and
when the policy changes.

Policy updates are atomically saved as `<logs_dir>/.retention.json`. That
policy overrides the corresponding TOML defaults so tray configuration
generation cannot replace a live API or CLI update. A process lock coordinates
policy updates, rotation, writes, and cleanup among relay processes that share
the log directory.

Cleanup manages only AIRelays traffic files in the documented date-based
layout. It does not follow symlinks or manage hard links, console output,
uploads, conversations, or unrelated files. A record too large for one log
file is represented by an explicit omission record containing its request ID,
phase, original size, and digest. If cleanup cannot enforce the policy, traffic
logging pauses and reports the error; serving requests continues.

## Consequences

The same bounded behavior applies to standalone and tray-managed relays without
requiring a platform-specific log-rotation service. Existing eligible traffic
logs are evaluated on the first upgraded start, so users should archive any
history they need before enabling or tightening retention.

Retention removes complete files permanently. It provides an upper bound on
traffic-log contents rather than a guaranteed number of days of history: a busy
relay can reach the size limit before the age limit. All writers sharing a log
directory must use a retention-aware AIRelays version; external writers and
older relay versions are outside this policy.

See [Configuration](../configuration.md#traffic-log-retention) for operation
and [API Notes](../api.md#traffic-log-retention-api) for the HTTP contract.
