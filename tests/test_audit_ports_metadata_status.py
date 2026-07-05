"""Tests for port validation, CLI help, index metadata, and strict lifecycle records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from toychain.cli import main
from toychain.constants import PERSISTENCE_SCHEMA_VERSION
from toychain.errors import PersistenceError, ValidationError
from toychain.node import Node
from toychain.node_config import NodeConfig, load_node_config, save_node_config, valid_port_arg
from toychain.persistence import DataStore
from toychain.process import node_status, run_local_network, start_node, stop_node
from toychain.process_identity import NodeReadiness, read_lifecycle, read_readiness, write_readiness

_SRC_ROOT = str(Path(__file__).resolve().parents[1] / "src")


def test_valid_port_arg_accepts_boundaries():
    assert valid_port_arg("0") == 0
    assert valid_port_arg("65535") == 65535


@pytest.mark.parametrize("value", ["-1", "65536"])
def test_cli_rejects_invalid_port_with_exit_code_2(tmp_path, value):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "toychain",
            "--data-dir",
            str(tmp_path),
            "node",
            "start",
            "--port",
            value,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _SRC_ROOT},
    )
    assert result.returncode == 2


def test_cli_rejects_base_port_overflow_before_starting_children(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "toychain",
            "--data-dir",
            str(tmp_path / "net"),
            "network",
            "run-local",
            "--nodes",
            "3",
            "--base-port",
            "65534",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _SRC_ROOT},
    )
    assert result.returncode == 1
    assert "65535" in result.stderr


def test_save_node_config_rejects_invalid_port(tmp_path):
    with pytest.raises(ValidationError, match="Port must be between"):
        save_node_config(
            tmp_path / "config.json",
            NodeConfig(
                schema_version=PERSISTENCE_SCHEMA_VERSION,
                data_dir=str(tmp_path),
                port=70000,
            ),
        )


def test_saved_config_always_loads(tmp_path):
    config = NodeConfig(
        schema_version=PERSISTENCE_SCHEMA_VERSION,
        data_dir=str(tmp_path),
        port=8080,
    )
    path = tmp_path / "config.json"
    save_node_config(path, config)
    loaded = load_node_config(path, expected_data_dir=tmp_path)
    assert loaded == config


def test_cli_help_documents_port_range_and_cleanup_behavior():
    port_help = subprocess.run(
        [sys.executable, "-m", "toychain", "node", "start", "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _SRC_ROOT},
    )
    node_help = subprocess.run(
        [sys.executable, "-m", "toychain", "node", "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _SRC_ROOT},
    )
    assert port_help.returncode == 0
    assert node_help.returncode == 0
    node_text = " ".join(node_help.stdout.split())
    assert "65535" in port_help.stdout
    assert "live verified nodes are never cleaned" in node_text
    assert "live unverified PIDs require --dangerous" in node_text


def _mine_height_one(node: Node, alice) -> str:
    from toychain.block import make_block_candidate, mine_block

    block = mine_block(
        make_block_candidate(
            previous_hash=bytes.fromhex(node.chain.tip_hash),
            miner_address=alice.address,
            height=1,
            difficulty_bits=1,
            timestamp=node.chain.tip.header.timestamp + 1,
        )
    )[0]
    node.chain.add_block(block)
    node.flush()
    return block.hash


def test_validate_chain_detects_tampered_index_height(tmp_path, alice):
    node = Node.open(tmp_path)
    block_hash = _mine_height_one(node, alice)
    index = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    index["blocks"][block_hash]["height"] = 99
    node.store.index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    report = Node.open(tmp_path, writable=False).chain.validate_all_blocks()
    assert not report.valid
    assert report.invalid_block_hash == block_hash
    assert "height" in report.message


def test_validate_chain_detects_tampered_index_cumulative_work(tmp_path, alice):
    node = Node.open(tmp_path)
    block_hash = _mine_height_one(node, alice)
    index = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    index["blocks"][block_hash]["cumulative_work"] = 1
    node.store.index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    report = Node.open(tmp_path, writable=False).chain.validate_all_blocks()
    assert not report.valid
    assert "cumulative_work" in report.message


def test_validate_chain_detects_tampered_index_parent_hash(tmp_path, alice):
    node = Node.open(tmp_path)
    block_hash = _mine_height_one(node, alice)
    index = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    index["blocks"][block_hash]["parent_hash"] = "ab" * 32
    node.store.index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    report = Node.open(tmp_path, writable=False).chain.validate_all_blocks()
    assert not report.valid
    assert "parent_hash" in report.message


def test_validate_chain_cli_exit_code_on_tampered_index(tmp_path, alice):
    node = Node.open(tmp_path)
    block_hash = _mine_height_one(node, alice)
    index = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    index["blocks"][block_hash]["height"] = 99
    node.store.index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    assert main(["--data-dir", str(tmp_path), "validate-chain"]) == 1


def test_node_status_reports_live_unverified_for_unrelated_pid(tmp_path):
    store = DataStore(tmp_path)
    store.initialize()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.pid_path.write_text(str(sleeper.pid), encoding="ascii")
        status = node_status(tmp_path)
        assert not status.running
        assert not status.verified
        assert status.state == "live_unverified"
    finally:
        sleeper.terminate()
        sleeper.wait()


def test_node_status_reports_running_verified_for_started_node(tmp_path):
    started = start_node(tmp_path, port=9950)
    try:
        status = node_status(tmp_path)
        assert status.running
        assert status.verified
        assert status.state == "running_verified"
        assert status.instance_id is not None
        assert started.pid == status.pid
    finally:
        stop_node(tmp_path, timeout=8)


def test_node_status_reports_stale_for_dead_pid(tmp_path):
    store = DataStore(tmp_path)
    store.initialize()
    store.pid_path.write_text("424242", encoding="ascii")
    status = node_status(tmp_path)
    assert status.state == "stale"
    assert not status.running


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "pid": "1234",
            "instance_id": "00000000-0000-4000-8000-000000000001",
            "started_at": 1,
            "process_start_token": 1,
            "data_dir": "/x",
            "executable": "/y",
        },
        {
            "schema_version": 1,
            "pid": 1,
            "instance_id": 99,
            "started_at": 1,
            "process_start_token": 1,
            "data_dir": "/x",
            "executable": "/y",
        },
        {
            "schema_version": 1,
            "pid": 1,
            "instance_id": "00000000-0000-4000-8000-000000000001",
            "started_at": 1,
            "process_start_token": 1,
            "data_dir": "",
            "executable": "/y",
        },
        {
            "schema_version": 1,
            "pid": 1,
            "instance_id": "not-a-uuid",
            "started_at": 1,
            "process_start_token": 1,
            "data_dir": "/x",
            "executable": "/y",
        },
        {
            "schema_version": 99,
            "pid": 1,
            "instance_id": "00000000-0000-4000-8000-000000000001",
            "started_at": 1,
            "process_start_token": 1,
            "data_dir": "/x",
            "executable": "/y",
        },
        {
            "schema_version": 1,
            "pid": 1,
            "instance_id": "00000000-0000-4000-8000-000000000001",
            "started_at": 1,
            "process_start_token": 1,
            "data_dir": "/x",
            "executable": "/y",
            "extra": True,
        },
    ],
)
def test_lifecycle_strict_loader_rejects_invalid_records(tmp_path, payload):
    path = tmp_path / "node.lifecycle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PersistenceError, match="Malformed|Unknown|schema"):
        read_lifecycle(path)


def test_readiness_round_trip(tmp_path):
    readiness = NodeReadiness(
        schema_version=1,
        instance_id="00000000-0000-4000-8000-000000000001",
        pid=123,
        data_dir=str(tmp_path),
        port=9000,
        ready_at=1,
    )
    path = tmp_path / "node.ready.json"
    write_readiness(path, readiness)
    loaded = read_readiness(path)
    assert loaded == readiness


def test_run_local_network_rejects_port_overflow_before_spawn(tmp_path):
    with pytest.raises(Exception, match="65535"):
        run_local_network(tmp_path / "net", nodes=3, base_port=65534)
