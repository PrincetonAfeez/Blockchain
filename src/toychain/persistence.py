"""Persistence-related functionality."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chain import Blockchain
from .crypto import KeyPair, address_from_public_key, public_key_from_private
from .errors import CodecError, ConsensusError, PersistenceError
from .mempool import Mempool
from .models import Block, Transaction


def _is_block_hash(value: Any) -> bool:
    """True if value is a 32-byte SHA-256 hash as 64 lowercase hex characters."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return value == value.lower()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PersistenceError(f"File does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"Could not read JSON file {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(text, encoding=encoding)
        os.replace(temporary, path)
    except OSError as exc:
        raise PersistenceError(f"Could not write file {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


@dataclass(frozen=True, slots=True)
class Wallet:
    private_key: bytes
    public_key: bytes
    address: str

    @classmethod
    def from_keypair(cls, keypair: KeyPair) -> "Wallet":
        return cls(keypair.private_key, keypair.public_key, keypair.address)

    def to_dict(self, *, include_private: bool = True) -> dict[str, str]:
        data = {
            "address": self.address,
            "public_key": base64.b64encode(self.public_key).decode("ascii"),
        }
        if include_private:
            data["private_key"] = base64.b64encode(self.private_key).decode("ascii")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Wallet":
        try:
            private_key = base64.b64decode(data["private_key"], validate=True)
            public_key = base64.b64decode(data["public_key"], validate=True)
            address = str(data["address"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError("Malformed wallet file") from exc
        if public_key_from_private(private_key) != public_key:
            raise PersistenceError("Wallet private and public keys do not match")
        if address_from_public_key(public_key) != address:
            raise PersistenceError("Wallet address does not match its public key")
        return cls(private_key, public_key, address)


class DataStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.chain_dir = self.data_dir / "chain"
        self.blocks_dir = self.chain_dir / "blocks"
        self.mempool_dir = self.data_dir / "mempool"
        self.wallet_path = self.data_dir / "wallet.json"
        self.index_path = self.chain_dir / "index.json"
        self.tip_path = self.chain_dir / "canonical_tip.txt"
        self.mempool_path = self.mempool_dir / "transactions.json"
        self.config_path = self.data_dir / "config.json"
        self.pid_path = self.data_dir / "node.pid"
        self.lock_path = self.data_dir / "node.lock"
        self.writelock_path = self.data_dir / "node.writelock"
        self.stop_path = self.data_dir / "node.stop"
        self.log_path = self.data_dir / "node.log"

    def initialize(self) -> None:
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        self.mempool_dir.mkdir(parents=True, exist_ok=True)

    def save_wallet(self, wallet: Wallet) -> None:
        if self.wallet_path.exists():
            raise PersistenceError(f"Wallet already exists: {self.wallet_path}")
        write_json(self.wallet_path, wallet.to_dict(include_private=True))
        try:
            self.wallet_path.chmod(0o600)
        except OSError:
            pass

    def load_wallet(self) -> Wallet:
        if not self.wallet_path.exists():
            raise PersistenceError(
                "No wallet in this data directory; run 'create-wallet' first"
            )
        data = read_json(self.wallet_path)
        if not isinstance(data, dict):
            raise PersistenceError("Wallet file must contain a JSON object")
        return Wallet.from_dict(data)

    def save_chain(self, chain: Blockchain) -> None:
        self.initialize()
        for block_hash, block in chain.blocks.items():
            write_json(self.blocks_dir / f"{block_hash}.json", block.to_dict())
        # The tip is derived from the block set on load, so it is not stored in
        # the index. canonical_tip.txt is a single human-readable mirror.
        write_json(
            self.index_path,
            {
                "blocks": {
                    block_hash: metadata.to_dict()
                    for block_hash, metadata in chain.metadata.items()
                },
            },
        )
        write_text_atomic(self.tip_path, chain.tip_hash + "\n", encoding="ascii")

    def load_chain(self, *, persist: bool = True) -> Blockchain:
        if not self.index_path.exists():
            chain = Blockchain()
            if persist:
                self.save_chain(chain)
            return chain
        index = read_json(self.index_path)
        if not isinstance(index, dict) or not isinstance(index.get("blocks"), dict):
            raise PersistenceError("Malformed chain index")
        blocks: list[Block] = []
        try:
            for block_hash in index["blocks"]:
                # Reject anything that is not a hex hash before building a path,
                # so a tampered index cannot traverse outside blocks/ on read.
                if not _is_block_hash(block_hash):
                    raise PersistenceError(f"Invalid block hash in index: {block_hash!r}")
                data = read_json(self.blocks_dir / f"{block_hash}.json")
                if not isinstance(data, dict):
                    raise PersistenceError(f"Block {block_hash} is not a JSON object")
                block = Block.from_dict(data)
                if block.hash != block_hash:
                    raise PersistenceError(f"Block filename/hash mismatch: {block_hash}")
                blocks.append(block)
            # The canonical tip is derived from the validated block set by
            # deterministic fork choice; index.json's canonical_tip and
            # canonical_tip.txt are human-readable mirrors, not trusted inputs
            # (avoiding a read-during-flush race). Trusting the node's own
            # persisted store keeps loads cheap; validate-chain re-verifies the
            # whole chain from genesis.
            return Blockchain.from_blocks(blocks, validate=False)
        except (CodecError, ConsensusError, KeyError) as exc:
            raise PersistenceError(f"Could not load chain: {exc}") from exc

    def save_mempool(self, mempool: Mempool) -> None:
        self.initialize()
        write_json(
            self.mempool_path,
            {"transactions": [tx.to_dict() for tx in mempool.transactions()]},
        )

    def load_mempool(self) -> Mempool:
        if not self.mempool_path.exists():
            return Mempool()
        data = read_json(self.mempool_path)
        if not isinstance(data, dict) or not isinstance(data.get("transactions"), list):
            raise PersistenceError("Malformed mempool file")
        try:
            return Mempool(Transaction.from_dict(tx) for tx in data["transactions"])
        except CodecError as exc:
            raise PersistenceError(f"Could not load mempool: {exc}") from exc

    def export_block(self, block: Block, output: str | Path) -> None:
        write_json(Path(output), block.to_dict())

    def import_block_file(self, path: str | Path) -> Block:
        data = read_json(Path(path))
        if not isinstance(data, dict):
            raise PersistenceError("Block import must contain a JSON object")
        return Block.from_dict(data)

    def export_transaction(self, transaction: Transaction, output: str | Path) -> None:
        write_json(Path(output), transaction.to_dict())

    def import_transaction_file(self, path: str | Path) -> Transaction:
        data = read_json(Path(path))
        if not isinstance(data, dict):
            raise PersistenceError("Transaction import must contain a JSON object")
        return Transaction.from_dict(data)
