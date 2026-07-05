"""Node-related functionality."""

from __future__ import annotations

import atexit
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .block import MiningStats, make_block_candidate, mine_block
from .chain import AddBlockResult, Blockchain
from .constants import DEFAULT_DIFFICULTY_BITS, LIFECYCLE_SCHEMA_VERSION, PERSISTENCE_SCHEMA_VERSION, READINESS_SCHEMA_VERSION
from .crypto import generate_keypair
from .errors import NodeRuntimeError, PersistenceError
from .mempool import Mempool, MempoolRepairReport
from .models import Block, Transaction
from .node_config import NodeConfig, save_node_config, validate_port_value
from .persistence import DataStore, Wallet
from .process import process_is_running
from .process_identity import (
    NodeLifecycle,
    NodeReadiness,
    cleanup_startup_files,
    new_instance_id,
    read_process_start_token,
    write_lifecycle,
    write_readiness,
)
from .transactions import create_signed_transaction


@dataclass(frozen=True, slots=True)
class MineResult:
    block: Block
    stats: MiningStats
    chain_result: AddBlockResult
    mempool_report: MempoolRepairReport | None


_WRITELOCK_CLEANUP_REGISTERED: set[str] = set()


def _read_lock_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _release_write_lock(path: Path) -> None:
    if _read_lock_pid(path) == os.getpid():
        path.unlink(missing_ok=True)


