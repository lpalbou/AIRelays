from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from airelay.log_retention import LogPolicyStore, LogRetentionPolicy, MIB, managed_logs


REDACTED_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    # OAuth authorization codes; scoped by _is_error_object so upstream
    # error codes (diagnostic vocabulary, not secrets) stay readable.
    "code",
    "bearer_token",
    "relay_token",
    "file_data",
}


def _is_text_content_type(content_type: str | None) -> bool:
    if not content_type:
        return True
    lowered = content_type.lower()
    return (
        lowered.startswith("text/")
        or "json" in lowered
        or "xml" in lowered
        or "javascript" in lowered
        or "x-www-form-urlencoded" in lowered
    )


def _is_error_object(container: dict[str, Any], parent_key: str | None) -> bool:
    """Whether a dict is an OpenAI-style error object, where `code` is
    diagnostic vocabulary ("context_length_exceeded"), not a credential.

    The blanket `code` redaction exists for OAuth authorization codes
    (callback query params {"code", "state"}, token-exchange bodies) — a
    one-time credential that never lives under an "error" key and never
    carries a "message" sibling. Error objects always do one or the other:
    the stream `error` event nests them under "error", `response.failed`
    under response["error"], and the inline variant ships code+message at
    the top level.

    operator 2026-08-01: every upstream error code in the incident's
    traffic log read "[REDACTED]" — the primary classification signal for
    diagnosing a misrouted invalid_request_error was scrubbed as if it
    were a secret, while the actual response sent to the client (composed
    from the unscrubbed BackendError, not from the log) kept the real
    code. Redaction happens at log-serialization time only.
    """
    return (parent_key or "").lower() == "error" or isinstance(
        container.get("message"), str
    )


