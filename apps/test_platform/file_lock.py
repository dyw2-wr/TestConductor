"""Cross-process non-blocking file locks released by the operating system."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator

from django.core.exceptions import ValidationError


@contextmanager
def nonblocking_file_lock(path: Path, *, conflict_message: str) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise ValidationError(conflict_message) from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


__all__ = ["nonblocking_file_lock"]
