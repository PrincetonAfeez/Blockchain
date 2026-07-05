"""Tests for safe node stop identity verification."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from toychain.errors import NodeRuntimeError
from toychain.persistence import DataStore
from toychain.process import start_node, stop_node
from toychain.process_identity import (
    NodeLifecycle,
    cleanup_stale_node_files,
    verify_process_identity,
    write_lifecycle,
)


def _write_lifecycle(store: DataStore, *, pid: int, instance_id: str = "test-instance") -> None:
    write_lifecycle(
        store.lifecycle_path,
        NodeLifecycle(
            schema_version=1,
            pid=pid,
            instance_id=instance_id,
            started_at=1,
            data_dir=str(store.data_dir),
            executable=os.path.normcase(str(os.path.realpath(sys.executable))),
        ),
    )


def test_stop_node_refuses_unrelated_live_pid(tmp_path):
    store = DataStore(tmp_path)
    store.initialize()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.pid_path.write_text(str(sleeper.pid), encoding="ascii")
        _write_lifecycle(store, pid=sleeper.pid)
        with pytest.raises(NodeRuntimeError, match="not verified as this Toychain node"):
            stop_node(store.data_dir)
        assert store.pid_path.exists()
    finally:
        sleeper.terminate()
        sleeper.wait()


def test_stop_node_cleans_dead_pid(tmp_path):
    store = DataStore(tmp_path)
    store.initialize()
    store.pid_path.write_text("424242", encoding="ascii")
    store.lock_path.write_text("", encoding="ascii")
    store.stop_path.write_text("stop\n", encoding="ascii")
    _write_lifecycle(store, pid=424242)

    stop_node(store.data_dir)
    assert not store.pid_path.exists()
    assert not store.lock_path.exists()
    assert not store.stop_path.exists()
    assert not store.lifecycle_path.exists()


def test_cleanup_stale_refuses_live_unverified_pid(tmp_path):
    store = DataStore(tmp_path)
    store.initialize()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.pid_path.write_text(str(sleeper.pid), encoding="ascii")
        with pytest.raises(NodeRuntimeError, match="Refusing to clean stale files"):
            cleanup_stale_node_files(store)
    finally:
        sleeper.terminate()
        sleeper.wait()


def test_start_and_stop_verified_node(tmp_path):
    started = start_node(tmp_path, port=9911)
    try:
        assert started.running
        store = DataStore(tmp_path)
        assert "instance_id" in store.lifecycle_path.read_text(encoding="utf-8")
        stopped = stop_node(tmp_path)
        assert not stopped.running
    except NodeRuntimeError:
        stop_node(tmp_path)
        raise


def test_verify_process_identity_accepts_matching_lifecycle(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    store.initialize()
    pid = os.getpid()
    lifecycle = NodeLifecycle(
        schema_version=1,
        pid=pid,
        instance_id="abc",
        started_at=1,
        data_dir=str(store.data_dir),
        executable=os.path.normcase(str(os.path.realpath(sys.executable))),
    )
    write_lifecycle(store.lifecycle_path, lifecycle)
    monkeypatch.setattr(
        "toychain.process_identity._linux_command_line",
        lambda _pid: f"toychain --data-dir {store.data_dir} _node-run",
    )
    monkeypatch.setattr("toychain.process_identity.os.name", "posix")
    verify_process_identity(pid, lifecycle, store)


def test_node_cleanup_stale_cli(tmp_path):
    from toychain.cli import main

    store = DataStore(tmp_path)
    store.initialize()
    store.pid_path.write_text("424242", encoding="ascii")
    assert main(["--data-dir", str(tmp_path), "node", "cleanup-stale"]) == 0
    assert not store.pid_path.exists()
