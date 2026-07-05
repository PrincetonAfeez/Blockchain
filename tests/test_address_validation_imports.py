"""Regression tests for address validation on imported transactions and blocks."""

from __future__ import annotations

import json

import pytest

from toychain.block import mine_block
from toychain.chain import Blockchain
from toychain.constants import COINBASE_EXTRANONCE_BYTES, FORMAT_VERSION
from toychain.crypto import sha256, sign
from toychain.errors import MempoolError, ValidationError
from toychain.mempool import Mempool
from toychain.merkle import build_merkle_root
from toychain.models import Block, BlockHeader, Transaction
from toychain.node import Node
from toychain.transactions import create_coinbase, signing_payload


def _signed_with_recipient(alice, recipient: str, *, amount: int = 5, nonce: int = 0) -> Transaction:
    unsigned = Transaction(
        version=FORMAT_VERSION,
        sender=alice.address,
        recipient=recipient,
        amount=amount,
        nonce=nonce,
        public_key=alice.public_key,
        signature=b"",
    )
    return unsigned.with_signature(sign(alice.private_key, signing_payload(unsigned)))


def _block_with_coinbase_recipient(chain: Blockchain, recipient: str) -> Block:
    parent = chain.tip
    parent_hash = bytes.fromhex(chain.tip_hash)
    height = chain.height + 1
    timestamp = parent.header.timestamp + 1
    extranonce = sha256(timestamp.to_bytes(8, "big") + parent_hash)[
        :COINBASE_EXTRANONCE_BYTES
    ]
    coinbase = create_coinbase(recipient, height, extranonce=extranonce)
    header = BlockHeader(
        version=FORMAT_VERSION,
        previous_hash=parent_hash,
        merkle_root=build_merkle_root((coinbase.tx_id_bytes(),)),
        timestamp=timestamp,
        difficulty_bits=1,
        nonce=0,
    )
    return mine_block(Block(header=header, transactions=(coinbase,)))[0]


@pytest.mark.parametrize(
    "recipient",
    [
        "not-an-address",
        "GENESIS",
        "tc1" + "z" * 40,
        pytest.param("tc1" + "a" * 39, id="short"),
    ],
)
def test_mempool_rejects_signed_import_with_malformed_recipient(alice, recipient):
    chain = Blockchain()
    funded = _block_with_coinbase_recipient(chain, alice.address)
    chain.add_block(funded)
    bad = _signed_with_recipient(alice, recipient)
    with pytest.raises(MempoolError, match="valid toychain address"):
        Mempool().submit(bad, chain.state)


def test_mempool_rejects_uppercase_recipient(alice, bob):
    chain = Blockchain()
    chain.add_block(_block_with_coinbase_recipient(chain, alice.address))
    bad = _signed_with_recipient(alice, bob.address.upper())
    with pytest.raises(MempoolError, match="valid toychain address"):
        Mempool().submit(bad, chain.state)


@pytest.mark.parametrize(
    "recipient",
    ["arbitrary-label", "GENESIS", "tc1" + "a" * 39],
)
def test_imported_block_rejects_malformed_coinbase_recipient(alice, recipient):
    chain = Blockchain()
    block = _block_with_coinbase_recipient(chain, recipient)
    with pytest.raises(ValidationError, match="(valid toychain address|Genesis coinbase recipient)"):
        chain.add_block(block)
    assert chain.height == 0


def test_genesis_coinbase_recipient_remains_valid():
    chain = Blockchain()
    assert chain.height == 0
    assert chain.validate_canonical_chain().valid


def test_imported_block_with_valid_coinbase_recipient_is_accepted(alice):
    chain = Blockchain()
    block = _block_with_coinbase_recipient(chain, alice.address)
    chain.add_block(block)
    assert chain.height == 1
    assert chain.state.balances[alice.address] == 50


def test_submit_tx_rejects_malformed_recipient_and_leaves_state_unchanged(tmp_path, alice):
    from toychain.cli import main

    data_dir = tmp_path / "demo"
    node = Node.open(data_dir)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    tip_before = node.chain.tip_hash
    height_before = node.chain.height
    balance_before = dict(node.chain.state.balances)

    bad = _signed_with_recipient(alice, "not-an-address")
    tx_file = tmp_path / "bad-tx.json"
    tx_file.write_text(json.dumps(bad.to_dict()), encoding="utf-8")

    exit_code = main(["--data-dir", str(data_dir), "submit-tx", str(tx_file)])
    assert exit_code == 1

    reopened = Node.open(data_dir)
    assert reopened.chain.tip_hash == tip_before
    assert reopened.chain.height == height_before
    assert reopened.chain.state.balances == balance_before
    assert len(reopened.mempool) == 0


def test_import_tx_rejects_malformed_recipient_and_leaves_state_unchanged(tmp_path, alice):
    from toychain.cli import main

    data_dir = tmp_path / "demo"
    node = Node.open(data_dir)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    tip_before = node.chain.tip_hash

    bad = _signed_with_recipient(alice, "not-an-address")
    tx_file = tmp_path / "bad-tx.json"
    tx_file.write_text(json.dumps(bad.to_dict()), encoding="utf-8")

    exit_code = main(["--data-dir", str(data_dir), "import-tx", str(tx_file)])
    assert exit_code == 1

    reopened = Node.open(data_dir)
    assert reopened.chain.tip_hash == tip_before
    assert len(reopened.mempool) == 0


def test_import_block_rejects_malformed_coinbase_and_leaves_chain_unchanged(tmp_path, alice):
    from toychain.cli import main

    data_dir = tmp_path / "demo"
    node = Node.open(data_dir)
    node.create_wallet()
    tip_before = node.chain.tip_hash
    height_before = node.chain.height

    block = _block_with_coinbase_recipient(Blockchain(), "arbitrary-label")
    block_file = tmp_path / "bad-block.json"
    block_file.write_text(json.dumps(block.to_dict()), encoding="utf-8")

    exit_code = main(["--data-dir", str(data_dir), "import-block", str(block_file)])
    assert exit_code == 1

    reopened = Node.open(data_dir)
    assert reopened.chain.tip_hash == tip_before
    assert reopened.chain.height == height_before
