"""Tests for local-network startup preflight and recovery registry protection."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from toychain.errors import NodeRuntimeError
from toychain.persistence import DataStore, write_json
from toychain.process import (
    dismiss_local_network_recovery,
    node_status,
    run_local_network,
    start_node_with_handle,
    stop_local_network,
)


def _write_registry(root, payload: dict) -> None:
    write_json(root / "local-network.json", payload)


def test_rerun_preserves_active_network_registry(tmp_path):
    root = tmp_path / "network"
    run_local_network(root, nodes=2, base_port=9900)
    original = (root / "local-network.json").read_bytes()
    try:
        with pytest.raises(NodeRuntimeError, match="registry already exists"):
            run_local_network(root, nodes=2, base_port=9900)
        assert (root / "local-network.json").read_bytes() == original
        assert not (root / "local-network.starting.json").exists()
        assert list(root.glob("local-network.starting.*.json")) == []
    finally:
        stop_local_network(root)


def test_rerun_preserves_existing_recovery_registry_byte_for_byte(tmp_path):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    recovery_path = root / "local-network.starting.json"
    payload = (
        b'{\n'
        b'  "nodes": [\n'
        b'    {\n'
        b'      "name": "node1",\n'
        b'      "pid": 424242,\n'
        b'      "state": "live_unverified"\n'
        b'    }\n'
        b'  ],\n'
        b'  "startup_error": "prior failure"\n'
        b'}\n'
    )
    recovery_path.write_bytes(payload)

    with pytest.raises(NodeRuntimeError, match="recovery registry"):
        run_local_network(root, nodes=1, base_port=9900)

    assert recovery_path.read_bytes() == payload
    assert not (root / "local-network.json").exists()
    assert list(root.glob("local-network.starting.*.json")) == []


def test_live_unverified_node_does_not_delete_existing_final_registry(tmp_path, monkeypatch):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    _write_registry(root, {"nodes": [{"name": "node1", "port": 9900}]})
    original = (root / "local-network.json").read_bytes()

    real_status = node_status

    def fake_status(data_dir):
        status = real_status(data_dir)
        if str(data_dir).endswith("node1"):
            return replace(
                status,
                running=False,
                verified=False,
                state="live_unverified",
                pid=424242,
                message="simulated unverified",
            )
        return status

    monkeypatch.setattr("toychain.process.node_status", fake_status)

    with pytest.raises(NodeRuntimeError, match="registry already exists"):
        run_local_network(root, nodes=1, base_port=9900)

    assert (root / "local-network.json").read_bytes() == original


def test_live_unverified_node_does_not_delete_existing_recovery_registry(tmp_path, monkeypatch):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    recovery_path = root / "local-network.starting.json"
    payload = b'{"nodes": [{"name": "node1", "pid": 424242}], "startup_error": "keep"}\n'
    recovery_path.write_bytes(payload)

    real_status = node_status

    def fake_status(data_dir):
        status = real_status(data_dir)
        if str(data_dir).endswith("node1"):
            return replace(
                status,
                running=False,
                verified=False,
                state="live_unverified",
                pid=424242,
            )
        return status

    monkeypatch.setattr("toychain.process.node_status", fake_status)

    with pytest.raises(NodeRuntimeError, match="recovery registry"):
        run_local_network(root, nodes=1, base_port=9900)

    assert recovery_path.read_bytes() == payload


def test_malformed_node_rejected_before_any_registry_write(tmp_path):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    store = DataStore(root / "node1")
    store.initialize()
    store.config_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(NodeRuntimeError, match="malformed"):
        run_local_network(root, nodes=1, base_port=9900)

    assert not (root / "local-network.json").exists()
    assert not (root / "local-network.starting.json").exists()
    assert list(root.glob("local-network.starting.*.json")) == []


def test_running_verified_node_rejected_before_any_registry_write(tmp_path):
    root = tmp_path / "network"
    started = start_node_with_handle(root / "node1", 9900, node_name="node1")
    try:
        with pytest.raises(NodeRuntimeError, match="running_verified"):
            run_local_network(root, nodes=1, base_port=9900)
        assert not (root / "local-network.json").exists()
        assert not (root / "local-network.starting.json").exists()
        assert list(root.glob("local-network.starting.*.json")) == []
    finally:
        started.popen.terminate()
        started.popen.wait(timeout=10)


def test_dismiss_stale_recovery_registry(tmp_path):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    recovery = root / "local-network.starting.json"
    write_json(
        recovery,
        {
            "nodes": [{"name": "node1", "pid": 999999, "state": "stopped"}],
            "startup_error": "old failure",
        },
    )

    dismiss_local_network_recovery(root)

    assert not recovery.exists()


def test_dismiss_stale_orphan_attempt_registry(tmp_path):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    orphan = root / "local-network.starting.00000000-0000-4000-8000-000000000099.json"
    write_json(
        orphan,
        {"nodes": [{"name": "node1", "pid": 999999}]},
    )

    dismiss_local_network_recovery(root)

    assert not orphan.exists()


def test_dismiss_refuses_when_recovery_pid_is_live(tmp_path, monkeypatch):
    root = tmp_path / "network"
    root.mkdir(parents=True)
    recovery = root / "local-network.starting.json"
    write_json(recovery, {"nodes": [{"name": "node1", "pid": 12345}]})
    monkeypatch.setattr("toychain.process.process_is_running", lambda pid: pid == 12345)

    with pytest.raises(NodeRuntimeError, match="live PIDs remain"):
        dismiss_local_network_recovery(root)

    assert recovery.exists()


def test_network_cli_dismiss_recovery(tmp_path):
    from toychain.cli import main

    root = tmp_path / "network"
    root.mkdir(parents=True)
    write_json(
        root / "local-network.starting.json",
        {"nodes": [{"name": "node1", "pid": 999999}]},
    )

    assert main(["--data-dir", str(root), "network", "dismiss-recovery"]) == 0
    assert not (root / "local-network.starting.json").exists()
