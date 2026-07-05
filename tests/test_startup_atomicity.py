"""Tests for failure-atomic node and local-network startup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toychain.constants import PERSISTENCE_SCHEMA_VERSION
from toychain.errors import NodeRuntimeError, PersistenceError
from toychain.node import run_node_process
from toychain.node_config import NodeConfig, load_node_config, save_node_config
from toychain.persistence import DataStore
from toychain.process import (
    run_local_network,
    start_node,
    start_node_with_handle,
    stop_node,
)
from toychain.process_identity import read_readiness

TEST_INSTANCE_ID = "00000000-0000-4000-8000-000000000099"


def test_config_write_failure_leaves_no_pid_or_lock(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    store.initialize()

    def fail_config(path, config):
        raise PersistenceError("simulated config write failure")

    monkeypatch.setattr("toychain.node.save_node_config", fail_config)
    with pytest.raises(PersistenceError, match="simulated config write failure"):
        run_node_process(tmp_path, port=9000, instance_id=TEST_INSTANCE_ID)
    assert not store.pid_path.exists()
    assert not store.lock_path.exists()
    assert not store.lifecycle_path.exists()
    assert not store.ready_path.exists()


def test_local_network_registry_write_failure_stops_started_nodes(tmp_path, monkeypatch):
    root = tmp_path / "network"
    started: list[str] = []
    original_start = start_node_with_handle

    def tracking_start(data_dir, port=0, *, node_name=None):
        started_child = original_start(data_dir, port=port, node_name=node_name)
        started.append(str(data_dir))
        return started_child

    def fail_registry(path, value):
        if path.name == "local-network.json":
            raise PersistenceError("simulated registry write failure")
        from toychain.persistence import write_json as real_write_json

        return real_write_json(path, value)

    monkeypatch.setattr("toychain.process.start_node_with_handle", tracking_start)
    monkeypatch.setattr("toychain.process.write_json", fail_registry)

    with pytest.raises(PersistenceError, match="simulated registry write failure"):
        run_local_network(root, nodes=2, base_port=9910)

    assert not (root / "local-network.json").exists()
    assert not (root / "local-network.starting.json").exists()
    for data_dir in started:
        status_path = Path(data_dir)
        assert not DataStore(status_path).pid_path.exists()


def test_local_network_second_child_failure_stops_first(tmp_path, monkeypatch):
    root = tmp_path / "network"
    calls = {"count": 0}
    original_start = start_node_with_handle

    def flaky_start(data_dir, port=0, *, node_name=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise NodeRuntimeError("simulated child startup failure")
        return original_start(data_dir, port=port, node_name=node_name)

    monkeypatch.setattr("toychain.process.start_node_with_handle", flaky_start)

    with pytest.raises(NodeRuntimeError, match="simulated child startup failure"):
        run_local_network(root, nodes=3, base_port=9920)

    assert not (root / "local-network.json").exists()
    assert not (root / "local-network.starting.json").exists()
    first_node = DataStore(root / "node1")
    assert not first_node.pid_path.exists()


def test_readiness_timeout_terminates_spawned_child(tmp_path, monkeypatch):
    monkeypatch.setattr("toychain.process.read_readiness", lambda _path: None)
    monkeypatch.setattr("toychain.process.time.sleep", lambda _seconds: None)

    with pytest.raises(
        NodeRuntimeError,
        match="(did not become ready|still alive)",
    ):
        start_node(tmp_path, port=9930)

    store = DataStore(tmp_path)
    assert not store.pid_path.exists()
    assert not store.lock_path.exists()
    assert not store.ready_path.exists()


def test_malformed_readiness_json_terminates_spawned_child(tmp_path, monkeypatch):
    def broken_readiness(path):
        if path.exists():
            raise PersistenceError("malformed readiness")
        return None

    monkeypatch.setattr("toychain.process.read_readiness", broken_readiness)
    monkeypatch.setattr("toychain.process.time.sleep", lambda _seconds: None)

    with pytest.raises(
        NodeRuntimeError,
        match="(did not become ready|still alive)",
    ):
        start_node(tmp_path, port=9931)

    store = DataStore(tmp_path)
    assert not store.pid_path.exists()
    assert not store.lock_path.exists()


def test_malformed_config_during_readiness_terminates_child(tmp_path, monkeypatch):
    original_load = __import__(
        "toychain.node_config", fromlist=["load_node_config"]
    ).load_node_config
    calls = {"count": 0}

    def flaky_load(path, *, expected_data_dir=None):
        calls["count"] += 1
        if calls["count"] > 1:
            raise PersistenceError("simulated malformed config during readiness")
        return original_load(path, expected_data_dir=expected_data_dir)

    monkeypatch.setattr("toychain.process.load_node_config", flaky_load)

    with pytest.raises(
        NodeRuntimeError,
        match="(did not become ready|malformed config|still alive)",
    ):
        start_node(tmp_path, port=9932)

    store = DataStore(tmp_path)
    assert not store.pid_path.exists()
    assert not store.lock_path.exists()


def test_successful_local_network_creates_atomic_registry(tmp_path):
    root = tmp_path / "network"
    statuses = run_local_network(root, nodes=2, base_port=9940)
    try:
        assert len(statuses) == 2
        registry = json.loads((root / "local-network.json").read_text(encoding="utf-8"))
        assert registry == {
            "nodes": [
                {"name": "node1", "port": 9940},
                {"name": "node2", "port": 9941},
            ],
        }
        assert not (root / "local-network.starting.json").exists()
        for status in statuses:
            store = DataStore(status.data_dir)
            assert store.ready_path.exists()
            readiness = read_readiness(store.ready_path)
            assert readiness is not None
            assert readiness.port == load_node_config(store.config_path).port
    finally:
        for status in statuses:
            stop_node(status.data_dir, timeout=8)


def test_node_config_rejects_unknown_properties(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data_dir": str(tmp_path),
                "port": 9000,
                "extra": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PersistenceError, match="Malformed node config"):
        load_node_config(path)


@pytest.mark.parametrize(
    ("port", "message"),
    [
        ("9000", "integer"),
        (True, "integer"),
        (-1, "JSON schema validation failed"),
    ],
)
def test_node_config_rejects_invalid_port(tmp_path, port, message):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"schema_version": 1, "data_dir": str(tmp_path), "port": port}),
        encoding="utf-8",
    )
    with pytest.raises(PersistenceError, match=message):
        load_node_config(path)


def test_node_config_rejects_unsupported_schema_version(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"schema_version": 99, "data_dir": str(tmp_path), "port": 9000}),
        encoding="utf-8",
    )
    with pytest.raises(PersistenceError, match="Malformed node config"):
        load_node_config(path)


def test_node_config_rejects_mismatched_data_dir(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"schema_version": 1, "data_dir": "/other/path", "port": 9000}),
        encoding="utf-8",
    )
    with pytest.raises(NodeRuntimeError, match="data_dir does not match"):
        load_node_config(path, expected_data_dir=tmp_path)


def test_node_config_missing_schema_version_treated_as_version_one(tmp_path):
    config = NodeConfig(
        schema_version=PERSISTENCE_SCHEMA_VERSION,
        data_dir=str(tmp_path),
        port=9000,
    )
    save_node_config(tmp_path / "config.json", config)
    legacy = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    del legacy["schema_version"]
    (tmp_path / "legacy-config.json").write_text(json.dumps(legacy), encoding="utf-8")
    loaded = load_node_config(tmp_path / "legacy-config.json", expected_data_dir=tmp_path)
    assert loaded.port == 9000
    assert loaded.schema_version == PERSISTENCE_SCHEMA_VERSION


def test_node_config_output_validates_against_schema(tmp_path):
    from toychain.json_validation import validate_json_schema

    config = NodeConfig(
        schema_version=PERSISTENCE_SCHEMA_VERSION,
        data_dir=str(tmp_path),
        port=9000,
    )
    validate_json_schema(config.to_dict(), "node-config")
