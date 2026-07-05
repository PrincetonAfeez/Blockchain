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
from .node_config import load_node_config, validate_port_value
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
    verified: bool = False
    state: str = "stopped"
    message: str | None = None
    instance_id: str | None = None

    @property
    def pid_is_live(self) -> bool:
        return self.pid is not None and process_is_running(self.pid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
            "running": self.running,
            "pid": self.pid,
            "port": self.port,
            "log_file": self.log_file,
            "verified": self.verified,
            "state": self.state,
            "message": self.message,
            "instance_id": self.instance_id,
        }


def _network_root(base_dir: str | Path) -> Path:
    return Path(base_dir).expanduser().resolve()


def _validate_network_ports(base_port: int, nodes: int) -> None:
    validate_port_value(base_port)
    if nodes <= 0:
        raise NodeRuntimeError("Local network must contain at least one node")
    if base_port + nodes - 1 > 65535:
        raise NodeRuntimeError("Local network port range exceeds 65535")


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
    config_error: str | None = None
    if store.config_path.exists():
        try:
            port = load_node_config(
                store.config_path,
                expected_data_dir=store.data_dir,
            ).port
        except (PersistenceError, NodeRuntimeError) as exc:
            config_error = str(exc)

    if config_error is not None:
        return ProcessStatus(
            data_dir=str(store.data_dir),
            running=False,
            pid=pid,
            port=port,
            log_file=str(store.log_path),
            verified=False,
            state="malformed",
            message=config_error,
        )

    if pid is None:
        has_stale_files = any(
            path.exists()
            for path in (
                store.lock_path,
                store.lifecycle_path,
                store.ready_path,
                store.stop_path,
            )
        )
        return ProcessStatus(
            data_dir=str(store.data_dir),
            running=False,
            pid=None,
            port=port,
            log_file=str(store.log_path),
            verified=False,
            state="stale" if has_stale_files else "stopped",
            message="Lifecycle files remain without node.pid" if has_stale_files else None,
        )

    if not process_is_running(pid):
        return ProcessStatus(
            data_dir=str(store.data_dir),
            running=False,
            pid=pid,
            port=port,
            log_file=str(store.log_path),
            verified=False,
            state="stale",
            message=f"PID {pid} is not running",
        )

    try:
        lifecycle = read_lifecycle(store.lifecycle_path)
    except PersistenceError as exc:
        return ProcessStatus(
            data_dir=str(store.data_dir),
            running=False,
            pid=pid,
            port=port,
            log_file=str(store.log_path),
            verified=False,
            state="malformed",
            message=str(exc),
        )

    if lifecycle is None:
        return ProcessStatus(
            data_dir=str(store.data_dir),
            running=False,
            pid=pid,
            port=port,
            log_file=str(store.log_path),
            verified=False,
            state="live_unverified",
            message="Missing node.lifecycle.json for live PID",
        )

    try:
        verify_process_identity(pid, lifecycle, store)
    except NodeRuntimeError as exc:
        return ProcessStatus(
            data_dir=str(store.data_dir),
            running=False,
            pid=pid,
            port=port,
            log_file=str(store.log_path),
            verified=False,
            state="live_unverified",
            message=str(exc),
        )

    return ProcessStatus(
        data_dir=str(store.data_dir),
        running=True,
        pid=pid,
        port=port,
        log_file=str(store.log_path),
        verified=True,
        state="running_verified",
        instance_id=lifecycle.instance_id,
    )


def _terminate_subprocess(
    child: subprocess.Popen[Any],
    store: DataStore | None = None,
    *,
    graceful_seconds: float = 2.0,
    kill_seconds: float = 3.0,
) -> bool:
    if child.poll() is not None:
        return True
    if store is not None:
        store.stop_path.write_text("stop\n", encoding="ascii")
    child.terminate()
    deadline = time.time() + graceful_seconds
    while time.time() < deadline:
        if child.poll() is not None:
            return True
        time.sleep(0.05)
    child.kill()
    try:
        child.wait(timeout=kill_seconds)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(child.pid)],
                capture_output=True,
                check=False,
            )
            try:
                child.wait(timeout=kill_seconds)
            except subprocess.TimeoutExpired:
                return child.poll() is not None
        return child.poll() is not None
    return child.poll() is not None


