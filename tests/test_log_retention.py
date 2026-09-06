from __future__ import annotations

import json
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from airelay.app import create_app
from airelay.cli import build_parser
from airelay.config import Settings
from airelay.log_retention import LogPolicyStore, LogRetentionPolicy, MIB, managed_logs
from airelay.traffic import TrafficLogger


def old_log(root, name="2020/01/01-00.log", size=20, age_days=10):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    timestamp = time.time() - age_days * 86400
    os.utime(path, (timestamp, timestamp))
    return path


def logger(root, days=7, total=3, chunk=1):
    return TrafficLogger(root, LogRetentionPolicy(days, total, chunk))


def test_age_cleanup_and_unowned_files(tmp_path):
    expired = old_log(tmp_path)
    recent = old_log(tmp_path, "2026/01/01-01.log", age_days=2)
    unknown = old_log(tmp_path, "notes.log", age_days=100)
    nested_unknown = old_log(tmp_path, "2020/01/debug.log", age_days=100)
    log = logger(tmp_path)
    log.maintain()
    assert not expired.exists()
    assert expired.parent.exists()  # Contains an unrelated file; preserve it.
    assert recent.exists() and unknown.exists() and nested_unknown.exists()
    assert log.status()["deleted_files"] == 1


def test_cleanup_removes_only_empty_owned_directories(tmp_path):
    expired = old_log(tmp_path)
    logger(tmp_path).maintain()
    assert not expired.parent.parent.exists()


def test_size_limit_deletes_oldest_even_without_age_limit(tmp_path):
    oldest = old_log(tmp_path, "2020/01/01-00.log", MIB, 30)
    recent = old_log(tmp_path, "2020/01/01-01.log", MIB, 1)
    log = logger(tmp_path, days=0, total=2)
    log.write({"request_id": "latest"})
    assert not oldest.exists()
    assert recent.exists()
    assert log.status()["usage_bytes"] <= 2 * MIB


def test_rotation_and_oversized_record_are_bounded_valid_json(tmp_path):
    log = logger(tmp_path, total=2)
    for index in range(12):
        log.write({"request_id": str(index), "body": "x" * 400_000})
    files = managed_logs(tmp_path)
    assert sum(info.st_size for _, info in files) <= 2 * MIB
    assert all(info.st_size <= MIB for _, info in files)
    assert any(len(path.stem) > 5 for path, _ in files)
    log.write({"request_id": "huge", "phase": "inbound_request", "body": "x" * (3 * MIB)})
    records = [json.loads(line) for path, _ in managed_logs(tmp_path) for line in path.read_text().splitlines()]
    huge = next(record for record in records if record["request_id"] == "huge")
    assert "log_record_omitted" in huge
    assert huge["original_bytes"] > MIB
    assert "body" not in huge
    assert log.status()["oversized_records"] == 1
    assert log.status()["usage_bytes"] <= 2 * MIB


def test_hourly_rollover_reserves_space_for_new_active_file(tmp_path, monkeypatch):
    from airelay import traffic

    class Clock(datetime):
        hour = 1

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 6, cls.hour, tzinfo=UTC)

    monkeypatch.setattr(traffic, "datetime", Clock)
    log = logger(tmp_path, days=0, total=1)
    log.write({"request_id": "first-hour", "body": "x" * 800_000})
    Clock.hour = 2
    log.write({"request_id": "second-hour", "body": "x" * 800_000})
    assert log.status()["usage_bytes"] <= MIB
    assert log.status()["deleted_files"] == 1


def test_legacy_oversized_current_file_rotated_on_idle_cleanup(tmp_path):
    log = logger(tmp_path, total=2)
    current = log._log_path(datetime.now(UTC))
    current.parent.mkdir(parents=True)
    current.write_bytes(b"x" * (4 * MIB))
    log.maintain()
    assert log.status()["usage_bytes"] == 0
    assert log.status()["deleted_bytes"] == 4 * MIB
    log.write({"request_id": "after-migration"})
    assert json.loads(current.read_text())["request_id"] == "after-migration"


def test_policy_change_shared_by_existing_writer_and_survives_restart(tmp_path):
    log = logger(tmp_path, days=30)
    old = old_log(tmp_path, age_days=20)
    log.maintain()
    assert old.exists()
    store = LogPolicyStore(tmp_path)
    store.update({"retention_days": 7, "max_total_mb": 2, "max_file_mb": 1})
    assert log.status()["policy"]["retention_days"] == 7
    log.write({"request_id": "reload"})
    assert not old.exists()
    restarted = TrafficLogger(tmp_path, LogRetentionPolicy(90, 100, 10))
    assert restarted.status()["policy"] == store.load().as_dict()


@pytest.mark.parametrize("changes", [
    {"retention_days": -1}, {"retention_days": 1.5}, {"retention_days": True},
    {"retention_days": "7"}, {"retention_days": None}, {"max_total_mb": 0},
    {"max_file_mb": 0}, {"max_total_mb": 10}, {"unknown": 1},
])
def test_invalid_policy_does_not_change_saved_file(tmp_path, changes):
    store = LogPolicyStore(tmp_path)
    store.update({"retention_days": 30})
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.update(changes)
    assert store.path.read_bytes() == before


def test_failed_atomic_save_keeps_previous_policy(tmp_path, monkeypatch):
    store = LogPolicyStore(tmp_path)
    store.update({"retention_days": 30})
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.update({"retention_days": 7})
    assert store.load().retention_days == 30
    assert not list(tmp_path.glob("*.tmp"))