def _acquire_write_lock(store: DataStore) -> None:
    """Serialize state-changing CLI commands on one data directory.

    A second concurrent writer (a different, live process) is rejected so two
    commands cannot read-modify-write the same chain and lose an update. A stale
    lock from a crashed writer, or a re-open within this same process, is
    reclaimed. The running daemon uses node.lock and is handled separately.
    """
    try:
        descriptor = os.open(store.writelock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = _read_lock_pid(store.writelock_path)
        if holder is not None and holder != os.getpid() and process_is_running(holder):
            raise NodeRuntimeError(
                f"Data directory is being modified by another process (pid {holder})"
            )
        store.writelock_path.unlink(missing_ok=True)
        descriptor = os.open(store.writelock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    os.close(descriptor)
    key = str(store.writelock_path)
    if key not in _WRITELOCK_CLEANUP_REGISTERED:
        atexit.register(_release_write_lock, store.writelock_path)
        _WRITELOCK_CLEANUP_REGISTERED.add(key)


class Node:
    def __init__(
        self,
        store: DataStore,
        chain: Blockchain,
        mempool: Mempool,
        *,
        writable: bool = True,
    ) -> None:
        self.store = store
        self.chain = chain
        self.mempool = mempool
        self.writable = writable

    @classmethod
    def open(
        cls,
        data_dir: str | Path,
        *,
        writable: bool = True,
        allow_locked: bool = False,
    ) -> "Node":
        store = DataStore(data_dir)
        if writable:
            store.initialize()
        if writable and store.lock_path.exists() and not allow_locked:
            raise NodeRuntimeError(
                f"Node data directory is owned by a running process: {store.data_dir}"
            )
        if writable and not allow_locked:
            # Exclude concurrent CLI writers (the daemon is excluded above by its
            # node.lock). Released on process exit; reclaimed if stale.
            _acquire_write_lock(store)
        chain = store.load_chain(persist=writable)
        mempool = store.load_mempool()
        before = mempool.transactions()
        mempool.revalidate(chain.state)
        node = cls(store, chain, mempool, writable=writable)
        # Read-only opens never touch disk; writers persist the mempool only if
        # revalidation against the canonical state actually changed it.
        if writable and mempool.transactions() != before:
            store.save_mempool(mempool)
        return node

    def _require_writable(self) -> None:
        if not self.writable:
            raise NodeRuntimeError("Node was opened read-only")

    def flush(self) -> None:
        self._require_writable()
        self.store.save_chain(self.chain)
        self.store.save_mempool(self.mempool)

    def create_wallet(self) -> Wallet:
        self._require_writable()
        wallet = Wallet.from_keypair(generate_keypair())
        self.store.save_wallet(wallet)
        return wallet

    def wallet(self) -> Wallet:
        return self.store.load_wallet()

    def create_transaction(
        self,
        recipient: str,
        amount: int,
        *,
        submit: bool = True,
    ) -> Transaction:
        wallet = self.wallet()
        projected = self.mempool.projected_state(self.chain.state)
        nonce = projected.nonces.get(wallet.address, 0)
        transaction = create_signed_transaction(
            private_key=wallet.private_key,
            public_key=wallet.public_key,
            sender=wallet.address,
            recipient=recipient,
            amount=amount,
            nonce=nonce,
        )
        if submit:
            self.submit_transaction(transaction)
        return transaction

    def submit_transaction(self, transaction: Transaction) -> str:
        self._require_writable()
        tx_id = self.mempool.submit(transaction, self.chain.state)
        self.store.save_mempool(self.mempool)
        return tx_id

    def add_block(self, block: Block) -> tuple[AddBlockResult, MempoolRepairReport | None]:
        self._require_writable()
        # A freshly accepted tip block is subject to the future-drift bound; pass
        # the current time so add_block enforces it (replay/load pass now=None).
        result = self.chain.add_block(block, now=int(time.time()))
        report: MempoolRepairReport | None = None
        if result.reorg is not None:
            report = self.mempool.repair_after_tip_change(self.chain, result.reorg)
            if result.reorg.is_reorg:
                _append_log(
                    self.store.log_path,
                    f"reorg old_tip={result.reorg.old_tip} "
                    f"new_tip={result.reorg.new_tip} "
                    f"common_ancestor={result.reorg.common_ancestor} "
                    f"orphaned={len(result.reorg.orphaned_hashes)} "
                    f"connected={len(result.reorg.connected_hashes)}",
                )
        else:
            self.mempool.remove_confirmed(self.chain.state)
        self.flush()
        return result, report

    def mine(
        self,
        miner_address: str,
        difficulty_bits: int = DEFAULT_DIFFICULTY_BITS,
    ) -> MineResult:
        self._require_writable()
        timestamp = max(int(time.time()), self.chain.tip.header.timestamp)
        candidate = make_block_candidate(
            previous_hash=bytes.fromhex(self.chain.tip_hash),
            miner_address=miner_address,
            height=self.chain.height + 1,
            transactions=self.mempool.transactions(),
            difficulty_bits=difficulty_bits,
            timestamp=timestamp,
        )
        block, stats = mine_block(candidate)
        chain_result, mempool_report = self.add_block(block)
        return MineResult(block, stats, chain_result, mempool_report)

    def mine_pending(
        self,
        difficulty_bits: int = DEFAULT_DIFFICULTY_BITS,
    ) -> MineResult | None:
        """Mine one block of the node's own pending mempool, if any.

        Used by the running node process to do real work autonomously. Returns
        None when the node has no mempool, no wallet to pay the reward to, or
        was opened read-only.
        """
        if not self.writable or len(self.mempool) == 0:
            return None
        try:
            miner_address = self.wallet().address
        except PersistenceError:
            return None
        return self.mine(miner_address, difficulty_bits)


def _append_log(log_path: Path, message: str, *, level: str = "INFO") -> None:
    """Append one timestamped line to the node log.

    A single mechanism shared by the running daemon and by reorg events recorded
    during CLI block imports, so node.log keeps one consistent line format and
    leaves no log file handles open between writes.
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"{timestamp} {level} {message}\n")


def run_node_process(
    data_dir: str | Path,
    port: int = 0,
    *,
    instance_id: str | None = None,
) -> int:
    validate_port_value(port)
    store = DataStore(data_dir)
    store.initialize()
    if store.lock_path.exists():
        raise NodeRuntimeError(f"Node data directory is already locked: {store.data_dir}")

    resolved_instance_id = instance_id or os.environ.get("TOYCHAIN_INSTANCE_ID") or new_instance_id()
    startup_files: list[Path] = []

    try:
        try:
            descriptor = os.open(store.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise NodeRuntimeError(
                f"Node data directory is already locked: {store.data_dir}"
            ) from exc
        os.close(descriptor)
        startup_files.append(store.lock_path)
        store.stop_path.unlink(missing_ok=True)

        save_node_config(
            store.config_path,
            NodeConfig(
                schema_version=PERSISTENCE_SCHEMA_VERSION,
                data_dir=str(store.data_dir),
                port=port,
            ),
        )
        startup_files.append(store.config_path)

        node = Node.open(store.data_dir, allow_locked=True)

        lifecycle = NodeLifecycle(
            schema_version=LIFECYCLE_SCHEMA_VERSION,
            pid=os.getpid(),
            instance_id=resolved_instance_id,
            started_at=int(time.time()),
            process_start_token=read_process_start_token(),
            data_dir=str(store.data_dir),
            executable=os.path.normcase(str(Path(sys.executable).resolve())),
        )
        write_lifecycle(store.lifecycle_path, lifecycle)
        startup_files.append(store.lifecycle_path)

        store.pid_path.write_text(str(os.getpid()), encoding="ascii")
        startup_files.append(store.pid_path)

        write_readiness(
            store.ready_path,
            NodeReadiness(
                schema_version=READINESS_SCHEMA_VERSION,
                instance_id=resolved_instance_id,
                pid=os.getpid(),
                data_dir=str(store.data_dir),
                port=port,
                ready_at=int(time.time()),
            ),
        )
        startup_files.append(store.ready_path)
    except Exception:
        cleanup_startup_files(store, tuple(startup_files))
        raise

    stopping = False

    def stop_handler(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop_handler)

    try:
        _append_log(
            store.log_path,
            f"node started pid={os.getpid()} port={port} height={node.chain.height}",
        )
        heartbeat_at = time.perf_counter()
        last_error: str | None = None
        warned_no_wallet = False
        while not stopping and not store.stop_path.exists():
            try:
                mined = node.mine_pending()
                last_error = None
            except Exception as exc:  # keep the daemon alive on a mining error
                message = str(exc)
                if message != last_error:  # do not repeat the same error every tick
                    _append_log(store.log_path, f"mining error: {message}", level="WARNING")
                    last_error = message
                mined = None
            if mined is not None:
                _append_log(
                    store.log_path,
                    f"mined height={mined.chain_result.height} "
                    f"hash={mined.block.hash} txs={len(mined.block.transactions)}",
                )
                continue  # drain remaining mempool without idling
            if len(node.mempool) > 0 and not warned_no_wallet:
                try:
                    node.wallet()
                except PersistenceError:
                    _append_log(
                        store.log_path, "mining disabled: no wallet in this data directory"
                    )
                    warned_no_wallet = True
            now = time.perf_counter()
            if now - heartbeat_at >= 5.0:
                _append_log(
                    store.log_path,
                    f"heartbeat height={node.chain.height} "
                    f"mempool={len(node.mempool)} tip={node.chain.tip_hash}",
                )
                heartbeat_at = now
            time.sleep(0.25)
        node.flush()
        _append_log(store.log_path, f"node stopped cleanly height={node.chain.height}")
        return 0
    finally:
        store.pid_path.unlink(missing_ok=True)
        store.lock_path.unlink(missing_ok=True)
        store.stop_path.unlink(missing_ok=True)
        store.lifecycle_path.unlink(missing_ok=True)
        store.ready_path.unlink(missing_ok=True)