def _wait_for_child_exit(child: subprocess.Popen[Any], *, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if child.poll() is not None:
            return True
        time.sleep(0.05)
    return child.poll() is not None


def _cleanup_dead_lifecycle_files(
    store: DataStore,
    *,
    child: subprocess.Popen[Any] | None = None,
) -> None:
    if child is not None and child.poll() is None:
        raise NodeRuntimeError(
            f"Refusing to remove lifecycle files while child PID {child.pid} is still alive"
        )
    tracked_pid = _read_pid(store.pid_path)
    if tracked_pid is not None and process_is_running(tracked_pid):
        child_reaped = child is not None and child.pid == tracked_pid and child.poll() is not None
        if not child_reaped:
            raise NodeRuntimeError(
                f"Refusing to remove lifecycle files while PID {tracked_pid} is still alive"
            )
    store.pid_path.unlink(missing_ok=True)
    store.lock_path.unlink(missing_ok=True)
    store.stop_path.unlink(missing_ok=True)
    store.lifecycle_path.unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)


def _wait_for_node_ready(
    store: DataStore,
    instance_id: str,
    *,
    deadline: float,
) -> ProcessStatus:
    while time.time() < deadline:
        try:
            readiness = read_readiness(store.ready_path)
            if readiness is not None and readiness.instance_id == instance_id:
                if store.config_path.exists():
                    load_node_config(store.config_path, expected_data_dir=store.data_dir)
                status = node_status(store.data_dir)
                if status.running and status.pid == readiness.pid:
                    return status
        except PersistenceError:
            break
        time.sleep(0.05)
    raise NodeRuntimeError(
        f"Node did not become ready within the allotted time; inspect {store.log_path}"
    )


def _spawn_node_child(
    store: DataStore,
    *,
    port: int,
    instance_id: str,
) -> subprocess.Popen[Any]:
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
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    with store.log_path.open("a", encoding="utf-8") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=store.data_dir,
            env=environment,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )


@dataclass(frozen=True, slots=True)
class StartedChild:
    name: str
    port: int
    data_dir: str
    instance_id: str
    popen: subprocess.Popen[Any]
    status: ProcessStatus


def _prepare_node_start(data_dir: str | Path, port: int) -> tuple[DataStore, str]:
    validate_port_value(port)
    store = DataStore(data_dir)
    store.initialize()
    existing = node_status(store.data_dir)
    if existing.state == "running_verified":
        raise NodeRuntimeError(f"Node is already running with PID {existing.pid}")
    if existing.state == "live_unverified":
        raise NodeRuntimeError(
            f"Refusing to start node: {existing.message or 'live unverified PID present'}"
        )
    if existing.state == "malformed":
        raise NodeRuntimeError(
            f"Refusing to start node: {existing.message or 'malformed node files'}"
        )
    if existing.state == "stale":
        cleanup_stale_node_files(store)
    store.lifecycle_path.unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)
    return store, new_instance_id()


def start_node_with_handle(
    data_dir: str | Path,
    port: int = 0,
    *,
    node_name: str | None = None,
) -> StartedChild:
    store, instance_id = _prepare_node_start(data_dir, port)
    child: subprocess.Popen[Any] | None = None
    try:
        child = _spawn_node_child(store, port=port, instance_id=instance_id)
        status = _wait_for_node_ready(
            store,
            instance_id,
            deadline=time.time() + 5.0,
        )
    except Exception as exc:
        if child is not None:
            if not _terminate_subprocess(child, store):
                raise NodeRuntimeError(
                    f"Node startup failed and child PID {child.pid} is still alive; "
                    f"inspect {store.log_path}"
                ) from exc
            if not _wait_for_child_exit(child):
                raise NodeRuntimeError(
                    f"Node startup failed and child PID {child.pid} is still alive; "
                    f"inspect {store.log_path}"
                ) from exc
        try:
            _cleanup_dead_lifecycle_files(store, child=child)
        except NodeRuntimeError as cleanup_exc:
            raise NodeRuntimeError(
                f"{exc}; additionally {cleanup_exc}"
            ) from exc
        raise
    assert child is not None
    return StartedChild(
        name=node_name or store.data_dir.name,
        port=port,
        data_dir=str(store.data_dir),
        instance_id=instance_id,
        popen=child,
        status=status,
    )


def start_node(data_dir: str | Path, port: int = 0) -> ProcessStatus:
    return start_node_with_handle(data_dir, port).status


def stop_node(data_dir: str | Path, timeout: float = 5.0) -> ProcessStatus:
    store = DataStore(data_dir)
    status = node_status(store.data_dir)
    if status.state == "live_unverified":
        raise NodeRuntimeError(
            f"Refusing to signal PID {status.pid} because it is not verified as this Toychain node"
        )
    if status.state == "malformed":
        raise NodeRuntimeError(
            f"Refusing to stop node: {status.message or 'malformed node files'}"
        )
    if not status.running:
        if status.state == "stale":
            cleanup_stale_node_files(store)
        return node_status(store.data_dir)

    pid = status.pid
    if pid is None:
        raise NodeRuntimeError("Verified running node is missing PID")
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


