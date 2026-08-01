from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._lock = threading.Lock()

    def _log_path(self, now: datetime) -> Path:
        year = now.strftime("%Y")
        month = now.strftime("%m")
        day = now.strftime("%d")
        hour = now.strftime("%H")
        return self._logs_dir / year / month / f"{day}-{hour}.log"

    def write(self, entry: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        entry.setdefault("logged_at", now.isoformat())
        path = self._log_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(redact_value(entry), ensure_ascii=True, separators=(",", ":"))
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
