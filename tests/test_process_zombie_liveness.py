"""Regression tests for POSIX zombie handling in process liveness checks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from toychain.errors import NodeRuntimeError
from toychain.process import dismiss_local_network_registry, run_local_network, stop_local_network
from toychain.process_identity import process_is_running
from tests.kill_helpers import kill_and_reap_pid, kill_without_reap

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX zombie semantics")


def test_zombie_pid_is_not_running() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import os; os._exit(0)"],
        start_new_session=True,
    )
    zombie_pid = child.pid
    time.sleep(0.05)
    try:
        assert process_is_running(zombie_pid) is False
    finally:
        os.waitpid(zombie_pid, 0)


def test_killed_child_is_reaped_before_liveness_check() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        start_new_session=True,
    )
    pid = child.pid
    assert process_is_running(pid) is True
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    assert process_is_running(pid) is False


def test_zombie_does_not_block_dismiss_registry(tmp_path) -> None:
    root = tmp_path / "network"
    run_local_network(root, nodes=1, base_port=9970)
    pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    kill_without_reap(pid)
    time.sleep(0.05)
    assert process_is_running(pid) is False
    dismiss_local_network_registry(root)
    os.waitpid(pid, 0)


def test_zombie_does_not_block_stop_local_network(tmp_path) -> None:
    root = tmp_path / "network"
    run_local_network(root, nodes=1, base_port=9972)
    pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    kill_without_reap(pid)
    time.sleep(0.05)
    assert process_is_running(pid) is False
    stop_local_network(root)
    os.waitpid(pid, 0)


def test_live_pid_blocks_dismiss_registry(tmp_path) -> None:
    root = tmp_path / "network"
    run_local_network(root, nodes=1, base_port=9971)
    try:
        with pytest.raises(NodeRuntimeError, match="live PIDs remain"):
            dismiss_local_network_registry(root)
    finally:
        stop_local_network(root)


def test_unrelated_live_pid_blocks_dismiss_registry(tmp_path) -> None:
    root = tmp_path / "network"
    run_local_network(root, nodes=1, base_port=9973)
    node_pid = int((root / "node1" / "node.pid").read_text(encoding="ascii"))
    kill_and_reap_pid(node_pid)

    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)"],
        start_new_session=True,
    )
    try:
        (root / "node1" / "node.pid").write_text(str(unrelated.pid), encoding="ascii")
        assert process_is_running(unrelated.pid) is True
        with pytest.raises(NodeRuntimeError, match="live PIDs remain"):
            dismiss_local_network_registry(root)
    finally:
        kill_and_reap_pid(unrelated.pid)
        dismiss_local_network_registry(root)
