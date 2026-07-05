"""Process-related functionality."""

from __future__ import annotations

import ctypes
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
from .persistence import DataStore, read_json, write_json

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
    try:
        config = read_json(store.config_path)
        configured = Path(str(config["data_dir"])).expanduser().resolve()
    except (KeyError, PersistenceError, OSError, TypeError, ValueError) as exc:
        raise NodeRuntimeError("Malformed node config.json") from exc
    if configured != store.data_dir:
        raise NodeRuntimeError("Node config data_dir does not match registry path")


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def node_status(data_dir: str | Path) -> ProcessStatus:
    store = DataStore(data_dir)
    pid: int | None = None
    if store.pid_path.exists():
        try:
            pid = int(store.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = None
    port: int | None = None
    if store.config_path.exists():
        try:
            port = int(read_json(store.config_path).get("port", 0))
        except (PersistenceError, AttributeError, TypeError, ValueError):
            port = None
    return ProcessStatus(
        data_dir=str(store.data_dir),
        running=pid is not None and process_is_running(pid),
        pid=pid,
        port=port,
        log_file=str(store.log_path),
    )


def start_node(data_dir: str | Path, port: int = 0) -> ProcessStatus:
    store = DataStore(data_dir)
    store.initialize()
    existing = node_status(store.data_dir)
    if existing.running:
        raise NodeRuntimeError(f"Node is already running with PID {existing.pid}")
    store.pid_path.unlink(missing_ok=True)
    store.lock_path.unlink(missing_ok=True)
    store.stop_path.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "toychain",
        "--data-dir",
        str(store.data_dir),
        "_node-run",
        "--port",
        str(port),
    ]
    environment = os.environ.copy()
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
    deadline = time.time() + 5
    while time.time() < deadline:
        status = node_status(store.data_dir)
        if status.running:
            return status
        time.sleep(0.05)
    raise NodeRuntimeError(f"Node did not start; inspect {store.log_path}")


def stop_node(data_dir: str | Path, timeout: float = 5.0) -> ProcessStatus:
    store = DataStore(data_dir)
    status = node_status(store.data_dir)
    if not status.running or status.pid is None:
        store.pid_path.unlink(missing_ok=True)
        store.lock_path.unlink(missing_ok=True)
        store.stop_path.unlink(missing_ok=True)
        return node_status(store.data_dir)
    store.stop_path.write_text("stop\n", encoding="ascii")
    try:
        if os.name != "nt":
            os.kill(status.pid, signal.SIGTERM)
    except OSError as exc:
        raise NodeRuntimeError(f"Could not signal node PID {status.pid}: {exc}") from exc
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not store.pid_path.exists() or not process_is_running(status.pid):
            return node_status(store.data_dir)
        time.sleep(0.1)
    raise NodeRuntimeError(f"Node PID {status.pid} did not stop within {timeout} seconds")


def run_local_network(
    base_dir: str | Path,
    nodes: int,
    base_port: int = 9001,
) -> list[ProcessStatus]:
    if nodes <= 0:
        raise NodeRuntimeError("Local network must contain at least one node")
    root = _network_root(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    statuses: list[ProcessStatus] = []
    try:
        for index in range(nodes):
            statuses.append(
                start_node(root / f"node{index + 1}", base_port + index)
            )
    except Exception:
        for status in statuses:
            try:
                stop_node(status.data_dir)
            except NodeRuntimeError:
                pass
        raise
    write_json(
        root / "local-network.json",
        {
            "nodes": [
                {"name": f"node{index + 1}", "port": base_port + index}
                for index in range(nodes)
            ],
        },
    )
    return statuses


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


def stop_local_network(base_dir: str | Path) -> list[ProcessStatus]:
    statuses = network_status(base_dir)
    stopped = [stop_node(status.data_dir) for status in statuses]
    return stopped
