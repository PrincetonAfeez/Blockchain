"""Tests for local-network registry path containment."""

from __future__ import annotations

import json
import os

import pytest

from toychain.errors import NodeRuntimeError
from toychain.process import network_status, run_local_network, stop_local_network


def _write_registry(root, payload: dict) -> None:
    (root / "local-network.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def test_network_status_accepts_valid_node_entries(tmp_path):
    root = tmp_path / "network"
    statuses = run_local_network(root, nodes=3, base_port=9900)
    try:
        assert len(statuses) == 3
        assert all(status.running for status in statuses)
        reported = network_status(root)
        assert len(reported) == 3
        assert [status.port for status in reported] == [9900, 9901, 9902]
    finally:
        stop_local_network(root)


def test_network_status_rejects_legacy_data_dir_entries(tmp_path):
    root = tmp_path / "network"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_registry(
        root,
        {
            "nodes": [
                {
                    "data_dir": str(outside),
                    "running": False,
                    "pid": None,
                    "port": 9900,
                    "log_file": str(outside / "node.log"),
                }
            ]
        },
    )
    with pytest.raises(NodeRuntimeError, match="(data_dir paths|JSON schema validation failed)"):
        network_status(root)


def test_network_status_rejects_traversal_name(tmp_path):
    root = tmp_path / "network"
    root.mkdir()
    _write_registry(root, {"nodes": [{"name": "../outside", "port": 9900}]})
    with pytest.raises(NodeRuntimeError, match="(Invalid local network node name|JSON schema validation failed)"):
        network_status(root)


def test_network_status_rejects_invalid_node_name_pattern(tmp_path):
    root = tmp_path / "network"
    root.mkdir()
    _write_registry(root, {"nodes": [{"name": "node0", "port": 9900}]})
    with pytest.raises(NodeRuntimeError, match="(Invalid local network node name|JSON schema validation failed)"):
        network_status(root)


@pytest.mark.skipif(os.name == "nt", reason="symlink containment test is Unix-specific")
def test_network_status_rejects_symlink_escape(tmp_path):
    root = tmp_path / "network"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "node1").symlink_to(outside, target_is_directory=True)
    _write_registry(root, {"nodes": [{"name": "node1", "port": 9900}]})
    with pytest.raises(NodeRuntimeError, match="escapes the network root"):
        network_status(root)


def test_stop_local_rejects_bad_registry_without_touching_sibling(tmp_path, monkeypatch):
    root = tmp_path / "network"
    root.mkdir()
    sibling = tmp_path / "sibling-node"
    sibling.mkdir()
    sibling_pid = sibling / "node.pid"
    sibling_pid.write_text("424242", encoding="ascii")
    sibling_stop = sibling / "node.stop"
    signaled: list[int] = []
    monkeypatch.setattr("toychain.process.os.kill", lambda pid, _sig: signaled.append(pid))

    _write_registry(
        root,
        {
            "nodes": [
                {
                    "data_dir": str(sibling.resolve()),
                    "running": True,
                    "pid": 424242,
                    "port": 9900,
                    "log_file": str(sibling / "node.log"),
                }
            ]
        },
    )

    with pytest.raises(NodeRuntimeError, match="(data_dir paths|JSON schema validation failed)"):
        stop_local_network(root)

    assert signaled == []
    assert sibling_pid.read_text(encoding="ascii") == "424242"
    assert not sibling_stop.exists()


def test_network_cli_stop_local_rejects_escape_with_exit_code_1(tmp_path):
    from toychain.cli import main

    root = tmp_path / "network"
    root.mkdir()
    _write_registry(root, {"nodes": [{"name": "../escape", "port": 9900}]})
    assert main(["--data-dir", str(root), "network", "stop-local"]) == 1
