"""Verify that a PID belongs to an expected Toychain node process."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import LIFECYCLE_SCHEMA_VERSION, READINESS_SCHEMA_VERSION
from .errors import NodeRuntimeError, PersistenceError
from .persistence import DataStore, read_json, write_json


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


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


@dataclass(frozen=True, slots=True)
class NodeLifecycle:
    schema_version: int
    pid: int
    instance_id: str
    started_at: int
    data_dir: str
    executable: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "instance_id": self.instance_id,
            "started_at": self.started_at,
            "data_dir": self.data_dir,
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeLifecycle":
        try:
            return cls(
                schema_version=int(data["schema_version"]),
                pid=int(data["pid"]),
                instance_id=str(data["instance_id"]),
                started_at=int(data["started_at"]),
                data_dir=str(data["data_dir"]),
                executable=str(data["executable"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError("Malformed node lifecycle file") from exc


def new_instance_id() -> str:
    import uuid

    return str(uuid.uuid4())


def write_lifecycle(path: Path, lifecycle: NodeLifecycle) -> None:
    write_json(path, lifecycle.to_dict())


def read_lifecycle(path: Path) -> NodeLifecycle | None:
    if not path.exists():
        return None
    data = read_json(path)
    if not isinstance(data, dict):
        raise PersistenceError("Node lifecycle file must contain a JSON object")
    lifecycle = NodeLifecycle.from_dict(data)
    if lifecycle.schema_version != LIFECYCLE_SCHEMA_VERSION:
        raise PersistenceError(
            f"Unsupported node lifecycle schema version: {lifecycle.schema_version}"
        )
    return lifecycle


@dataclass(frozen=True, slots=True)
class NodeReadiness:
    schema_version: int
    instance_id: str
    pid: int
    data_dir: str
    port: int
    ready_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "data_dir": self.data_dir,
            "port": self.port,
            "ready_at": self.ready_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeReadiness":
        try:
            return cls(
                schema_version=int(data["schema_version"]),
                instance_id=str(data["instance_id"]),
                pid=int(data["pid"]),
                data_dir=str(data["data_dir"]),
                port=int(data["port"]),
                ready_at=int(data["ready_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError("Malformed node readiness file") from exc


def write_readiness(path: Path, readiness: NodeReadiness) -> None:
    write_json(path, readiness.to_dict())


def read_readiness(path: Path) -> NodeReadiness | None:
    if not path.exists():
        return None
    data = read_json(path)
    if not isinstance(data, dict):
        raise PersistenceError("Node readiness file must contain a JSON object")
    readiness = NodeReadiness.from_dict(data)
    if readiness.schema_version != READINESS_SCHEMA_VERSION:
        raise PersistenceError(
            f"Unsupported node readiness schema version: {readiness.schema_version}"
        )
    return readiness


def cleanup_startup_files(store: DataStore, paths: tuple[Path, ...]) -> None:
    for path in reversed(paths):
        path.unlink(missing_ok=True)
    store.config_path.with_suffix(store.config_path.suffix + ".tmp").unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)


def _normalized_executable(path: str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _linux_process_start_time(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm may contain spaces inside parentheses; start time is field 22 (1-indexed).
    close_paren = stat.rfind(")")
    if close_paren == -1:
        return None
    fields = stat[close_paren + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _linux_command_line(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _windows_process_info(pid: int) -> tuple[str | None, int | None]:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, pid
    )
    if not handle:
        return None, None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            created_at = None
        else:
            created_at = (creation.dwHighDateTime << 32) + creation.dwLowDateTime

        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        query_full = 0x1000
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, query_full, image, ctypes.byref(size)
        ):
            executable = image.value
        else:
            executable = None
        return executable, created_at
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _windows_command_line(pid: int) -> str | None:
    import subprocess

    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    command_line = completed.stdout.strip()
    return command_line or None


def verify_process_identity(
    pid: int,
    lifecycle: NodeLifecycle,
    store: DataStore,
) -> None:
    if pid != lifecycle.pid:
        raise NodeRuntimeError("Lifecycle PID does not match node.pid")
    if _normalized_path(lifecycle.data_dir) != _normalized_path(store.data_dir):
        raise NodeRuntimeError("Lifecycle data_dir does not match this data directory")

    if os.name == "nt":
        executable, _created_at = _windows_process_info(pid)
        command_line = _windows_command_line(pid)
    else:
        executable = None
        command_line = _linux_command_line(pid)
        _linux_process_start_time(pid)

    if executable is not None:
        if _normalized_executable(executable) != _normalized_executable(lifecycle.executable):
            raise NodeRuntimeError("Process executable does not match lifecycle record")

    if command_line is not None:
        data_dir = str(store.data_dir)
        if "toychain" not in command_line or "_node-run" not in command_line:
            raise NodeRuntimeError("Process command line is not a Toychain node")
        if data_dir not in command_line and str(store.data_dir) not in command_line:
            raise NodeRuntimeError("Process command line does not reference this data directory")


def cleanup_stale_node_files(store: DataStore, *, force: bool = False) -> None:
    pid: int | None = None
    if store.pid_path.exists():
        try:
            pid = int(store.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = None
    if pid is not None and process_is_running(pid):
        lifecycle = read_lifecycle(store.lifecycle_path)
        if lifecycle is not None:
            try:
                verify_process_identity(pid, lifecycle, store)
            except NodeRuntimeError:
                if not force:
                    raise NodeRuntimeError(
                        f"Refusing to clean stale files while PID {pid} is alive but unverified"
                    ) from None
        elif not force:
            raise NodeRuntimeError(
                f"Refusing to clean stale files while PID {pid} is alive without lifecycle identity"
            )
    store.pid_path.unlink(missing_ok=True)
    store.lock_path.unlink(missing_ok=True)
    store.stop_path.unlink(missing_ok=True)
    store.lifecycle_path.unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)
