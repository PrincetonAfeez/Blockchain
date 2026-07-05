"""Process-related functionality."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CodecError, NodeRuntimeError, PersistenceError
from .json_validation import reject_unknown_keys, validate_json_schema
from .node_config import load_node_config
from .persistence import DataStore, read_json, write_json
from .process_identity import (
    cleanup_stale_node_files as _cleanup_stale_node_files,
    new_instance_id,
    process_is_running,
    read_lifecycle,
    read_readiness,
    verify_process_identity,
)

NODE_NAME_PATTERN = re.compile(r"^node[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    data_dir: str
    running: bool
    pid: int | None
    port: int | None
    log_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
            "running": self.running,
            "pid": self.pid,
            "port": self.port,
            "log_file": self.log_file,
        }


def _network_root(base_dir: str | Path) -> Path:
    return Path(base_dir).expanduser().resolve()


def _read_network_registry(registry: Path) -> dict[str, Any]:
    data = read_json(registry)
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise NodeRuntimeError("Malformed local network registry")
    try:
        validate_json_schema(data, "local-network")
        reject_unknown_keys(data, frozenset({"nodes"}), "local network registry")
    except CodecError as exc:
        raise NodeRuntimeError(str(exc)) from exc
    return data


def _resolve_registry_node_path(root: Path, entry: dict[str, Any]) -> Path:
    if "data_dir" in entry:
        raise NodeRuntimeError(
            "Local network registry must use node names, not data_dir paths"
        )
    name = entry.get("name")
    if not isinstance(name, str) or NODE_NAME_PATTERN.fullmatch(name) is None:
        raise NodeRuntimeError(
            f"Invalid local network node name: {name!r}; expected node[1-9][0-9]*"
        )
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise NodeRuntimeError(
            f"Local network node path escapes the network root: {name!r}"
        ) from exc
    return candidate


def _verify_node_config_data_dir(node_path: Path) -> None:
    store = DataStore(node_path)
    if not store.config_path.exists():
        return
    load_node_config(store.config_path, expected_data_dir=store.data_dir)


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def node_status(data_dir: str | Path) -> ProcessStatus:
    store = DataStore(data_dir)
    pid = _read_pid(store.pid_path)
    port: int | None = None
    if store.config_path.exists():
        port = load_node_config(store.config_path, expected_data_dir=store.data_dir).port
    return ProcessStatus(
        data_dir=str(store.data_dir),
        running=pid is not None and process_is_running(pid),
        pid=pid,
        port=port,
        log_file=str(store.log_path),
    )


def _abort_unready_node(store: DataStore, instance_id: str) -> None:
    pid = _read_pid(store.pid_path)
    if pid is None or not process_is_running(pid):
        _cleanup_stale_node_files(store, force=True)
        return
    lifecycle = read_lifecycle(store.lifecycle_path)
    if lifecycle is None or lifecycle.instance_id != instance_id:
        raise NodeRuntimeError(
            f"Node did not become ready and PID {pid} is not verified as this Toychain node"
        )
    try:
        verify_process_identity(pid, lifecycle, store)
    except NodeRuntimeError as exc:
        raise NodeRuntimeError(
            f"Node did not become ready; refusing to signal unverified PID {pid}"
        ) from exc
    store.stop_path.write_text("stop\n", encoding="ascii")
    try:
        if os.name != "nt":
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not process_is_running(pid):
            _cleanup_stale_node_files(store)
            return
        time.sleep(0.05)
    _cleanup_stale_node_files(store, force=True)


def start_node(data_dir: str | Path, port: int = 0) -> ProcessStatus:
    store = DataStore(data_dir)
    store.initialize()
    existing = node_status(store.data_dir)
    if existing.running:
        raise NodeRuntimeError(f"Node is already running with PID {existing.pid}")
    store.lifecycle_path.unlink(missing_ok=True)
    instance_id = new_instance_id()
    command = [
        sys.executable,
        "-m",
        "toychain",
        "--data-dir",
        str(store.data_dir),
        "_node-run",
        "--port",
        str(port),
        "--instance-id",
        instance_id,
    ]
    environment = os.environ.copy()
    environment["TOYCHAIN_INSTANCE_ID"] = instance_id
    src_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = (
        src_root
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    with store.log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=store.data_dir,
            env=environment,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        readiness = read_readiness(store.ready_path)
        if readiness is not None and readiness.instance_id == instance_id:
            status = node_status(store.data_dir)
            if status.running and status.pid == readiness.pid:
                return status
        time.sleep(0.05)
    _abort_unready_node(store, instance_id)
    raise NodeRuntimeError(
        f"Node did not become ready within 5 seconds; inspect {store.log_path}"
    )


def stop_node(data_dir: str | Path, timeout: float = 5.0) -> ProcessStatus:
    store = DataStore(data_dir)
    status = node_status(store.data_dir)
    pid = status.pid
    if pid is None or not status.running:
        cleanup_stale_node_files(store)
        return node_status(store.data_dir)

    lifecycle = read_lifecycle(store.lifecycle_path)
    if lifecycle is None:
        raise NodeRuntimeError(
            f"Refusing to signal PID {pid} because lifecycle identity is missing"
        )
    try:
        verify_process_identity(pid, lifecycle, store)
    except NodeRuntimeError as exc:
        raise NodeRuntimeError(
            f"Refusing to signal PID {pid} because it is not verified as this Toychain node"
        ) from exc

    store.stop_path.write_text("stop\n", encoding="ascii")
    try:
        if os.name != "nt":
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise NodeRuntimeError(f"Could not signal node PID {pid}: {exc}") from exc
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not store.pid_path.exists() or not process_is_running(pid):
            return node_status(store.data_dir)
        time.sleep(0.1)
    raise NodeRuntimeError(f"Node PID {pid} did not stop within {timeout} seconds")


def run_local_network(
    base_dir: str | Path,
    nodes: int,
    base_port: int = 9001,
) -> list[ProcessStatus]:
    if nodes <= 0:
        raise NodeRuntimeError("Local network must contain at least one node")
    root = _network_root(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    registry = root / "local-network.json"
    starting_registry = root / "local-network.starting.json"
    planned = {
        "nodes": [
            {"name": f"node{index + 1}", "port": base_port + index}
            for index in range(nodes)
        ],
    }
    statuses: list[ProcessStatus] = []
    try:
        write_json(starting_registry, planned)
        for index in range(nodes):
            statuses.append(
                start_node(root / f"node{index + 1}", base_port + index)
            )
        write_json(registry, planned)
        starting_registry.unlink(missing_ok=True)
        return statuses
    except Exception:
        for status in statuses:
            try:
                stop_node(status.data_dir)
            except NodeRuntimeError:
                pass
        registry.unlink(missing_ok=True)
        starting_registry.unlink(missing_ok=True)
        raise


def network_status(base_dir: str | Path) -> list[ProcessStatus]:
    root = _network_root(base_dir)
    registry = root / "local-network.json"
    if not registry.exists():
        return []
    data = _read_network_registry(registry)
    statuses: list[ProcessStatus] = []
    for entry in data["nodes"]:
        if not isinstance(entry, dict):
            raise NodeRuntimeError("Malformed local network registry entry")
        node_path = _resolve_registry_node_path(root, entry)
        _verify_node_config_data_dir(node_path)
        statuses.append(node_status(node_path))
    return statuses


def cleanup_stale_node_files(store: DataStore, *, force: bool = False) -> None:
    _cleanup_stale_node_files(store, force=force)


def stop_local_network(base_dir: str | Path) -> list[ProcessStatus]:
    statuses = network_status(base_dir)
    stopped = [stop_node(status.data_dir) for status in statuses]
    return stopped
