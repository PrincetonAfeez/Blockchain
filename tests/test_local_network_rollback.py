"""Tests for local-network rollback and recovery registry preservation."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.kill_helpers import kill_and_reap_pid

from toychain.errors import NodeRuntimeError
from toychain.persistence import DataStore
from toychain.process import (
    ProcessStatus,
    _rollback_started_children,
    _terminate_subprocess as real_terminate_subprocess,
    node_status,
    process_is_running,
    run_local_network,
    start_node_with_handle,
    stop_node,
)


def _flaky_two_node_start(monkeypatch, *, fail_on: int = 2):
    real_start = start_node_with_handle
    calls = {"count": 0}

    def flaky_start(data_dir, port=0, *, node_name=None):
        calls["count"] += 1
        if calls["count"] == fail_on:
            raise NodeRuntimeError("simulated later startup failure")
        return real_start(data_dir, port=port, node_name=node_name)

    monkeypatch.setattr("toychain.process.start_node_with_handle", flaky_start)


def test_rollback_preserves_registry_when_stop_refused_and_pid_live(tmp_path, monkeypatch):
    root = tmp_path / "network"
    _flaky_two_node_start(monkeypatch)
    monkeypatch.setattr(
        "toychain.process.stop_node",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NodeRuntimeError("Refusing to signal PID because it is not verified")
        ),
    )
    monkeypatch.setattr("toychain.process._terminate_subprocess", lambda *_a, **_k: False)

    with pytest.raises(NodeRuntimeError, match="recovery registry preserved"):
        run_local_network(root, nodes=2, base_port=9960)

    assert (root / "local-network.starting.json").exists()
    assert not (root / "local-network.json").exists()
    recovery = json.loads((root / "local-network.starting.json").read_text(encoding="utf-8"))
    assert recovery["nodes"][0]["pid"] is not None
    assert recovery["nodes"][0]["name"] == "node1"
    assert recovery["startup_error"] == "simulated later startup failure"

    live_pid = recovery["nodes"][0]["pid"]
    if live_pid is not None and process_is_running(live_pid):
        kill_and_reap_pid(live_pid)


def test_rollback_preserves_registry_for_live_unverified_status(tmp_path, monkeypatch):
    root = tmp_path / "network"
    started = start_node_with_handle(root / "node1", 9961, node_name="node1")
    try:
        real_status = node_status

        def fake_status(data_dir):
            status = real_status(data_dir)
            if str(data_dir).endswith("node1"):
                return replace(
                    status,
                    running=False,
                    verified=False,
                    state="live_unverified",
                    message="simulated unverified",
                )
            return status

        monkeypatch.setattr("toychain.process.node_status", fake_status)
        monkeypatch.setattr(
            "toychain.process.stop_node",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                NodeRuntimeError("Refusing to signal PID because it is not verified")
            ),
        )
        monkeypatch.setattr("toychain.process._terminate_subprocess", lambda *_a, **_k: False)

        starting_registry = root / "local-network.starting.json"
        attempt_registry = root / "local-network.starting.test-attempt.json"
        planned = [
            {
                "name": "node1",
                "port": 9961,
                "instance_id": started.instance_id,
                "pid": started.status.pid,
            }
        ]
        with pytest.raises(NodeRuntimeError, match="recovery registry preserved"):
            _rollback_started_children(
                [started],
                attempt_registry=attempt_registry,
                recovery_registry=starting_registry,
                planned_nodes=planned,
                original_error=NodeRuntimeError("simulated failure"),
            )

        recovery = json.loads(starting_registry.read_text(encoding="utf-8"))
        assert recovery["nodes"][0]["state"] == "live_unverified"
        assert recovery["nodes"][0]["pid"] == started.status.pid
    finally:
        store = DataStore(started.data_dir)
        real_terminate_subprocess(started.popen, store)
        started.popen.wait(timeout=10)


def test_rollback_preserves_registry_for_malformed_live_pid(tmp_path, monkeypatch):
    root = tmp_path / "network"
    started = start_node_with_handle(root / "node1", 9962, node_name="node1")
    try:
        real_status = node_status

        def fake_status(data_dir):
            status = real_status(data_dir)
            if str(data_dir).endswith("node1"):
                return replace(
                    status,
                    running=False,
                    verified=False,
                    state="malformed",
                    message="simulated malformed lifecycle",
                )
            return status

        monkeypatch.setattr("toychain.process.node_status", fake_status)
        monkeypatch.setattr("toychain.process._terminate_subprocess", lambda *_a, **_k: False)

        starting_registry = root / "local-network.starting.json"
        attempt_registry = root / "local-network.starting.test-attempt.json"
        with pytest.raises(NodeRuntimeError, match="recovery registry preserved"):
            _rollback_started_children(
                [started],
                attempt_registry=attempt_registry,
                recovery_registry=starting_registry,
                planned_nodes=[{"name": "node1", "port": 9962}],
                original_error=NodeRuntimeError("simulated failure"),
            )

        recovery = json.loads(starting_registry.read_text(encoding="utf-8"))
        assert recovery["nodes"][0]["state"] == "malformed"
        assert recovery["nodes"][0]["pid"] == started.status.pid
    finally:
        store = DataStore(started.data_dir)
        real_terminate_subprocess(started.popen, store)
        started.popen.wait(timeout=10)


def test_rollback_removes_temporary_registry_when_pid_already_dead(tmp_path, monkeypatch):
    root = tmp_path / "network"
    _flaky_two_node_start(monkeypatch)
    monkeypatch.setattr(
        "toychain.process.stop_node",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NodeRuntimeError("Refusing to signal PID because it is not verified")
        ),
    )

    with pytest.raises(NodeRuntimeError, match="simulated later startup failure"):
        run_local_network(root, nodes=2, base_port=9963)

    assert not (root / "local-network.json").exists()
    assert not (root / "local-network.starting.json").exists()
    assert not DataStore(root / "node1").pid_path.exists()


def test_rollback_terminates_child_via_parent_popen_handle(tmp_path, monkeypatch):
    root = tmp_path / "network"
    _flaky_two_node_start(monkeypatch)
    terminated: list[int] = []
    real_terminate = __import__(
        "toychain.process", fromlist=["_terminate_subprocess"]
    )._terminate_subprocess

    def tracking_terminate(popen, store=None, **kwargs):
        terminated.append(popen.pid)
        return real_terminate(popen, store, **kwargs)

    monkeypatch.setattr(
        "toychain.process.stop_node",
        lambda *_a, **_k: (_ for _ in ()).throw(NodeRuntimeError("refused")),
    )
    monkeypatch.setattr("toychain.process._terminate_subprocess", tracking_terminate)

    with pytest.raises(NodeRuntimeError, match="simulated later startup failure"):
        run_local_network(root, nodes=2, base_port=9964)

    assert terminated
    assert not DataStore(root / "node1").pid_path.exists()
    assert not (root / "local-network.starting.json").exists()


def test_successful_rollback_cleans_temporary_registry(tmp_path, monkeypatch):
    root = tmp_path / "network"
    _flaky_two_node_start(monkeypatch)

    with pytest.raises(NodeRuntimeError, match="simulated later startup failure"):
        run_local_network(root, nodes=2, base_port=9965)

    assert not (root / "local-network.json").exists()
    assert not (root / "local-network.starting.json").exists()
    assert not DataStore(root / "node1").pid_path.exists()


def test_rollback_preserves_registry_when_child_survives_termination(tmp_path, monkeypatch):
    root = tmp_path / "network"
    _flaky_two_node_start(monkeypatch)
    monkeypatch.setattr(
        "toychain.process.stop_node",
        lambda *_a, **_k: (_ for _ in ()).throw(NodeRuntimeError("refused")),
    )
    monkeypatch.setattr("toychain.process._terminate_subprocess", lambda *_a, **_k: False)
    monkeypatch.setattr("toychain.process._wait_for_child_exit", lambda *_a, **_k: False)

    with pytest.raises(NodeRuntimeError, match="recovery registry preserved"):
        run_local_network(root, nodes=2, base_port=9966)

    recovery = json.loads((root / "local-network.starting.json").read_text(encoding="utf-8"))
    node_entry = recovery["nodes"][0]
    assert node_entry["name"] == "node1"
    assert node_entry["port"] == 9966
    assert node_entry["instance_id"] is not None
    assert node_entry["pid"] is not None
    assert not (root / "local-network.json").exists()

    stop_node(root / "node1", timeout=8)


def test_process_status_pid_is_live_distinguishes_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr("toychain.process.process_is_running", lambda _pid: False)
    status = ProcessStatus(
        data_dir=str(tmp_path),
        running=False,
        pid=424242,
        port=None,
        log_file=str(tmp_path / "node.log"),
        verified=False,
        state="live_unverified",
    )
    assert status.running is False
    assert status.pid_is_live is False

    monkeypatch.setattr("toychain.process.process_is_running", lambda _pid: True)
    assert ProcessStatus(
        data_dir=str(tmp_path),
        running=False,
        pid=424242,
        port=None,
        log_file=str(tmp_path / "node.log"),
        verified=False,
        state="live_unverified",
    ).pid_is_live is True
