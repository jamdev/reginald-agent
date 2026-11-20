"""Single-writer run lock (flock). The vault's concurrency rules assume serial
agent writers; this makes them trivially true."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class LockHeldError(RuntimeError):
    pass


@contextmanager
def run_lock(lock_path: Path, blocking: bool = False):
    lock_path = lock_path.expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise LockHeldError(f"another run holds the lock at {lock_path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