def redact_value(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        in_error_object = _is_error_object(value, parent_key)
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if lowered in REDACTED_KEYS and not (lowered == "code" and in_error_object):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_value(item, parent_key=key)
        return redacted
    if isinstance(value, list):
        return [redact_value(item, parent_key=parent_key) for item in value]
    return value


def snapshot_body(content_type: str | None, body: bytes) -> dict[str, Any]:
    if not body:
        return {"kind": "empty", "bytes": 0}

    sha256 = hashlib.sha256(body).hexdigest()
    if _is_text_content_type(content_type):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {
                "kind": "text",
                "bytes": len(body),
                "sha256": sha256,
                "text": text,
            }
        return {
            "kind": "json",
            "bytes": len(body),
            "sha256": sha256,
            "json": redact_value(parsed),
        }
    return {
        "kind": "binary_summary",
        "bytes": len(body),
        "sha256": sha256,
        "note": "Binary payload omitted by default. This is explicit, not silent truncation.",
    }


class TrafficLogger:
    cleanup_interval_seconds = 60

    def __init__(self, logs_dir: Path, policy: LogRetentionPolicy | None = None) -> None:
        self.policy_store = LogPolicyStore(logs_dir, policy)
        self._logs_dir = self.policy_store.logs_dir
        self._policy = self.policy_store.load()
        self._checked_policy: LogRetentionPolicy | None = None
        self._lock = threading.RLock()
        self._last_cleanup = float("-inf")
        self._last_path: Path | None = None
        self._last_warning = float("-inf")
        self._last_error: str | None = None
        self._last_cleanup_at: str | None = None
        self._deleted_files = 0
        self._deleted_bytes = 0
        self._oversized_records = 0
        self._dropped_records = 0

    def _reload_policy(self) -> bool:
        policy = self.policy_store.load()
        changed = policy != self._checked_policy
        self._policy = policy
        return changed

    def _failure(self, error: Exception) -> None:
        self._last_error = str(error)
        now = time.monotonic()
        if now - self._last_warning >= self.cleanup_interval_seconds:
            logging.getLogger(__name__).warning("Traffic log storage: %s", error)
            self._last_warning = now

    def _safe_current_path(self, now: datetime) -> Path:
        path = self._log_path(now)
        for directory in (path.parent.parent, path.parent):
            directory.mkdir(exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise OSError(f"Refusing unsafe log directory: {directory}")
        return path

    @staticmethod
    def _size(path: Path) -> int:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"Refusing unsafe log file: {path}")
        return info.st_size

    def _rotate(self, path: Path) -> None:
        path.rename(path.with_name(f"{path.stem}.{uuid.uuid4().hex}.log"))

    def _cleanup(self, now: datetime, current: Path) -> None:
        self._checked_policy = self._policy
        self._last_cleanup = time.monotonic()
        files = managed_logs(self._logs_dir)
        total = sum(info.st_size for _, info in files)
        # Reserve enough space for the entire active chunk. Writers sharing
        # this directory rotate the same hourly file under the process lock.
        # No directory walk is needed for every streamed record.
        reserve = max(0, self._policy.max_file_mb * MIB - self._size(current))
        target = self._policy.max_total_mb * MIB - reserve
        cutoff = now.timestamp() - self._policy.retention_days * 86400
        errors = []
        for path, info in files:
            expired = self._policy.retention_days > 0 and info.st_mtime <= cutoff
            if path == current or (not expired and total <= target):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                total -= info.st_size
                continue
            except OSError as error:
                errors.append(str(error))
                continue
            total -= info.st_size
            self._deleted_files += 1
            self._deleted_bytes += info.st_size
            # Remove only empty directories in the recognized layout.
            for directory in (path.parent, path.parent.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        self._last_cleanup = time.monotonic()
        self._last_cleanup_at = now.isoformat()
        if errors:
            raise OSError("; ".join(errors[:3]))
        self._last_error = None

    def maintain(self) -> None:
        """Startup/idle cleanup; failures remain visible without breaking relay requests."""
        with self._lock:
            try:
                with self.policy_store.locked():
                    self._reload_policy()
                    now = datetime.now(UTC)
                    current = self._safe_current_path(now)
                    if self._size(current) >= self._policy.max_file_mb * MIB:
                        self._rotate(current)
                    self._cleanup(now, current)
            except (OSError, ValueError, TypeError) as error:
                self._failure(error)

    def configure(self, changes: dict[str, Any]) -> dict[str, Any]:
        self.policy_store.update(changes)
        self.maintain()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock, self.policy_store.locked():
            self._reload_policy()
            files = managed_logs(self._logs_dir)
            total = sum(info.st_size for _, info in files)
            return {
                "policy": self._policy.as_dict(),
                "policy_source": "saved" if self.policy_store.path.exists() else "config",
                "policy_path": str(self.policy_store.path),
                "logs_dir": str(self._logs_dir),
                "usage_bytes": total,
                "file_count": len(files),
                "over_budget": total > self._policy.max_total_mb * MIB,
                "cleanup_interval_seconds": self.cleanup_interval_seconds,
                "last_cleanup_at": self._last_cleanup_at,
                "deleted_files": self._deleted_files,
                "deleted_bytes": self._deleted_bytes,
                "oversized_records": self._oversized_records,
                "dropped_records": self._dropped_records,
                "last_error": self._last_error,
            }

    def _log_path(self, now: datetime) -> Path:
        year = now.strftime("%Y")
        month = now.strftime("%m")
        day = now.strftime("%d")
        hour = now.strftime("%H")
        return self._logs_dir / year / month / f"{day}-{hour}.log"

    def write(self, entry: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        entry.setdefault("logged_at", now.isoformat())
        line = json.dumps(redact_value(entry), ensure_ascii=True, separators=(",", ":"))
        with self._lock:
            try:
                with self.policy_store.locked():
                    now = datetime.now(UTC)
                    changed = self._reload_policy()
                    due = time.monotonic() - self._last_cleanup >= self.cleanup_interval_seconds
                    if self._last_error and not changed and not due:
                        self._dropped_records += 1
                        return
                    limit = self._policy.max_file_mb * MIB
                    if len(line) + 1 > limit:
                        # A single huge request cannot defeat the disk cap.
                        # Keep an explicit, correlatable omission record.
                        line = json.dumps({
                            "logged_at": now.isoformat(),
                            "request_id": str(entry.get("request_id", ""))[:256],
                            "phase": str(entry.get("phase", ""))[:128],
                            "log_record_omitted": "Record exceeds max_file_mb; payload omitted.",
                            "original_bytes": len(line) + 1,
                            "sha256": hashlib.sha256(line.encode("ascii")).hexdigest(),
                        }, ensure_ascii=True)
                        self._oversized_records += 1
                    path = self._safe_current_path(now)
                    size = self._size(path)
                    rotated = size > 0 and size + len(line) + 1 > limit
                    if rotated:
                        self._rotate(path)
                    if changed or rotated or due or path != self._last_path:
                        self._cleanup(now, path)
                    self._last_path = path
                    # Cleanup may have removed the now-empty month directory.
                    path = self._safe_current_path(now)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(path, flags, 0o600)
                    with os.fdopen(fd, "a", encoding="ascii") as handle:
                        handle.write(line + "\n")
            except (OSError, ValueError, TypeError) as error:
                self._dropped_records += 1
                self._failure(error)
