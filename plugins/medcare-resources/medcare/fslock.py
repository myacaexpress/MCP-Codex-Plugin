"""Cross-process safe construction and publication of on-disk caches.

Several MCP server processes (Codex chats, the remote server's workers, test runs) normally share one
per-user cache directory. Rules used everywhere a cache file is created or replaced:

* One builder at a time per dataset: an exclusive advisory lock (``fcntl.flock``) on a per-dataset lock
  file, held for check -> build -> publish. A per-name thread lock makes threads in one process queue too.
  Waiting is bounded (MEDCARE_CACHE_LOCK_TIMEOUT, default 900 s); on timeout a CacheLockTimeout is raised
  so callers report an explicit lookup failure instead of guessing.
* Every builder writes to its own uniquely named temp file (pid + random) in the same directory and
  publishes with an atomic ``os.replace``. Readers therefore only ever see a complete file.
* A builder removes only its own temp file. Leftover temp files of the same dataset are removed only
  while holding that dataset's lock, when no other builder can be running (they belong to a dead process).
* Published SQLite files are never written again (freshness stamps live in a small sidecar JSON written
  atomically), so readers can open them read-only/immutable and a replaced file never changes under them.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:  # POSIX (macOS/Linux, the supported platforms)
    import fcntl
except ImportError:  # pragma: no cover - Windows is unsupported; thread lock only
    fcntl = None

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


class CacheLockTimeout(Exception):
    pass


def _timeout() -> float:
    try:
        return float(os.environ.get("MEDCARE_CACHE_LOCK_TIMEOUT", "900"))
    except ValueError:
        return 900.0


@contextmanager
def dataset_lock(directory: Path, name: str, timeout: float | None = None):
    """Exclusive lock for building/publishing dataset `name` in `directory` (threads and processes)."""
    timeout = _timeout() if timeout is None else timeout
    directory.mkdir(parents=True, exist_ok=True)
    key = str(directory.resolve() / name)
    with _GUARD:
        tlock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout
    if not tlock.acquire(timeout=max(0.0, timeout)):
        raise CacheLockTimeout(f"Timed out waiting for the {name} cache (another build in progress).")
    fd = None
    try:
        if fcntl is not None:
            fd = os.open(str(directory / f".{name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CacheLockTimeout(f"Timed out waiting for the {name} cache "
                                               "(another process is building it).") from None
                    time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        tlock.release()


def temp_path(target: Path) -> Path:
    """A temp file name owned by this process/call, in the target's directory (same filesystem)."""
    return target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def remove_orphan_temps(target_pattern_dir: Path, prefix: str) -> list[str]:
    """Delete leftover temp files for a dataset. Call ONLY while holding that dataset's lock."""
    removed = []
    for p in target_pattern_dir.glob(f".{prefix}*.tmp*"):
        try:
            p.unlink()
            removed.append(p.name)
        except FileNotFoundError:
            pass
    return removed


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".checked.json")


def read_sidecar(path: Path) -> dict:
    try:
        data = json.loads(sidecar(path).read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def write_sidecar(path: Path, **values) -> None:
    data = read_sidecar(path)
    data.update({k: str(v) for k, v in values.items()})
    atomic_write_text(sidecar(path), json.dumps(data))
