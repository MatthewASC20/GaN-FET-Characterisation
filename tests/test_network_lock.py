from __future__ import annotations

import os
import socket

from gan_fet.storage import network_lock
from gan_fet.storage.db import Database


def test_database_instances_share_one_process_lease(tmp_path):
    project = tmp_path / "project"
    first = Database(project / "one.db")
    second = Database(project / "two.db")
    try:
        assert first._net_lock.ownership_token == second._net_lock.ownership_token
        assert first._net_lock.is_acquired
        assert second._net_lock.is_acquired
    finally:
        second.close()
        first.close()

    assert not (tmp_path / "project.lock").exists()


def test_permission_error_is_not_treated_as_dead_process(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda _pid, _signal: (_ for _ in ()).throw(PermissionError()))

    assert not network_lock._is_local_pid_dead(socket.gethostname(), 999_999)


def test_missing_process_is_detected(monkeypatch):
    monkeypatch.setattr(
        os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert network_lock._is_local_pid_dead(socket.gethostname(), 999_999)
