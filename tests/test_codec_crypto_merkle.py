"""Tests for the codec, crypto, and merkle functionality."""

from __future__ import annotations

from dataclasses import replace

import pytest

from toychain.block import GENESIS_BLOCK, GENESIS_HASH
from toychain.codec import (
    encode_block,
    encode_block_header,
    encode_transaction,
    parse_block,
    parse_block_header,
    parse_transaction,
)
from toychain.constants import FORMAT_VERSION
from toychain.crypto import address_from_public_key
from toychain.errors import CodecError, ValidationError
from toychain.merkle import (
    build_merkle_root,
    create_merkle_proof,
    verify_merkle_proof,
)
from toychain.models import MerkleProof, MerkleProofStep
from toychain.transactions import (
    create_signed_transaction,
    validate_transaction_authenticity,
)


def signed_tx(alice, bob):
    return create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=7,
        nonce=0,
    )


def test_key_generation_and_address_are_stable(alice):
    assert len(alice.private_key) == 32
    assert len(alice.public_key) == 32
    assert address_from_public_key(alice.public_key) == alice.address


def test_signature_verifies_and_tampering_fails(alice, bob):
    transaction = signed_tx(alice, bob)
    validate_transaction_authenticity(transaction)
    with pytest.raises(ValidationError, match="signature"):
        validate_transaction_authenticity(replace(transaction, amount=8))
    with pytest.raises(ValidationError, match="sender address"):
        validate_transaction_authenticity(
            replace(transaction, public_key=bob.public_key)
        )


def test_transaction_codec_is_canonical_and_strict(alice, bob):
    transaction = signed_tx(alice, bob)
    first = encode_transaction(transaction)
    second = encode_transaction(transaction)
    assert first == second
    assert parse_transaction(first) == transaction

    with pytest.raises(CodecError, match="Truncated"):
        parse_transaction(first[:-1])
    with pytest.raises(CodecError, match="extra"):
        parse_transaction(first + b"\x00")
    wrong_version = bytearray(first)
    wrong_version[3] = FORMAT_VERSION + 1
    with pytest.raises(CodecError, match="Unsupported"):
        parse_transaction(bytes(wrong_version))
    with pytest.raises(CodecError, match="magic"):
        parse_transaction(b"BAD" + first[3:])


def test_block_and_header_codecs_are_canonical_and_strict():
    assert GENESIS_HASH == "f3171adea945753f0fda17639ec319e839e33f7a52a53777fb185d92c1f5cf22"
    header_bytes = encode_block_header(GENESIS_BLOCK.header)
    assert header_bytes == encode_block_header(GENESIS_BLOCK.header)
    assert parse_block_header(header_bytes) == GENESIS_BLOCK.header
    block_bytes = encode_block(GENESIS_BLOCK)
    assert parse_block(block_bytes) == GENESIS_BLOCK
    with pytest.raises(CodecError, match="Truncated"):
        parse_block(block_bytes[:-2])
    with pytest.raises(CodecError, match="extra"):
        parse_block(block_bytes + b"x")


def test_merkle_root_odd_leaf_rule_and_proofs(alice, bob):
    transactions = [
        signed_tx(alice, bob),
        create_signed_transaction(
            private_key=alice.private_key,
            public_key=alice.public_key,
            sender=alice.address,
            recipient=bob.address,
            amount=8,
            nonce=1,
        ),
        create_signed_transaction(
            private_key=alice.private_key,
            public_key=alice.public_key,
            sender=alice.address,
            recipient=bob.address,
            amount=9,
            nonce=2,
        ),
    ]
    tx_ids = tuple(tx.tx_id_bytes() for tx in transactions)
    assert build_merkle_root(tx_ids) == build_merkle_root(tx_ids)
    proof = create_merkle_proof(tx_ids, 2)
    assert len(proof.steps) == 2
    assert verify_merkle_proof(proof)

    bad_hash = replace(
        proof,
        steps=(
            MerkleProofStep(b"\xff" * 32, proof.steps[0].sibling_on_left),
            *proof.steps[1:],
        ),
    )
    assert not verify_merkle_proof(bad_hash)
    assert not verify_merkle_proof(replace(proof, transaction_index=1))
    assert not verify_merkle_proof(replace(proof, tx_id=b"\x00" * 32))


def test_empty_merkle_tree_is_rejected():
    with pytest.raises(ValidationError, match="no transactions"):
        build_merkle_root(())


def test_transaction_from_dict_rejects_non_integer_amount(alice, bob):
    from toychain.models import Transaction

    base = signed_tx(alice, bob).to_dict()
    for bad in (5.9, "5", True):
        with pytest.raises(CodecError, match="amount must be an integer"):
            Transaction.from_dict({**base, "amount": bad})


def test_header_and_proof_reject_non_integer_fields():
    from toychain.models import BlockHeader, MerkleProof

    header = GENESIS_BLOCK.header.to_dict()
    with pytest.raises(CodecError, match="timestamp must be an integer"):
        BlockHeader.from_dict({**header, "timestamp": 1700000000.5})
    with pytest.raises(CodecError, match="difficulty_bits must be an integer"):
        BlockHeader.from_dict({**header, "difficulty_bits": True})

    proof = {
        "root": "00" * 32, "tx_id": "11" * 32,
        "transaction_index": 0, "transaction_count": 1, "steps": [],
    }
    with pytest.raises(CodecError, match="transaction_index must be an integer"):
        MerkleProof.from_dict({**proof, "transaction_index": 1.0})


def test_block_encode_rejects_too_many_transactions(monkeypatch):
    import toychain.codec as codec
    from toychain.models import Block

    monkeypatch.setattr(codec, "MAX_TRANSACTIONS", 0)
    with pytest.raises(CodecError, match="too large"):
        codec.encode_block(Block(header=GENESIS_BLOCK.header, transactions=GENESIS_BLOCK.transactions))
