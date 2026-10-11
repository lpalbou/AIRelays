"""Owner-only files and directories, the same way on every OS (ADR 0008).

POSIX gets 0700 directories and 0600 files from the moment they exist (no
write-then-chmod window). Windows ignores POSIX modes; files under the user
profile inherit its owner/SYSTEM/Administrators ACL instead.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import time
from pathlib import Path

_WINDOWS = sys.platform == "win32"


def ensure_private_dir(path: Path) -> Path:
    """Create `path` (and missing parents) owner-only. An existing directory
    is tightened when we own it; a mounted volume owned by someone else is
    left alone rather than failing the relay."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _WINDOWS:
        try:
            if path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077:
                os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically replace `path` with `data`, readable only by the owner.

    The temporary file is created 0600 in the same directory, flushed to
    disk, then renamed over the target, so readers never see a partial
    file and a crash never leaves a truncated credential."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    if not _WINDOWS:
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass


def _replace(source: str, target: Path) -> None:
    # Windows refuses to replace a file another process (antivirus, an
    # indexer, a reader) holds open; those holds are brief.
    for attempt in range(10):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if not _WINDOWS or getattr(error, "winerror", None) not in (5, 32) or attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


class LockUnsupported(OSError):
    """The filesystem refuses file locks (e.g. NFS without lockd)."""


class InterProcessLock:
    """Exclusive lock on a sidecar file shared by every AIRelays process.

    The lock file is never deleted or replaced, so all processes lock the
    same inode. Acquisition polls a non-blocking lock until a deadline, so
    it can run in a worker thread without hanging forever."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, timeout: float) -> None:
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock(fd)
                self._fd = fd
                return
            except OSError as error:
                if error.errno not in _CONTENDED:
                    os.close(fd)
                    raise LockUnsupported(error.errno, f"File locking is unsupported for {self.path}") from error
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise TimeoutError(f"Timed out waiting for {self.path.name}") from None
                time.sleep(0.1)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)


if _WINDOWS:
    import msvcrt

    # msvcrt reports a held byte range as EACCES (or EDEADLOCK).
    _CONTENDED = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    _CONTENDED = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
