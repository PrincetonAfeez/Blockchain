"""Tests for JSON schemas and strict model loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toychain.block import GENESIS_BLOCK
from toychain.errors import CodecError, PersistenceError
from toychain.json_validation import validate_json_schema
from toychain.merkle import create_merkle_proof
from toychain.models import Block, BlockHeader, MerkleProof, Transaction
from toychain.persistence import DataStore, Wallet
from toychain.transactions import create_signed_transaction


def _signed_tx_dict(alice, bob):
    return create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=7,
        nonce=0,
    ).to_dict()


@pytest.mark.parametrize(
    ("schema_name", "payload"),
    [
        ("transaction", lambda alice, bob: _signed_tx_dict(alice, bob)),
        ("block-header", lambda _a, _b: GENESIS_BLOCK.header.to_dict()),
        ("block", lambda _a, _b: GENESIS_BLOCK.to_dict()),
        (
            "merkle-proof",
            lambda alice, bob: create_merkle_proof(
                (
                    create_signed_transaction(
                        private_key=alice.private_key,
                        public_key=alice.public_key,
                        sender=alice.address,
                        recipient=bob.address,
                        amount=1,
                        nonce=0,
                    ).tx_id_bytes(),
                ),
                0,
            ).to_dict(),
        ),
    ],
)
def test_to_dict_output_validates_against_schema(schema_name, payload, alice, bob):
    validate_json_schema(payload(alice, bob), schema_name)


def test_example_files_validate_against_schemas():
    examples = Path(__file__).resolve().parents[1] / "schema" / "examples"
    validate_json_schema(
        json.loads((examples / "transaction.example.json").read_text(encoding="utf-8")),
        "transaction",
    )
    validate_json_schema(
        json.loads((examples / "block-header.example.json").read_text(encoding="utf-8")),
        "block-header",
    )
    validate_json_schema(
        json.loads((examples / "block.example.json").read_text(encoding="utf-8")),
        "block",
    )


def test_transaction_from_dict_rejects_non_string_sender(alice, bob):
    base = _signed_tx_dict(alice, bob)
    with pytest.raises(CodecError, match="string"):
        Transaction.from_dict({**base, "sender": 123})


def test_transaction_from_dict_rejects_boolean_amount(alice, bob):
    base = _signed_tx_dict(alice, bob)
    with pytest.raises(CodecError, match="integer"):
        Transaction.from_dict({**base, "amount": True})


def test_transaction_from_dict_rejects_unknown_properties(alice, bob):
    base = _signed_tx_dict(alice, bob)
    with pytest.raises(CodecError, match="(Unknown transaction properties|Additional properties)"):
        Transaction.from_dict({**base, "extra": "nope"})


def test_transaction_from_dict_rejects_unsupported_version(alice, bob):
    base = _signed_tx_dict(alice, bob)
    with pytest.raises(CodecError, match="(Unsupported version|JSON schema validation failed)"):
        Transaction.from_dict({**base, "version": 2})


def test_transaction_from_dict_rejects_short_public_key(alice, bob):
    base = _signed_tx_dict(alice, bob)
    with pytest.raises(CodecError, match="(public_key|JSON schema validation failed)"):
        Transaction.from_dict({**base, "public_key": "ab"})


def test_transaction_from_dict_rejects_uppercase_hex(alice, bob):
    base = _signed_tx_dict(alice, bob)
    with pytest.raises(CodecError, match="(lowercase hexadecimal|JSON schema validation failed)"):
        Transaction.from_dict({**base, "signature": "A" * 128})


def test_transaction_from_dict_rejects_missing_required_field(alice, bob):
    base = _signed_tx_dict(alice, bob)
    del base["nonce"]
    with pytest.raises(CodecError, match="(Malformed transaction object|JSON schema validation failed)"):
        Transaction.from_dict(base)


def test_block_header_from_dict_rejects_boolean_difficulty():
    header = GENESIS_BLOCK.header.to_dict()
    with pytest.raises(CodecError, match="integer"):
        BlockHeader.from_dict({**header, "difficulty_bits": True})


def test_block_header_from_dict_rejects_short_hash():
    header = GENESIS_BLOCK.header.to_dict()
    with pytest.raises(CodecError, match="(exactly 32 bytes|JSON schema validation failed)"):
        BlockHeader.from_dict({**header, "previous_hash": "00" * 16})


def test_block_header_from_dict_rejects_malformed_hex():
    header = GENESIS_BLOCK.header.to_dict()
    with pytest.raises(CodecError, match="(not valid hexadecimal|JSON schema validation failed)"):
        BlockHeader.from_dict({**header, "merkle_root": "not-hex"})


def test_block_from_dict_rejects_mismatched_hash():
    payload = GENESIS_BLOCK.to_dict()
    payload["hash"] = "00" * 64
    with pytest.raises(CodecError, match="does not match"):
        Block.from_dict(payload)


def test_merkle_proof_from_dict_rejects_non_boolean_step_flag():
    proof = {
        "root": "00" * 32,
        "tx_id": "11" * 32,
        "transaction_index": 0,
        "transaction_count": 1,
        "steps": [{"sibling": "22" * 32, "sibling_on_left": "yes"}],
    }
    with pytest.raises(CodecError, match="boolean"):
        MerkleProof.from_dict(proof)


def test_wallet_from_dict_rejects_numeric_address(alice):
    wallet = Wallet.from_keypair(alice)
    payload = {
        "schema_version": 1,
        **wallet.to_dict(include_private=True),
        "address": 123,
    }
    with pytest.raises(PersistenceError, match="Malformed wallet file"):
        Wallet.from_dict(payload)


def test_persisted_wallet_and_index_validate_against_schemas(tmp_path, alice):
    from toychain.node import Node

    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)

    wallet_payload = json.loads(node.store.wallet_path.read_text(encoding="utf-8"))
    validate_json_schema(wallet_payload, "wallet")

    index_payload = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    validate_json_schema(index_payload, "chain-index")
