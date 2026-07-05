"""Tests for documented operational limits and boundary behavior."""

from __future__ import annotations

import struct
import time

import pytest

from toychain.block import GENESIS_BLOCK, make_block_candidate, mine_block
from toychain.chain import Blockchain
from toychain.codec import (
    MAX_FIELD_BYTES,
    MAX_TRANSACTIONS,
    encode_block,
    encode_unsigned_transaction,
    parse_block,
    parse_unsigned_transaction,
)
from toychain.constants import MAX_DIFFICULTY_BITS, MAX_TIMESTAMP_DRIFT_SECONDS
from toychain.errors import CodecError, ValidationError
from toychain.models import Block, Transaction


def _unsigned_tx(sender: str) -> Transaction:
    return Transaction(
        sender=sender,
        recipient="tc1" + "a" * 40,
        amount=1,
        nonce=0,
        public_key=b"\x01" * 32,
    )


def test_max_field_bytes_boundary_on_encode_and_parse():
    at_limit = "a" * MAX_FIELD_BYTES
    over_limit = "a" * (MAX_FIELD_BYTES + 1)

    encoded = encode_unsigned_transaction(_unsigned_tx(at_limit))
    parsed = parse_unsigned_transaction(encoded)
    assert parsed.sender == at_limit

    with pytest.raises(CodecError, match="too large"):
        encode_unsigned_transaction(_unsigned_tx(over_limit))

    length = struct.pack(">I", MAX_FIELD_BYTES + 1)
    payload = b"TXU\x01" + length + b"x" * (MAX_FIELD_BYTES + 1)
    with pytest.raises(CodecError, match="too large"):
        parse_unsigned_transaction(payload)


def test_max_transactions_boundary_on_encode_and_parse():
    coinbase = GENESIS_BLOCK.transactions[0]
    header = GENESIS_BLOCK.header

    assert MAX_TRANSACTIONS == 100_000
    at_limit = Block(header=header, transactions=(coinbase,) * MAX_TRANSACTIONS)
    encode_block(at_limit)

    over_limit = Block(
        header=header,
        transactions=(coinbase,) * (MAX_TRANSACTIONS + 1),
    )
    with pytest.raises(CodecError, match="too large"):
        encode_block(over_limit)

    header_bytes = header.canonical_bytes()
    over_count_payload = (
        b"BLK\x01"
        + struct.pack(">I", len(header_bytes))
        + header_bytes
        + struct.pack(">I", MAX_TRANSACTIONS + 1)
    )
    with pytest.raises(CodecError, match="too large"):
        parse_block(over_count_payload)


def test_difficulty_bits_boundary_on_mine_and_acceptance(alice):
    chain = Blockchain()
    parent_ts = chain.tip.header.timestamp

    candidate_zero = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=1,
        difficulty_bits=0,
        timestamp=parent_ts + 1,
    )
    chain.add_block(mine_block(candidate_zero)[0])

    candidate_max = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=2,
        difficulty_bits=MAX_DIFFICULTY_BITS,
        timestamp=parent_ts + 2,
    )
    assert candidate_max.header.difficulty_bits == MAX_DIFFICULTY_BITS

    candidate_over = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=3,
        difficulty_bits=MAX_DIFFICULTY_BITS + 1,
        timestamp=parent_ts + 3,
    )
    with pytest.raises(ValidationError, match="between 0 and 24"):
        mine_block(candidate_over)


def test_timestamp_drift_boundary_on_fresh_acceptance(alice):
    chain = Blockchain()
    now = int(time.time())
    parent_ts = chain.tip.header.timestamp

    at_limit = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=1,
        difficulty_bits=1,
        timestamp=now + MAX_TIMESTAMP_DRIFT_SECONDS,
    )
    block_at_limit = mine_block(at_limit)[0]
    chain.add_block(block_at_limit, now=now)

    beyond = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=chain.height + 1,
        difficulty_bits=1,
        timestamp=now + MAX_TIMESTAMP_DRIFT_SECONDS + 1,
    )
    block_beyond = mine_block(beyond)[0]
    with pytest.raises(ValidationError, match="too far in the future"):
        chain.add_block(block_beyond, now=now)
