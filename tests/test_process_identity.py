"""Tests for safe node stop identity verification."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from toychain.errors import NodeRuntimeError
from toychain.node import Node
from toychain.persistence import DataStore
from toychain.process import node_status, start_node, stop_node
from toychain.process_identity import (
    NodeLifecycle,
    cleanup_stale_node_files,
    process_is_running,
    read_process_start_token,
    verify_process_identity,
    write_lifecycle,
)


TEST_INSTANCE_ID = "00000000-0000-4000-8000-000000000099"


def _write_lifecycle(
    store: DataStore,
    *,
    pid: int,
    instance_id: str = TEST_INSTANCE_ID,
    process_start_token: int | None = None,
) -> None:
    write_lifecycle(
        store.lifecycle_path,
        NodeLifecycle(
            schema_version=1,
            pid=pid,
            instance_id=instance_id,
            started_at=1,
            process_start_token=(
                process_start_token
                if process_start_token is not None
                else read_process_start_token(pid)
            ),
            data_dir=str(store.data_dir),
            executable=os.path.normcase(str(os.path.realpath(sys.executable))),
        ),
    )


def _mock_posix_identity(monkeypatch, store: DataStore, lifecycle: NodeLifecycle) -> None:
    monkeypatch.setattr("toychain.process_identity.os.name", "posix")
    monkeypatch.setattr(
        "toychain.process_identity._linux_executable",
        lambda _pid: lifecycle.executable,
    )
    monkeypatch.setattr(
        "toychain.process_identity._linux_command_line",
        lambda _pid: (
            f"python -m toychain --data-dir {store.data_dir} _node-run "
            f"--instance-id {lifecycle.instance_id}"
        ),
    )
    monkeypatch.setattr(
        "toychain.process_identity._linux_process_start_time",
        lambda _pid: lifecycle.process_start_token,
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
    _write_lifecycle(store, pid=424242, process_start_token=0)

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


def test_cleanup_stale_refuses_healthy_verified_node(tmp_path):
    started = start_node(tmp_path, port=9912)
    store = DataStore(tmp_path)
    try:
        assert started.running
        with pytest.raises(
            NodeRuntimeError,
            match="Refusing to clean lifecycle files for active Toychain node",
        ):
            cleanup_stale_node_files(store)
        assert store.pid_path.exists()
        assert store.lock_path.exists()
        assert store.lifecycle_path.exists()
        assert store.ready_path.exists()
        assert node_status(tmp_path).running
        with pytest.raises(NodeRuntimeError, match="owned by a running process"):
            Node.open(tmp_path)
    finally:
        stop_node(tmp_path, timeout=8)


def test_cleanup_stale_dangerous_does_not_remove_verified_live_node(tmp_path):
    started = start_node(tmp_path, port=9913)
    store = DataStore(tmp_path)
    try:
        assert started.running
        with pytest.raises(
            NodeRuntimeError,
            match="Refusing to clean lifecycle files for active Toychain node",
        ):
            cleanup_stale_node_files(store, force=True)
        assert store.lock_path.exists()
    finally:
        stop_node(tmp_path, timeout=8)


def test_start_and_stop_verified_node(tmp_path):
    started = start_node(tmp_path, port=9911)
    try:
        assert started.running
        store = DataStore(tmp_path)
        assert "instance_id" in store.lifecycle_path.read_text(encoding="utf-8")
        assert "process_start_token" in store.lifecycle_path.read_text(encoding="utf-8")
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
        instance_id=TEST_INSTANCE_ID,
        started_at=1,
        process_start_token=read_process_start_token(pid),
        data_dir=str(store.data_dir),
        executable=os.path.normcase(str(os.path.realpath(sys.executable))),
    )
    write_lifecycle(store.lifecycle_path, lifecycle)
    _mock_posix_identity(monkeypatch, store, lifecycle)
    verify_process_identity(pid, lifecycle, store)


def test_verify_process_identity_rejects_wrong_command_line(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    pid = os.getpid()
    lifecycle = NodeLifecycle(
        schema_version=1,
        pid=pid,
        instance_id=TEST_INSTANCE_ID,
        started_at=1,
        process_start_token=read_process_start_token(pid),
        data_dir=str(store.data_dir),
        executable=os.path.normcase(str(os.path.realpath(sys.executable))),
    )
    _mock_posix_identity(monkeypatch, store, lifecycle)
    monkeypatch.setattr(
        "toychain.process_identity._linux_command_line",
        lambda _pid: f"python -m toychain --data-dir {store.data_dir} _node-run",
    )
    with pytest.raises(NodeRuntimeError, match="instance_id"):
        verify_process_identity(pid, lifecycle, store)


def test_verify_process_identity_rejects_wrong_start_token(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    pid = os.getpid()
    lifecycle = NodeLifecycle(
        schema_version=1,
        pid=pid,
        instance_id=TEST_INSTANCE_ID,
        started_at=1,
        process_start_token=read_process_start_token(pid),
        data_dir=str(store.data_dir),
        executable=os.path.normcase(str(os.path.realpath(sys.executable))),
    )
    _mock_posix_identity(monkeypatch, store, lifecycle)
    monkeypatch.setattr(
        "toychain.process_identity._linux_process_start_time",
        lambda _pid: lifecycle.process_start_token + 1,
    )
    with pytest.raises(NodeRuntimeError, match="start token"):
        verify_process_identity(pid, lifecycle, store)


def test_verify_process_identity_rejects_missing_command_line(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    pid = os.getpid()
    lifecycle = NodeLifecycle(
        schema_version=1,
        pid=pid,
        instance_id=TEST_INSTANCE_ID,
        started_at=1,
        process_start_token=read_process_start_token(pid),
        data_dir=str(store.data_dir),
        executable=os.path.normcase(str(os.path.realpath(sys.executable))),
    )
    monkeypatch.setattr("toychain.process_identity.os.name", "posix")
    monkeypatch.setattr(
        "toychain.process_identity._linux_executable",
        lambda _pid: lifecycle.executable,
    )
    monkeypatch.setattr("toychain.process_identity._linux_command_line", lambda _pid: None)
    monkeypatch.setattr(
        "toychain.process_identity._linux_process_start_time",
        lambda _pid: lifecycle.process_start_token,
    )
    with pytest.raises(NodeRuntimeError, match="Process identity could not be verified"):
        verify_process_identity(pid, lifecycle, store)


def test_verify_process_identity_rejects_missing_executable(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    pid = os.getpid()
    lifecycle = NodeLifecycle(
        schema_version=1,
        pid=pid,
        instance_id=TEST_INSTANCE_ID,
        started_at=1,
        process_start_token=read_process_start_token(pid),
        data_dir=str(store.data_dir),
        executable=os.path.normcase(str(os.path.realpath(sys.executable))),
    )
    monkeypatch.setattr("toychain.process_identity.os.name", "posix")
    monkeypatch.setattr("toychain.process_identity._linux_executable", lambda _pid: None)
    monkeypatch.setattr(
        "toychain.process_identity._linux_command_line",
        lambda _pid: (
            f"python -m toychain --data-dir {store.data_dir} _node-run "
            f"--instance-id {lifecycle.instance_id}"
        ),
    )
    monkeypatch.setattr(
        "toychain.process_identity._linux_process_start_time",
        lambda _pid: lifecycle.process_start_token,
    )
    with pytest.raises(NodeRuntimeError, match="Process identity could not be verified"):
        verify_process_identity(pid, lifecycle, store)


def test_process_is_running_treats_proc_zombie_state_as_not_running(monkeypatch) -> None:
    from pathlib import Path as RealPath

    monkeypatch.setattr("toychain.process_identity.os.name", "posix")

    class _ZombieStatPath:
        def read_text(self, encoding: str = "ascii") -> str:
            return "42 (python) Z 0 0 0 0 0 0 0 0"

    def fake_path(value: str | RealPath) -> RealPath | _ZombieStatPath:
        if str(value) == "/proc/42/stat":
            return _ZombieStatPath()
        return RealPath(value)

    monkeypatch.setattr("toychain.process_identity.Path", fake_path)
    monkeypatch.setattr(
        "toychain.process_identity.os.kill",
        lambda _pid, _sig: (_ for _ in ()).throw(AssertionError("kill must not run")),
    )
    assert process_is_running(42) is False


def test_node_cleanup_stale_cli(tmp_path):
    from toychain.cli import main

    store = DataStore(tmp_path)
    store.initialize()
    store.pid_path.write_text("424242", encoding="ascii")
    assert main(["--data-dir", str(tmp_path), "node", "cleanup-stale"]) == 0
    assert not store.pid_path.exists()
