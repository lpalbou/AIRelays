"""Shared, live traffic-log policy. No dependency on HTTP or the desktop.

The policy belongs to the log directory, so desktop config regeneration cannot
overwrite it. A process lock serializes rotation, cleanup and policy updates
across CLI commands and relay workers; files are never held open between writes.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

MIB = 1024 * 1024
LOG_NAME = re.compile(r"\d{2}-\d{2}(?:\.[0-9a-f]{32})?\.log\Z")


@dataclass(frozen=True)
class LogRetentionPolicy:
    retention_days: int = 7
    max_total_mb: int = 1024
    max_file_mb: int = 50

    def __post_init__(self) -> None:
        for name, maximum, minimum in (
            ("retention_days", 36500, 0),
            ("max_total_mb", 1048576, 1),
            ("max_file_mb", 10240, 1),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
        if self.max_file_mb > self.max_total_mb:
            raise ValueError("max_file_mb must not exceed max_total_mb.")

    @classmethod
    def from_dict(cls, values: dict) -> LogRetentionPolicy:
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown log retention fields.")
        return cls(**values)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class LogPolicyStore:
    def __init__(self, logs_dir: Path, default: LogRetentionPolicy | None = None) -> None:
        self.logs_dir = logs_dir.absolute()
        self.path = self.logs_dir / ".retention.json"
        self.default = default or LogRetentionPolicy()
        self._thread_lock = threading.RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            # Never unlink the lock file: that would create independent locks
            # for processes still holding the original inode.
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.logs_dir / ".retention.lock", flags, 0o600)
            with os.fdopen(fd, "r+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> LogRetentionPolicy:
        try:
            with self.path.open(encoding="utf-8") as handle:
                return LogRetentionPolicy.from_dict(json.load(handle))
        except FileNotFoundError:
            return self.default

    def update(self, changes: dict) -> LogRetentionPolicy:
        with self.locked():
            policy = LogRetentionPolicy.from_dict({**self.load().as_dict(), **changes})
            fd, name = tempfile.mkstemp(prefix=".retention-", suffix=".tmp", dir=self.logs_dir)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(policy.as_dict(), handle)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return policy


def managed_logs(root: Path) -> list[tuple[Path, os.stat_result]]:
    """Only the relay's exact layout; no recursive traversal or symlinks.

    Include legacy hourly files and size-rotated UUID files. Unrecognized
    files, hard links and symlinked directories are outside our ownership.
    """
    result = []
    if not root.exists():
        return result
    with os.scandir(root) as years:
        for year in years:
            if not re.fullmatch(r"\d{4}", year.name) or not year.is_dir(follow_symlinks=False):
                continue
            with os.scandir(year.path) as months:
                for month in months:
                    if not re.fullmatch(r"0[1-9]|1[0-2]", month.name) or not month.is_dir(follow_symlinks=False):
                        continue
                    with os.scandir(month.path) as files:
                        for entry in files:
                            if not LOG_NAME.fullmatch(entry.name):
                                continue
                            day, hour = entry.name[:5].split("-")
                            try:
                                datetime(int(year.name), int(month.name), int(day), int(hour))
                            except ValueError:
                                continue
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                result.append((Path(entry.path), info))
    return sorted(result, key=lambda item: (item[1].st_mtime_ns, str(item[0])))
