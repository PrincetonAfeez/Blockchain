"""Tests for full-store validate-chain behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest

from toychain.block import block_work, make_block_candidate, mine_block
from toychain.chain import BlockMetadata, Blockchain
from toychain.constants import COINBASE_EXTRANONCE_BYTES
from toychain.crypto import sha256
from toychain.models import Block, BlockHeader
from toychain.merkle import build_merkle_root
from toychain.transactions import create_coinbase, create_signed_transaction


def _inject_fork_block(chain: Blockchain, block: Block) -> str:
    block_hash = block.hash
    parent_hash = block.header.previous_hash.hex()
    parent_meta = chain.metadata[parent_hash]
    chain.blocks[block_hash] = block
    chain.metadata[block_hash] = BlockMetadata(
        parent_hash=parent_hash,
        height=parent_meta.height + 1,
        cumulative_work=parent_meta.cumulative_work + block_work(block),
    )
    chain.children.setdefault(parent_hash, set()).add(block_hash)
    chain.children.setdefault(block_hash, set())
    chain._states[block_hash] = chain._states[parent_hash].copy()
    chain.index_metadata[block_hash] = chain.metadata[block_hash]
    return block_hash


def _valid_fork_block(
    chain: Blockchain,
    parent_hash: str,
    height: int,
    alice,
    *,
    difficulty_bits: int = 1,
    timestamp_offset: int = 1,
) -> Block:
    parent = chain.blocks[parent_hash]
    return mine_block(
        make_block_candidate(
            previous_hash=bytes.fromhex(parent_hash),
            miner_address=alice.address,
            height=height,
            difficulty_bits=difficulty_bits,
            timestamp=parent.header.timestamp + timestamp_offset,
        )
    )[0]


def _valid_fork_at_genesis(
    chain: Blockchain,
    alice,
    *,
    difficulty_bits: int = 1,
    timestamp_offset: int = 1,
) -> Block:
    return _valid_fork_block(
        chain,
        chain.genesis_hash,
        1,
        alice,
        difficulty_bits=difficulty_bits,
        timestamp_offset=timestamp_offset,
    )


def test_validate_all_blocks_accepts_valid_canonical_and_fork(alice):
    chain = Blockchain()
    canonical = _valid_fork_at_genesis(chain, alice, difficulty_bits=2)
    chain.add_block(canonical)
    fork = _valid_fork_at_genesis(chain, alice, timestamp_offset=2)
    _inject_fork_block(chain, fork)

    report = chain.validate_all_blocks(explain=True)
    assert report.valid
    assert report.checked_blocks == 3
    assert report.checked_canonical_blocks == 2
    assert report.checked_fork_blocks == 1


def test_validate_all_blocks_rejects_invalid_fork_merkle(alice):
    chain = Blockchain()
    chain.add_block(_valid_fork_at_genesis(chain, alice))
    fork = _valid_fork_at_genesis(chain, alice, timestamp_offset=2)
    bad_fork = mine_block(
        replace(fork, header=replace(fork.header, merkle_root=b"\xff" * 32, nonce=0))
    )[0]
    fork_hash = _inject_fork_block(chain, bad_fork)

    report = chain.validate_all_blocks()
    assert not report.valid
    assert report.invalid_block_hash == fork_hash
    assert "merkle root" in report.message.lower()


def test_validate_all_blocks_rejects_invalid_fork_pow(alice):
    chain = Blockchain()
    chain.add_block(_valid_fork_at_genesis(chain, alice))
    fork = _valid_fork_at_genesis(chain, alice, timestamp_offset=2)
    bad_fork = replace(
        fork,
        header=replace(
            fork.header,
            difficulty_bits=24,
        ),
    )
    fork_hash = _inject_fork_block(chain, bad_fork)

    report = chain.validate_all_blocks()
    assert not report.valid
    assert report.invalid_block_hash == fork_hash
    assert "proof of work" in report.message.lower()


def test_validate_all_blocks_rejects_invalid_fork_signature(alice, bob):
    chain = Blockchain()
    chain.add_block(_valid_fork_at_genesis(chain, alice))
    tx = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=5,
        nonce=0,
    )
    bad_tx = replace(tx, signature=b"\x00" * 64)
    fork = mine_block(
        make_block_candidate(
            previous_hash=bytes.fromhex(chain.tip_hash),
            miner_address=alice.address,
            height=2,
            transactions=(bad_tx,),
            difficulty_bits=1,
            timestamp=chain.tip.header.timestamp + 2,
        )
    )[0]
    fork_hash = _inject_fork_block(chain, fork)

    report = chain.validate_all_blocks()
    assert not report.valid
    assert report.invalid_block_hash == fork_hash
    assert "signature" in report.message.lower()


def test_validate_all_blocks_rejects_invalid_fork_coinbase(alice):
    chain = Blockchain()
    parent_hash = bytes.fromhex(chain.tip_hash)
    timestamp = chain.tip.header.timestamp + 1
    extranonce = sha256(timestamp.to_bytes(8, "big") + parent_hash)[
        :COINBASE_EXTRANONCE_BYTES
    ]
    coinbase = create_coinbase("arbitrary-label", 1, extranonce=extranonce)
    header = BlockHeader(
        version=1,
        previous_hash=parent_hash,
        merkle_root=build_merkle_root((coinbase.tx_id_bytes(),)),
        timestamp=timestamp,
        difficulty_bits=1,
        nonce=0,
    )
    fork = mine_block(Block(header=header, transactions=(coinbase,)))[0]
    fork_hash = _inject_fork_block(chain, fork)

    report = chain.validate_all_blocks()
    assert not report.valid
    assert report.invalid_block_hash == fork_hash


def test_validate_all_blocks_rejects_incorrect_metadata(alice):
    chain = Blockchain()
    fork = _valid_fork_at_genesis(chain, alice)
    fork_hash = _inject_fork_block(chain, fork)
    chain.index_metadata[fork_hash] = BlockMetadata(
        parent_hash=chain.genesis_hash,
        height=99,
        cumulative_work=chain.metadata[fork_hash].cumulative_work,
    )

    report = chain.validate_all_blocks()
    assert not report.valid
    assert report.invalid_block_hash == fork_hash
    assert "metadata" in report.message.lower()


def test_validate_chain_cli_reports_invalid_fork(tmp_path, alice):
    from toychain.cli import main
    from toychain.node import Node

    node = Node.open(tmp_path)
    node.mine(node.create_wallet().address, difficulty_bits=1)
    fork = _valid_fork_at_genesis(node.chain, alice)
    bad_fork = replace(fork, header=replace(fork.header, merkle_root=b"\x00" * 32))
    _inject_fork_block(node.chain, bad_fork)
    node.flush()

    assert main(["--data-dir", str(tmp_path), "validate-chain"]) == 1
