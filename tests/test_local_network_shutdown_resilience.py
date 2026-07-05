"""Tests for resilient local-network shutdown with malformed node configs."""

from __future__ import annotations

import pytest

from toychain.constants import PERSISTENCE_SCHEMA_VERSION
from toychain.errors import NodeRuntimeError
from toychain.node_config import NodeConfig, save_node_config
from toychain.process import (
    dismiss_local_network_registry,
    run_local_network,
    stop_local_network,
    stop_node,
)
from tests.kill_helpers import kill_and_reap_pid


def _kill_pid(pid: int) -> None:
    kill_and_reap_pid(pid)


def test_malformed_config_on_first_node_does_not_block_stop_of_second(tmp_path, monkeypatch):
    root = tmp_path / "network"
    run_local_network(root, nodes=2, base_port=9940)
    (root / "node1" / "config.json").write_text("{not-json", encoding="utf-8")
    original = (root / "local-network.json").read_bytes()
    node2_attempts: list[str] = []

    def tracking_stop(data_dir, timeout=5.0):
        if str(data_dir).endswith("node2"):
            node2_attempts.append(str(data_dir))
        return stop_node(data_dir, timeout=timeout)

    monkeypatch.setattr("toychain.process.stop_node", tracking_stop)

    with pytest.raises(NodeRuntimeError, match="shutdown incomplete"):
        stop_local_network(root)

    assert len(node2_attempts) == 1
    assert (root / "local-network.json").read_bytes() == original

    monkeypatch.setattr("toychain.process.stop_node", stop_node)
    node1_pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    _kill_pid(node1_pid)
    stop_local_network(root)


def test_registry_preserved_while_malformed_node_has_live_pid(tmp_path):
    root = tmp_path / "network"
    run_local_network(root, nodes=2, base_port=9945)
    (root / "node1" / "config.json").write_text("{not-json", encoding="utf-8")
    original = (root / "local-network.json").read_bytes()

    with pytest.raises(NodeRuntimeError, match="registry preserved"):
        stop_local_network(root)

    assert (root / "local-network.json").read_bytes() == original
    assert (root / "node1" / "node.pid").exists()

    node1_pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    _kill_pid(node1_pid)
    stop_node(root / "node2", timeout=8)
    stop_local_network(root)


def test_dismiss_registry_succeeds_with_malformed_config_when_pids_dead(tmp_path):
    root = tmp_path / "network"
    run_local_network(root, nodes=2, base_port=9950)
    node1_pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    (root / "node1" / "config.json").write_text("{not-json", encoding="utf-8")

    stop_node(root / "node2", timeout=8)
    _kill_pid(node1_pid)

    dismiss_local_network_registry(root)
    assert not (root / "local-network.json").exists()


def test_mismatched_config_data_dir_does_not_block_stop_of_later_nodes(tmp_path, monkeypatch):
    root = tmp_path / "network"
    run_local_network(root, nodes=2, base_port=9955)
    save_node_config(
        root / "node1" / "config.json",
        NodeConfig(
            schema_version=PERSISTENCE_SCHEMA_VERSION,
            data_dir=str(tmp_path / "elsewhere"),
            port=9955,
        ),
    )
    original = (root / "local-network.json").read_bytes()
    node2_attempts: list[str] = []

    def tracking_stop(data_dir, timeout=5.0):
        if str(data_dir).endswith("node2"):
            node2_attempts.append(str(data_dir))
        return stop_node(data_dir, timeout=timeout)

    monkeypatch.setattr("toychain.process.stop_node", tracking_stop)

    with pytest.raises(NodeRuntimeError, match="shutdown incomplete"):
        stop_local_network(root)

    assert len(node2_attempts) == 1
    assert (root / "local-network.json").read_bytes() == original

    monkeypatch.setattr("toychain.process.stop_node", stop_node)
    node1_pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    _kill_pid(node1_pid)
    stop_local_network(root)


def test_network_status_reports_malformed_node_without_aborting(tmp_path):
    from toychain.process import network_status

    root = tmp_path / "network"
    run_local_network(root, nodes=2, base_port=9960)
    (root / "node1" / "config.json").write_text("{not-json", encoding="utf-8")
    try:
        statuses = network_status(root)
        assert len(statuses) == 2
        assert statuses[0].state == "malformed"
        assert statuses[1].running is True
    finally:
        node1_pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
        _kill_pid(node1_pid)
        stop_node(root / "node2", timeout=8)
        stop_local_network(root)
