from __future__ import annotations

import json

import pytest

from airelay.backend import BackendError
from airelay.retry import RetryPolicy, is_retriable, retry_call


class RecordingTraffic:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def failing_then(results: list) -> tuple[callable, list[int]]:
    """An operation that pops one scripted outcome per call: exceptions are
    raised, anything else returned. Also returns a call counter."""
    calls = [0]

    async def operation():
        calls[0] += 1
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return operation, calls


def test_policy_delay_schedule_repeats_last_entry() -> None:
    policy = RetryPolicy(attempts=5, backoff_seconds=(5.0, 20.0, 60.0))
    assert [policy.delay_for(i) for i in range(5)] == [5.0, 20.0, 60.0, 60.0, 60.0]
    assert RetryPolicy(attempts=2, backoff_seconds=()).delay_for(0) == 0.0


def test_is_retriable_classification() -> None:
    assert is_retriable(BackendError(429, "limit"))
    assert is_retriable(BackendError(502, "bad gateway"))
    assert is_retriable(BackendError(503, "unavailable"))
    assert not is_retriable(BackendError(400, "bad request"))
    assert not is_retriable(BackendError(401, "auth"))
    assert not is_retriable(BackendError(422, "invalid"))


@pytest.mark.asyncio
async def test_retry_call_returns_first_success_without_sleeping() -> None:
    operation, calls = failing_then(["ok"])
    sleep = SleepRecorder()
    result = await retry_call(
        operation,
        policy=RetryPolicy(),
        request_id="req_1",
        traffic=RecordingTraffic(),
        sleep=sleep,
    )
    assert result == "ok"
    assert calls[0] == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_retry_call_retries_transient_failure_then_returns_success() -> None:
    operation, calls = failing_then([BackendError(502, "overloaded"), "ok"])
    sleep = SleepRecorder()
    traffic = RecordingTraffic()
    result = await retry_call(
        operation,
        policy=RetryPolicy(attempts=3, backoff_seconds=(5.0, 20.0, 60.0)),
        request_id="req_1",
        traffic=traffic,
        sleep=sleep,
    )
    assert result == "ok"
    assert calls[0] == 2
    assert sleep.delays == [5.0]
    assert len(traffic.records) == 1
    record = traffic.records[0]
    assert record["phase"] == "retry_backoff"
    assert record["attempt"] == 1
    assert record["max_attempts"] == 3
    assert record["delay_seconds"] == 5.0
    assert record["status_code"] == 502


@pytest.mark.asyncio
async def test_retry_call_exhausts_and_raises_last_error() -> None:
    errors = [BackendError(502, f"fail {i}") for i in range(4)]
    operation, calls = failing_then(list(errors))
    sleep = SleepRecorder()
    traffic = RecordingTraffic()
    with pytest.raises(BackendError) as excinfo:
        await retry_call(
            operation,
            policy=RetryPolicy(attempts=3, backoff_seconds=(5.0, 20.0, 60.0)),
            request_id="req_1",
            traffic=traffic,
            sleep=sleep,
        )
    assert excinfo.value.detail == "fail 3"
    assert calls[0] == 4
    assert sleep.delays == [5.0, 20.0, 60.0]
    assert [r["attempt"] for r in traffic.records] == [1, 2, 3]


@pytest.mark.asyncio
async def test_retry_call_does_not_retry_client_errors() -> None:
    operation, calls = failing_then([BackendError(422, "invalid request")])
    sleep = SleepRecorder()
    with pytest.raises(BackendError):
        await retry_call(
            operation,
            policy=RetryPolicy(),
            request_id="req_1",
            traffic=RecordingTraffic(),
            sleep=sleep,
        )
    assert calls[0] == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_retry_call_attempts_zero_disables_retry() -> None:
    operation, calls = failing_then([BackendError(502, "overloaded")])
    with pytest.raises(BackendError):
        await retry_call(
            operation,
            policy=RetryPolicy(attempts=0),
            request_id="req_1",
            traffic=RecordingTraffic(),
            sleep=SleepRecorder(),
        )
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_retry_call_skips_futile_wait_for_long_quota_reset() -> None:
    detail = json.dumps(
        {"error": {"type": "usage_limit_reached", "message": "spent", "resets_in_seconds": 9000}}
    )
    operation, calls = failing_then([BackendError(429, detail)])
    sleep = SleepRecorder()
    with pytest.raises(BackendError):
        await retry_call(
            operation,
            policy=RetryPolicy(attempts=3, backoff_seconds=(5.0, 20.0, 60.0)),
            request_id="req_1",
            traffic=RecordingTraffic(),
            sleep=sleep,
        )
    assert calls[0] == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_retry_call_retries_when_quota_resets_within_budget() -> None:
    detail = json.dumps(
        {"error": {"type": "rate_limit_reached", "message": "burst", "resets_in_seconds": 10}}
    )
    operation, calls = failing_then([BackendError(429, detail), "ok"])
    sleep = SleepRecorder()
    result = await retry_call(
        operation,
        policy=RetryPolicy(attempts=3, backoff_seconds=(5.0, 20.0, 60.0)),
        request_id="req_1",
        traffic=RecordingTraffic(),
        sleep=sleep,
    )
    assert result == "ok"
    assert calls[0] == 2
    assert sleep.delays == [5.0]


@pytest.mark.asyncio
async def test_retry_call_stops_when_client_disconnected() -> None:
    operation, calls = failing_then([BackendError(502, "overloaded"), "ok"])

    async def disconnected() -> bool:
        return True

    sleep = SleepRecorder()
    traffic = RecordingTraffic()
    with pytest.raises(BackendError):
        await retry_call(
            operation,
            policy=RetryPolicy(),
            request_id="req_1",
            traffic=traffic,
            should_abort=disconnected,
            sleep=sleep,
        )
    assert calls[0] == 1
    assert sleep.delays == []
    skipped = [r for r in traffic.records if r["phase"] == "retry_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "client_disconnected"


@pytest.mark.asyncio
async def test_retry_call_aborts_after_sleep_when_client_disconnects_mid_backoff() -> None:
    """A client that hangs up during the backoff wait must not cost another
    full upstream attempt."""
    operation, calls = failing_then([BackendError(502, "overloaded"), "ok"])
    checks = {"count": 0}

    async def disconnects_during_sleep() -> bool:
        checks["count"] += 1
        return checks["count"] > 1  # connected before the sleep, gone after

    sleep = SleepRecorder()
    with pytest.raises(BackendError):
        await retry_call(
            operation,
            policy=RetryPolicy(attempts=3, backoff_seconds=(5.0,)),
            request_id="req_1",
            traffic=RecordingTraffic(),
            should_abort=disconnects_during_sleep,
            sleep=sleep,
        )
    assert calls[0] == 1  # the second upstream attempt never ran
    assert sleep.delays == [5.0]


@pytest.mark.asyncio
async def test_retry_call_logs_futile_skip() -> None:
    detail = json.dumps(
        {"error": {"type": "usage_limit_reached", "message": "spent", "resets_in_seconds": 9000}}
    )
    operation, calls = failing_then([BackendError(429, detail)])
    traffic = RecordingTraffic()
    with pytest.raises(BackendError):
        await retry_call(
            operation,
            policy=RetryPolicy(attempts=3, backoff_seconds=(5.0, 20.0, 60.0)),
            request_id="req_1",
            traffic=traffic,
            sleep=SleepRecorder(),
        )
    skipped = [r for r in traffic.records if r["phase"] == "retry_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "quota_resets_beyond_backoff"
    assert skipped[0]["resets_in_seconds"] == 9000.0