def _write_starting_registry(path: Path, planned: dict[str, Any]) -> None:
    write_json(path, planned)


def _update_starting_registry_entry(
    path: Path,
    *,
    name: str,
    instance_id: str,
    pid: int | None,
) -> None:
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise NodeRuntimeError("Malformed local network starting registry")
    updated_nodes: list[dict[str, Any]] = []
    for entry in data["nodes"]:
        if not isinstance(entry, dict):
            raise NodeRuntimeError("Malformed local network starting registry entry")
        if entry.get("name") == name:
            updated = dict(entry)
            updated["instance_id"] = instance_id
            updated["pid"] = pid
            updated_nodes.append(updated)
        else:
            updated_nodes.append(entry)
    write_json(path, {"nodes": updated_nodes})


def _rollback_started_children(
    started_children: list[StartedChild],
    *,
    starting_registry: Path,
    planned_nodes: list[dict[str, Any]],
    original_error: Exception,
) -> None:
    recovery_nodes: list[dict[str, Any]] = []

    for child in started_children:
        store = DataStore(child.data_dir)
        stop_failure: str | None = None
        current = node_status(child.data_dir)

        if current.state == "running_verified":
            try:
                stop_node(child.data_dir)
            except NodeRuntimeError as exc:
                stop_failure = str(exc)

        if child.popen.poll() is None:
            _terminate_subprocess(child.popen, store)
            _wait_for_child_exit(child.popen)

        current = node_status(child.data_dir)
        popen_live = child.popen.poll() is None
        parent_pid = child.popen.pid
        if popen_live:
            pid_live = True
        else:
            # Parent-held Popen confirms this spawn exited; only treat a different
            # tracked PID as live (orphan / pid reuse on the same path).
            pid_live = current.pid_is_live and (
                current.pid is not None and current.pid != parent_pid
            )

        if pid_live or popen_live:
            recovery_nodes.append(
                {
                    "name": child.name,
                    "port": child.port,
                    "instance_id": child.instance_id,
                    "pid": current.pid if current.pid is not None else child.popen.pid,
                    "state": current.state,
                    "message": current.message,
                    "stop_failure": stop_failure,
                }
            )
            continue

        try:
            _cleanup_dead_lifecycle_files(store, child=child.popen)
        except NodeRuntimeError:
            pass

    if recovery_nodes:
        write_json(
            starting_registry,
            {
                "nodes": recovery_nodes,
                "planned_nodes": planned_nodes,
                "stop_failures": [
                    entry["stop_failure"]
                    for entry in recovery_nodes
                    if entry.get("stop_failure")
                ],
                "startup_error": str(original_error),
            },
        )
        raise NodeRuntimeError(
            "Local network startup failed and live children remain; "
            f"recovery registry preserved at {starting_registry}"
        ) from original_error


def run_local_network(
    base_dir: str | Path,
    nodes: int,
    base_port: int = 9001,
) -> list[ProcessStatus]:
    _validate_network_ports(base_port, nodes)
    root = _network_root(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    registry = root / "local-network.json"
    starting_registry = root / "local-network.starting.json"
    planned_nodes = [
        {
            "name": f"node{index + 1}",
            "port": base_port + index,
            "instance_id": None,
            "pid": None,
        }
        for index in range(nodes)
    ]
    started_children: list[StartedChild] = []
    try:
        _write_starting_registry(starting_registry, {"nodes": planned_nodes})
        for index in range(nodes):
            node_name = f"node{index + 1}"
            node_port = base_port + index
            started = start_node_with_handle(
                root / node_name,
                node_port,
                node_name=node_name,
            )
            started_children.append(started)
            _update_starting_registry_entry(
                starting_registry,
                name=node_name,
                instance_id=started.instance_id,
                pid=started.status.pid,
            )
        write_json(
            registry,
            {
                "nodes": [
                    {"name": f"node{index + 1}", "port": base_port + index}
                    for index in range(nodes)
                ],
            },
        )
        starting_registry.unlink(missing_ok=True)
        return [child.status for child in started_children]
    except Exception as exc:
        registry.unlink(missing_ok=True)
        try:
            _rollback_started_children(
                started_children,
                starting_registry=starting_registry,
                planned_nodes=planned_nodes,
                original_error=exc,
            )
        except NodeRuntimeError:
            raise
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
