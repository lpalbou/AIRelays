"""OpenAI-shaped error bodies (`ErrorResponse` in OpenAI's OpenAPI spec).

Every error AIRelays returns uses `{"error": {"message", "type", "param",
"code"}}`. A top-level `detail` mirror stays for existing AIRelays clients
(the desktop app and CLI read it); the spec allows extra keys.
"""

from __future__ import annotations

from typing import Any


def error_type(status_code: int) -> str:
    if status_code == 429:
        return "rate_limit_error"
    return "server_error" if status_code >= 500 else "invalid_request_error"


def error_body(message: str, *, status_code: int, code: str | None = None,
               param: str | None = None, type_: str | None = None) -> dict[str, Any]:
    return {
        "error": {"message": message, "type": type_ or error_type(status_code), "param": param, "code": code},
        "detail": message,
    }
