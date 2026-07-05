"""Verify that a PID belongs to an expected Toychain node process."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import LIFECYCLE_SCHEMA_VERSION, READINESS_SCHEMA_VERSION
from .errors import CodecError, NodeRuntimeError, PersistenceError
from .json_validation import (
    persistence_schema_version,
    reject_unknown_keys,
    strict_int,
    strict_str,
    validate_json_schema,
)
from .persistence import DataStore, read_json, write_json

_LIFECYCLE_KEYS = frozenset(
    {
        "schema_version",
        "pid",
        "instance_id",
        "started_at",
        "process_start_token",
        "data_dir",
        "executable",
    }
)


_READINESS_KEYS = frozenset(
    {
        "schema_version",
        "instance_id",
        "pid",
        "data_dir",
        "port",
        "ready_at",
    }
)


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        success = ctypes.windll.kernel32.GetExitCodeProcess(
            handle, ctypes.byref(exit_code)
        )
        ctypes.windll.kernel32.CloseHandle(handle)
        if not success:
            return False
        return exit_code.value == still_active

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="ascii").split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except OSError:
        pass

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
    process_start_token: int
    data_dir: str
    executable: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "instance_id": self.instance_id,
            "started_at": self.started_at,
            "process_start_token": self.process_start_token,
            "data_dir": self.data_dir,
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeLifecycle":
        try:
            validate_json_schema(data, "node-lifecycle")
            reject_unknown_keys(data, _LIFECYCLE_KEYS, "node lifecycle")
            persistence_schema_version(data["schema_version"])
            data_dir = strict_str(data["data_dir"], "data_dir")
            executable = strict_str(data["executable"], "executable")
            if not data_dir.strip():
                raise CodecError("data_dir must not be empty")
            if not executable.strip():
                raise CodecError("executable must not be empty")
            return cls(
                schema_version=strict_int(data["schema_version"], "schema_version"),
                pid=strict_int(data["pid"], "pid"),
                instance_id=strict_str(data["instance_id"], "instance_id"),
                started_at=strict_int(data["started_at"], "started_at"),
                process_start_token=strict_int(
                    data["process_start_token"], "process_start_token"
                ),
                data_dir=data_dir,
                executable=executable,
            )
        except (KeyError, TypeError, ValueError, CodecError) as exc:
            raise PersistenceError("Malformed node lifecycle file") from exc


def new_instance_id() -> str:
    import uuid

    return str(uuid.uuid4())


def write_lifecycle(path: Path, lifecycle: NodeLifecycle) -> None:
    payload = lifecycle.to_dict()
    validate_json_schema(payload, "node-lifecycle")
    reject_unknown_keys(payload, _LIFECYCLE_KEYS, "node lifecycle")
    write_json(path, payload)


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
            validate_json_schema(data, "node-readiness")
            reject_unknown_keys(data, _READINESS_KEYS, "node readiness")
            persistence_schema_version(data["schema_version"])
            data_dir = strict_str(data["data_dir"], "data_dir")
            if not data_dir.strip():
                raise CodecError("data_dir must not be empty")
            return cls(
                schema_version=strict_int(data["schema_version"], "schema_version"),
                instance_id=strict_str(data["instance_id"], "instance_id"),
                pid=strict_int(data["pid"], "pid"),
                data_dir=data_dir,
                port=strict_int(data["port"], "port"),
                ready_at=strict_int(data["ready_at"], "ready_at"),
            )
        except (KeyError, TypeError, ValueError, CodecError) as exc:
            raise PersistenceError("Malformed node readiness file") from exc


def write_readiness(path: Path, readiness: NodeReadiness) -> None:
    payload = readiness.to_dict()
    validate_json_schema(payload, "node-readiness")
    reject_unknown_keys(payload, _READINESS_KEYS, "node readiness")
    write_json(path, payload)


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
    pid = _read_pid_file(store.pid_path)
    if pid is not None and process_is_running(pid):
        raise NodeRuntimeError(
            f"Refusing to remove lifecycle files while PID {pid} is still alive"
        )
    for path in reversed(paths):
        path.unlink(missing_ok=True)
    store.config_path.with_suffix(store.config_path.suffix + ".tmp").unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)


def _read_pid_file(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _normalized_executable(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def read_process_start_token(pid: int | None = None) -> int:
    target = os.getpid() if pid is None else pid
    if os.name == "nt":
        _executable, token = _windows_process_info(target)
    else:
        token = _linux_process_start_time(target)
    if token is None:
        raise NodeRuntimeError(
            f"Could not read process start token for PID {target}"
        )
    return token


def _linux_process_start_time(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
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


def _linux_executable(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
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
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, image, ctypes.byref(size)
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


def _identity_refusal(pid: int) -> NodeRuntimeError:
    return NodeRuntimeError(
        f"Process identity could not be verified; refusing to signal PID {pid}"
    )


def _command_line_has_instance_id(command_line: str, instance_id: str) -> bool:
    return (
        f"--instance-id {instance_id}" in command_line
        or f"--instance-id={instance_id}" in command_line
    )


def _verify_command_line(command_line: str, lifecycle: NodeLifecycle, store: DataStore) -> None:
    lowered = command_line.lower()
    if "toychain" not in lowered and "python" not in lowered:
        raise NodeRuntimeError("Process command line is not a Toychain node")
    if "_node-run" not in command_line:
        raise NodeRuntimeError("Process command line is not a Toychain node")
    data_dir = str(store.data_dir)
    if data_dir not in command_line and str(store.data_dir) not in command_line:
        raise NodeRuntimeError("Process command line does not reference this data directory")
    if not _command_line_has_instance_id(command_line, lifecycle.instance_id):
        raise NodeRuntimeError("Process command line does not match lifecycle instance_id")


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
        executable, process_start_token = _windows_process_info(pid)
        command_line = _windows_command_line(pid)
    else:
        executable = _linux_executable(pid)
        command_line = _linux_command_line(pid)
        process_start_token = _linux_process_start_time(pid)

    if executable is None:
        raise _identity_refusal(pid)
    if _normalized_executable(executable) != _normalized_executable(lifecycle.executable):
        raise NodeRuntimeError("Process executable does not match lifecycle record")

    if command_line is None:
        raise _identity_refusal(pid)
    _verify_command_line(command_line, lifecycle, store)

    if process_start_token is None:
        raise _identity_refusal(pid)
    if process_start_token != lifecycle.process_start_token:
        raise NodeRuntimeError("Process start token does not match lifecycle record")


def cleanup_stale_node_files(store: DataStore, *, force: bool = False) -> None:
    pid = _read_pid_file(store.pid_path)
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
            else:
                raise NodeRuntimeError(
                    f"Refusing to clean lifecycle files for active Toychain node PID {pid}"
                )
        elif not force:
            raise NodeRuntimeError(
                f"Refusing to clean stale files while PID {pid} is alive without lifecycle identity"
            )
    store.pid_path.unlink(missing_ok=True)
    store.lock_path.unlink(missing_ok=True)
    store.stop_path.unlink(missing_ok=True)
    store.lifecycle_path.unlink(missing_ok=True)
    store.ready_path.unlink(missing_ok=True)
