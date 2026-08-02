"""Fail-closed lease locking for a project directory on a shared filesystem.

The sidecar is created with ``O_EXCL`` so contenders cannot both acquire an
absent lock. A process-local registry reference-counts multiple Database
instances using the same project directory. Every lease has an unguessable
token; heartbeat and release only modify a sidecar that still carries it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

log = logging.getLogger(__name__)

LOCK_HEARTBEAT_INTERVAL_S = 30.0
LOCK_STALE_THRESHOLD_S = 120.0


class ConcurrentAccessError(RuntimeError):
    """Raised when the project lease is owned elsewhere or cannot be verified."""


@dataclass
class _SharedLease:
    lock_file: Path
    pid: int
    host: str
    token: str
    editing_since: str
    refs: int = 1
    stop: threading.Event = field(default_factory=threading.Event)
    io_lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None
    lost_reason: str | None = None


def _is_local_pid_dead(host: str, pid: Any) -> bool:
    """Return True if host is this workstation and pid is a dead process."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    if host != socket.gethostname():
        return False
    try:
        import ctypes
        windll = getattr(ctypes, "windll", None)
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is not None:
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return False
            return True
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        # Lack of permission or an unknown platform error is not evidence that
        # the owning process is gone. Acquisition is intentionally fail-closed.
        return False


