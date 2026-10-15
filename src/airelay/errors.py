"""OpenAI-shaped error bodies (`ErrorResponse` in OpenAI's OpenAPI spec).

Every error AIRelays returns uses `{"error": {"message", "type", "param",
"code"}}`. A top-level `detail` mirror stays for existing AIRelays clients
(the desktop app and CLI read it); the spec allows extra keys.
"""

from __future__ import annotations

from typing import Any


class InvalidRequestError(ValueError):
    """A request AIRelays refuses before any upstream call, for every route
    and runtime: 422 (413 for size), with an optional OpenAI `code` and
    `param`. The message says what to change."""

    status_code = 422

    def __init__(self, message: str, *, code: str | None = None, param: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.param = param


class RequestTooLargeError(InvalidRequestError):
    status_code = 413

    def __init__(self, message: str) -> None:
        super().__init__(message, code="request_too_large")


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