def test_symlinks_and_hardlinks_are_not_managed_or_written(tmp_path):
    root = tmp_path / "logs"
    outside = old_log(tmp_path / "outside")
    month = root / "2020/01"
    month.mkdir(parents=True)
    (month / "01-00.log").symlink_to(outside)
    os.link(outside, month / "01-01.log")
    (root / "2019").symlink_to(outside.parent.parent, target_is_directory=True)
    log = logger(root)
    log.maintain()
    assert outside.exists() and outside.stat().st_size == 20
    assert (month / "01-00.log").is_symlink()
    assert (month / "01-01.log").exists()
    current = log._log_path(datetime.now(UTC))
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(outside)
    log.maintain()
    log.write({"secret": "must not escape"})
    assert outside.read_bytes() == b"x" * 20
    assert log._last_error is not None


def test_symlinked_current_year_is_not_rotated(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "logs"
    now = datetime.now(UTC)
    path = old_log(outside, now.strftime("%m/%d-%H.log"), 2 * MIB)
    root.mkdir()
    (root / now.strftime("%Y")).symlink_to(outside, target_is_directory=True)
    log = logger(root)
    log.maintain()
    log.write({"request_id": "unsafe"})
    assert path.stat().st_size == 2 * MIB
    assert log._last_error is not None


def test_cleanup_failure_visible_and_does_not_break_relay_or_grow_logs(tmp_path, monkeypatch):
    old = old_log(tmp_path)
    original_unlink = Path.unlink
    def deny(path, *args, **kwargs):
        if path == old:
            raise PermissionError("cleanup denied")
        return original_unlink(path, *args, **kwargs)
    log = logger(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", deny)
        log.maintain()
        for _ in range(5):
            log.write({"request_id": "no-crash"})
        assert log.status()["last_error"] == "cleanup denied"
        assert log.status()["usage_bytes"] == 20
        assert log.status()["dropped_records"] == 5
    log.maintain()
    log.write({"request_id": "recovered"})
    assert log.status()["last_error"] is None
    assert not old.exists()


def _write_worker(root, worker):
    log = logger(Path(root), days=0, total=3)
    for index in range(30):
        log.write({"request_id": f"{worker}-{index}", "body": "x" * 40_000})


def test_multiple_process_writers_share_rotation_and_disk_budget(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_write_worker, args=(str(tmp_path), i)) for i in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    files = managed_logs(tmp_path)
    assert sum(info.st_size for _, info in files) <= 3 * MIB
    assert all(info.st_size <= MIB for _, info in files)
    records = [json.loads(line) for path, _ in files for line in path.read_text().splitlines()]
    assert records
    assert len({entry["request_id"] for entry in records}) == len(records)


def test_policy_partial_updates_are_serialized(tmp_path):
    def update(changes):
        LogPolicyStore(tmp_path).update(changes)
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(update, [{"retention_days": 30}, {"max_total_mb": 2048}]))
    policy = LogPolicyStore(tmp_path).load()
    assert policy.retention_days == 30 and policy.max_total_mb == 2048


def settings_for(root):
    return Settings(
        data_dir=root / "data", logs_dir=root / "logs", config_path=root / "config.toml",
        bearer_token_file=root / "token", bearer_token="test-token",
        enable_openai_provider=False, enable_claude=False,
    )


def test_api_requires_auth_validates_persists_and_applies(tmp_path):
    settings = settings_for(tmp_path)
    old = old_log(settings.logs_dir, age_days=20)
    settings.log_retention_days = 30
    with TestClient(create_app(settings)) as client:
        assert old.exists()
        assert client.get("/v1/relay/logging").status_code == 401
        assert client.put("/v1/relay/logging", json={"retention_days": 1}).status_code == 401
        client.headers["Authorization"] = "Bearer test-token"
        assert client.get("/v1/relay/logging").json()["policy"]["retention_days"] == 30
        response = client.put("/v1/relay/logging", json={"retention_days": 7})
        assert response.status_code == 200
        assert not old.exists()
        assert response.json()["deleted_files"] == 1
        assert client.put("/v1/relay/logging", json={"max_file_mb": False}).status_code == 422
        assert client.put("/v1/relay/logging", json={"max_total_mb": 0}).status_code == 422
        assert client.get("/v1/relay/logging").json()["policy"]["max_total_mb"] == 1024
    assert TrafficLogger(settings.logs_dir).status()["policy"]["retention_days"] == 7


def test_idle_cleanup_runs_and_task_stops_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(TrafficLogger, "cleanup_interval_seconds", 0.02)
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings)):
        old = old_log(settings.logs_dir)
        deadline = time.monotonic() + 3
        while old.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not old.exists()
    later = old_log(settings.logs_dir)
    time.sleep(0.08)
    assert later.exists()


def test_cli_offline_shares_policy_and_config_roundtrip(tmp_path, capsys):
    settings = settings_for(tmp_path)
    settings.log_retention_days = 30
    settings.write_config_file()
    assert Settings.from_sources(settings.config_path).log_policy().retention_days == 30
    parser = build_parser()
    args = parser.parse_args(["logs", "--config", str(settings.config_path), "--retention-days", "7", "--json"])
    args.func(args)
    result = json.loads(capsys.readouterr().out)
    assert result["policy"]["retention_days"] == 7
    assert result["policy_source"] == "saved"
    assert Settings.from_sources(settings.config_path).log_policy().retention_days == 30
    with TestClient(create_app(settings)) as client:
        response = client.get("/v1/relay/logging", headers={"Authorization": "Bearer test-token"})
        assert response.json()["policy"]["retention_days"] == 7
