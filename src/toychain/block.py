"""Block-related functionality."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from .constants import (
    BLOCK_HEADER_DOMAIN,
    COINBASE_EXTRANONCE_BYTES,
    DEFAULT_DIFFICULTY_BITS,
    FORMAT_VERSION,
    GENESIS_RECIPIENT,
    GENESIS_TIMESTAMP,
    MAX_DIFFICULTY_BITS,
    ZERO_HASH,
)
from .crypto import is_valid_address, sha256
from .errors import ValidationError
from .merkle import build_merkle_root
from .models import Block, BlockHeader, Transaction
from .transactions import create_coinbase


@dataclass(frozen=True, slots=True)
class MiningStats:
    attempts: int
    elapsed_seconds: float
    hashes_per_second: float
    transaction_count: int
    merkle_root: str
    block_hash: str
    nonce: int

    def to_dict(self) -> dict[str, int | float | str]:
        return {
            "attempts": self.attempts,
            "elapsed_seconds": self.elapsed_seconds,
            "hashes_per_second": self.hashes_per_second,
            "transaction_count": self.transaction_count,
            "merkle_root": self.merkle_root,
            "block_hash": self.block_hash,
            "nonce": self.nonce,
        }


def block_header_hash(header: BlockHeader) -> bytes:
    return sha256(BLOCK_HEADER_DOMAIN + header.canonical_bytes())


def meets_difficulty(hash_bytes: bytes, difficulty_bits: int) -> bool:
    if len(hash_bytes) != 32:
        return False
    if not 0 <= difficulty_bits <= MAX_DIFFICULTY_BITS:
        return False
    value = int.from_bytes(hash_bytes, "big")
    target = 1 << (256 - difficulty_bits) if difficulty_bits else 1 << 256
    return value < target


def block_work(block: Block) -> int:
    return 1 << block.header.difficulty_bits


def mine_block(candidate: Block) -> tuple[Block, MiningStats]:
    difficulty = candidate.header.difficulty_bits
    if not 0 <= difficulty <= MAX_DIFFICULTY_BITS:
        raise ValidationError(
            f"Difficulty must be between 0 and {MAX_DIFFICULTY_BITS} bits"
        )
    start = time.perf_counter()
    attempts = 0
    nonce = candidate.header.nonce
    while nonce <= 0xFFFFFFFFFFFFFFFF:
        attempts += 1
        header = replace(candidate.header, nonce=nonce)
        digest = block_header_hash(header)
        if meets_difficulty(digest, difficulty):
            elapsed = time.perf_counter() - start
            mined = replace(candidate, header=header)
            stats = MiningStats(
                attempts=attempts,
                elapsed_seconds=elapsed,
                hashes_per_second=attempts / elapsed if elapsed else float("inf"),
                transaction_count=len(mined.transactions),
                merkle_root=mined.header.merkle_root.hex(),
                block_hash=digest.hex(),
                nonce=nonce,
            )
            return mined, stats
        nonce += 1
    raise ValidationError("Nonce space exhausted without finding valid proof of work")


def make_block_candidate(
    *,
    previous_hash: bytes,
    miner_address: str,
    height: int,
    transactions: list[Transaction] | tuple[Transaction, ...] = (),
    difficulty_bits: int = DEFAULT_DIFFICULTY_BITS,
    timestamp: int | None = None,
    allow_genesis_recipient: bool = False,
) -> Block:
    if miner_address == GENESIS_RECIPIENT:
        if not allow_genesis_recipient:
            raise ValidationError("Miner is not a valid toychain address.")
    elif not is_valid_address(miner_address):
        raise ValidationError("Miner is not a valid toychain address.")
    if timestamp is not None and not 0 <= timestamp < 2**64:
        raise ValidationError("Block timestamp must fit in an unsigned 64-bit integer")
    resolved_timestamp = int(time.time()) if timestamp is None else timestamp
    # Bind the coinbase extranonce to the parent hash and timestamp so competing
    # same-height blocks get distinct coinbase transaction ids. Different parents
    # or timestamps no longer collide; only re-mining identical block content can.
    extranonce = sha256(resolved_timestamp.to_bytes(8, "big") + previous_hash)[
        :COINBASE_EXTRANONCE_BYTES
    ]
    coinbase = create_coinbase(miner_address, height, extranonce=extranonce)
    all_transactions = (coinbase, *transactions)
    root = build_merkle_root(tuple(tx.tx_id_bytes() for tx in all_transactions))
    header = BlockHeader(
        version=FORMAT_VERSION,
        previous_hash=previous_hash,
        merkle_root=root,
        timestamp=resolved_timestamp,
        difficulty_bits=difficulty_bits,
        nonce=0,
    )
    return Block(header=header, transactions=tuple(all_transactions))


def create_genesis_block() -> Block:
    candidate = make_block_candidate(
        previous_hash=ZERO_HASH,
        miner_address=GENESIS_RECIPIENT,
        height=0,
        transactions=(),
        difficulty_bits=0,
        timestamp=GENESIS_TIMESTAMP,
        allow_genesis_recipient=True,
    )
    mined, _ = mine_block(candidate)
    return mined


GENESIS_BLOCK = create_genesis_block()
GENESIS_HASH = GENESIS_BLOCK.hash