class NetworkProjectLock:
    """Advisory lease for one shared project directory.

    Atomic creation supplies exclusion. The heartbeat is a liveness lease, not
    the acquisition primitive. Any uncertainty is treated as a conflict.
    """

    _registry_lock: ClassVar[threading.RLock] = threading.RLock()
    _registry: ClassVar[dict[str, _SharedLease]] = {}

    def __init__(self, target_dir: Path):
        self.target_dir = Path(target_dir).resolve()
        self.lock_file = self.target_dir.parent / f"{self.target_dir.name}.lock"
        self._pid = os.getpid()
        self._host = socket.gethostname()
        self._editing_since = datetime.now(timezone.utc).isoformat()
        self._lock = threading.RLock()
        self._lease: _SharedLease | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self.is_acquired = False

    @property
    def ownership_token(self) -> str | None:
        return self._lease.token if self._lease is not None else None

    def acquire(self) -> bool:
        """Acquire the lease or raise; acquisition never degrades to a warning."""
        with self._lock, self._registry_lock:
            if self.is_acquired:
                return True

            key = os.fspath(self.lock_file)
            shared = self._registry.get(key)
            if shared is not None:
                # A fork inherits Python memory but not the parent's live thread.
                if shared.pid == os.getpid() and shared.host == self._host:
                    if shared.lost_reason is not None:
                        raise ConcurrentAccessError(
                            f"Project lock ownership is no longer valid: "
                            f"{shared.lost_reason}"
                        )
                    try:
                        with shared.io_lock:
                            current = self._read_path_info(
                                shared.lock_file, strict=True
                            )
                            if current.get("token") != shared.token:
                                raise ConcurrentAccessError(
                                    "sidecar token no longer matches"
                                )
                    except Exception as exc:
                        shared.lost_reason = str(exc)
                        raise ConcurrentAccessError(
                            f"Project lock ownership is no longer valid: {exc}"
                        ) from exc
                    shared.refs += 1
                    self._attach(shared)
                    return True
                self._registry.pop(key, None)

            lease = _SharedLease(
                lock_file=self.lock_file,
                pid=self._pid,
                host=self._host,
                token=uuid.uuid4().hex,
                editing_since=self._editing_since,
            )
            self._acquire_sidecar(lease)
            try:
                lease.thread = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(lease,),
                    daemon=True,
                    name="network-lock-heartbeat",
                )
                lease.thread.start()
                self._registry[key] = lease
                self._attach(lease)
            except Exception:
                self._remove_if_owned(lease)
                raise
            return True

    def _attach(self, lease: _SharedLease) -> None:
        self._lease = lease
        self._heartbeat_thread = lease.thread
        self.is_acquired = True

    def _acquire_sidecar(self, lease: _SharedLease) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(
                    self.lock_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                info = self._read_lock_info(strict=True)
                if info is None:  # strict reads never return None; narrows typing.
                    raise ConcurrentAccessError(
                        f"Existing project lock disappeared at {self.lock_file}"
                    )
                if _is_local_pid_dead(str(info.get("host", "")), info.get("pid")):
                    raise ConcurrentAccessError(
                        f"{self._conflict_message(info)} The local owner is no "
                        "longer running, but the lease was not removed "
                        "automatically because another process could acquire "
                        "the path during cleanup. Verify the process is stopped, "
                        f"then remove the lock file manually: {self.lock_file}"
                    )
                if self._is_stale(info):
                    raise ConcurrentAccessError(
                        f"{self._conflict_message(info)} The lease appears stale, "
                        "but it was not removed automatically. Verify that the "
                        "owning process is stopped, then remove the lock file "
                        f"manually: {self.lock_file}"
                    )
                raise ConcurrentAccessError(self._conflict_message(info))
            except OSError as exc:
                raise ConcurrentAccessError(
                    f"Project lock could not be created at {self.lock_file}: {exc}"
                ) from exc

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._payload(lease), handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                self.lock_file.unlink(missing_ok=True)
                raise
            return

    @staticmethod
    def _payload(lease: _SharedLease) -> dict[str, Any]:
        return {
            "pid": lease.pid,
            "host": lease.host,
            "token": lease.token,
            "editing_since": lease.editing_since,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "heartbeat_timestamp": time.time(),
        }

    @staticmethod
    def _conflict_message(info: dict[str, Any]) -> str:
        return (
            "Project directory is currently locked by workstation "
            f"'{info.get('host', 'UNKNOWN')}' "
            f"(PID: {info.get('pid')}, Editing Since: {info.get('editing_since')})."
        )

    def heartbeat(self) -> None:
        """Refresh this process's shared lease, failing if ownership was lost."""
        with self._lock:
            if not self.is_acquired or self._lease is None:
                return
            try:
                self._refresh_lease(self._lease)
            except Exception as exc:
                self._mark_lost(self._lease, exc)
                raise

    def assert_owned(self) -> None:
        """Synchronously verify the sidecar token before a database write."""
        with self._lock:
            if not self.is_acquired or self._lease is None:
                raise ConcurrentAccessError("Project lock is not acquired")
            lease = self._lease
            if lease.lost_reason is not None:
                raise ConcurrentAccessError(
                    f"Project lock ownership is no longer valid: "
                    f"{lease.lost_reason}"
                )
            with lease.io_lock:
                try:
                    current = self._read_path_info(lease.lock_file, strict=True)
                    if current.get("token") != lease.token:
                        raise ConcurrentAccessError(
                            f"Project lock ownership was lost for {lease.lock_file}"
                        )
                except Exception as exc:
                    if lease.lost_reason is None:
                        lease.lost_reason = str(exc)
                    raise ConcurrentAccessError(
                        f"Project lock ownership is no longer valid: {exc}"
                    ) from exc

    @classmethod
    def _refresh_lease(cls, lease: _SharedLease) -> None:
        with lease.io_lock:
            try:
                # Keep the validated inode open while refreshing it. If another
                # host expires/unlinks this lease concurrently, this write stays
                # on the old unlinked inode and cannot replace the new owner's
                # sidecar.
                with lease.lock_file.open("r+", encoding="utf-8") as handle:
                    info = json.load(handle)
                    if not isinstance(info, dict) or info.get("token") != lease.token:
                        raise ConcurrentAccessError(
                            f"Project lock ownership was lost for {lease.lock_file}"
                        )
                    handle.seek(0)
                    handle.truncate()
                    json.dump(cls._payload(lease), handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, json.JSONDecodeError) as exc:
                raise ConcurrentAccessError(
                    f"Project lock heartbeat could not refresh {lease.lock_file}: {exc}"
                ) from exc

            current = cls._read_path_info(lease.lock_file, strict=True)
            if current.get("token") != lease.token:
                raise ConcurrentAccessError(
                    f"Project lock ownership was lost for {lease.lock_file}"
                )

    def release(self) -> None:
        """Release one local reference; the final reference removes the lease."""
        lease: _SharedLease | None = None
        final_reference = False
        with self._lock, self._registry_lock:
            if not self.is_acquired or self._lease is None:
                return
            lease = self._lease
            lease.refs -= 1
            self.is_acquired = False
            self._lease = None
            self._heartbeat_thread = None
            if lease.refs == 0:
                self._registry.pop(os.fspath(lease.lock_file), None)
                lease.stop.set()
                final_reference = True

        if not final_reference or lease is None:
            return
        if lease.thread is not None and lease.thread is not threading.current_thread():
            lease.thread.join(timeout=5.0)
        self._remove_if_owned(lease)

    def _remove_if_owned(self, lease: _SharedLease) -> None:
        with lease.io_lock:
            try:
                info = self._read_path_info(lease.lock_file, strict=True)
            except FileNotFoundError:
                return
            except ConcurrentAccessError as exc:
                log.warning("Could not verify lock during release: %s", exc)
                return
            if info.get("token") != lease.token:
                log.warning("Not removing project lock now owned by another lease")
                return
            try:
                lease.lock_file.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                log.warning("Could not remove lock file %s: %s", lease.lock_file, exc)

    def _read_lock_info(self, *, strict: bool = False) -> dict[str, Any] | None:
        try:
            return self._read_path_info(self.lock_file, strict=True)
        except FileNotFoundError:
            if strict:
                raise ConcurrentAccessError(
                    f"Project lock disappeared while reading {self.lock_file}"
                )
            return None
        except ConcurrentAccessError:
            if strict:
                raise
            return None

    @staticmethod
    def _read_path_info(path: Path, *, strict: bool) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            if strict:
                raise ConcurrentAccessError(
                    f"Existing project lock at {path} cannot be verified: {exc}"
                ) from exc
            return {}
        if not isinstance(data, dict):
            raise ConcurrentAccessError(
                f"Existing project lock at {path} has an invalid payload"
            )
        return data

    @staticmethod
    def _is_stale(info: dict[str, Any]) -> bool:
        try:
            timestamp = float(info["heartbeat_timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConcurrentAccessError(
                f"Existing project lock has no valid lease timestamp: {exc}"
            ) from exc
        token = info.get("token")
        if token is not None and (not isinstance(token, str) or not token):
            raise ConcurrentAccessError("Existing project lock has an invalid token")
        return (time.time() - timestamp) > LOCK_STALE_THRESHOLD_S

    @classmethod
    def _heartbeat_loop(cls, lease: _SharedLease) -> None:
        while not lease.stop.wait(LOCK_HEARTBEAT_INTERVAL_S):
            try:
                cls._refresh_lease(lease)
            except Exception as exc:
                log.error("Project lock heartbeat failed; ownership is uncertain: %s", exc)
                cls._mark_lost(lease, exc)
                lease.stop.set()
                return

    @staticmethod
    def _mark_lost(lease: _SharedLease, exc: Exception) -> None:
        with lease.io_lock:
            if lease.lost_reason is None:
                lease.lost_reason = str(exc)
