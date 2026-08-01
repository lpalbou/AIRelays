"""Automatic retry with exponential backoff for upstream LLM calls.

An episodic upstream bad window (e.g. the 2026-08-01 `server_is_overloaded`
incident) fails requests for seconds to minutes while the very same request
succeeds moments later. The relay retries the whole attempt — including the
account pool's failover pass — a configurable number of times before giving
the client the real error.

Retries only happen where they are honest: before any response byte has
reached the client. Streaming lanes therefore retry only the pre-header
phase; once SSE headers are committed a failure surfaces in-band instead.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from airelay.backend import BackendError

T = TypeVar("T")

# What retrying can plausibly fix: transient upstream failures (5xx, incl.
# the structured 502s minted for in-stream failures and transport errors)
# and rate windows (429). Client errors (4xx) would fail identically and
# auth errors need user action, so both surface immediately.
_RETRIABLE_EXACT = 429
_RETRIABLE_MIN = 500


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to re-attempt a failed upstream call and how long to
    wait before each retry. `attempts` counts retries after the initial
    call; 0 disables retrying. A schedule shorter than `attempts` repeats
    its last delay."""

    attempts: int = 3
    backoff_seconds: tuple[float, ...] = (5.0, 20.0, 60.0)

    def delay_for(self, retry_index: int) -> float:
        if not self.backoff_seconds:
            return 0.0
        if retry_index < len(self.backoff_seconds):
            return max(0.0, float(self.backoff_seconds[retry_index]))
        return max(0.0, float(self.backoff_seconds[-1]))

    def remaining_budget(self, retry_index: int) -> float:
        """Total sleep still ahead if every remaining retry runs."""
        return sum(self.delay_for(index) for index in range(retry_index, self.attempts))


def is_retriable(error: BackendError) -> bool:
    return error.status_code == _RETRIABLE_EXACT or error.status_code >= _RETRIABLE_MIN


def _resets_in_seconds(error: BackendError) -> float | None:
    """The upstream's own recovery estimate, when its error body carries
    one (usage-limit errors do). Used to skip retries that cannot succeed:
    waiting 85 seconds for a quota window that resets in three hours only
    delays the honest 429."""
    try:
        payload = json.loads(error.detail or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("error")
    if not isinstance(inner, dict):
        return None
    resets = inner.get("resets_in_seconds")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool) and resets > 0:
        return float(resets)
    return None


async def retry_call(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    request_id: str,
    traffic: Any,
    should_abort: Callable[[], Awaitable[bool]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Runs `operation`, retrying retriable BackendErrors per `policy`.

    Every retry writes a `retry_backoff` traffic record so the traffic log
    shows what happened between the failing attempt and the final outcome.
    `should_abort` (e.g. `request.is_disconnected`) stops retrying for a
    client that is no longer waiting for the answer.
    """
    retry_index = 0
    while True:
        try:
            return await operation()
        except BackendError as error:
            if retry_index >= policy.attempts or not is_retriable(error):
                raise
            resets = _resets_in_seconds(error)
            if resets is not None and resets > policy.remaining_budget(retry_index):
                _log_retry_skipped(
                    traffic, request_id, error, "quota_resets_beyond_backoff", resets
                )
                raise
            if should_abort is not None and await should_abort():
                _log_retry_skipped(traffic, request_id, error, "client_disconnected", None)
                raise
            delay = policy.delay_for(retry_index)
            traffic.write(
                {
                    "request_id": request_id,
                    "phase": "retry_backoff",
                    "attempt": retry_index + 1,
                    "max_attempts": policy.attempts,
                    "delay_seconds": delay,
                    "status_code": error.status_code,
                    "reason": (error.detail or "")[:500],
                }
            )
            await sleep(delay)
            # Re-check after the wait: a client that hung up during a long
            # backoff must not cost another full upstream attempt.
            if should_abort is not None and await should_abort():
                _log_retry_skipped(traffic, request_id, error, "client_disconnected", None)
                raise
            retry_index += 1


def _log_retry_skipped(
    traffic: Any,
    request_id: str,
    error: BackendError,
    reason: str,
    resets_in_seconds: float | None,
) -> None:
    """Explains an *absent* retry in the traffic log — a retriable failure
    with retries still available that was deliberately not retried."""
    record: dict[str, Any] = {
        "request_id": request_id,
        "phase": "retry_skipped",
        "reason": reason,
        "status_code": error.status_code,
    }
    if resets_in_seconds is not None:
        record["resets_in_seconds"] = resets_in_seconds
    traffic.write(record)
