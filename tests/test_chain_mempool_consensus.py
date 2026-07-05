"""Tests for the chain, mempool, and consensus functionality."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from toychain.block import make_block_candidate, mine_block
from toychain.chain import Blockchain
from toychain.errors import ConsensusError, MempoolError, ValidationError
from toychain.mempool import Mempool
from toychain.transactions import create_signed_transaction


def mined_on(
    chain: Blockchain,
    parent_hash: str,
    miner: str,
    *,
    transactions=(),
    difficulty: int = 1,
    timestamp_offset: int = 1,
):
    parent = chain.blocks[parent_hash]
    height = chain.metadata[parent_hash].height + 1
    candidate = make_block_candidate(
        previous_hash=bytes.fromhex(parent_hash),
        miner_address=miner,
        height=height,
        transactions=transactions,
        difficulty_bits=difficulty,
        timestamp=parent.header.timestamp + timestamp_offset,
    )
    return mine_block(candidate)[0]


def test_mined_block_validates_and_state_replays(alice, bob):
    chain = Blockchain()
    reward = mined_on(chain, chain.tip_hash, alice.address, difficulty=2)
    chain.add_block(reward)
    transaction = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=17,
        nonce=0,
    )
    transfer = mined_on(
        chain,
        chain.tip_hash,
        alice.address,
        transactions=(transaction,),
        difficulty=2,
    )
    chain.add_block(transfer)
    report = chain.validate_canonical_chain()
    assert report.valid
    assert chain.state.balances[alice.address] == 83
    assert chain.state.balances[bob.address] == 17
    assert chain.state.nonces[alice.address] == 1


def test_tampered_block_and_unknown_parent_fail(alice):
    chain = Blockchain()
    block = mined_on(chain, chain.tip_hash, alice.address)
    bad_body = replace(block, transactions=(replace(block.transactions[0], amount=49),))
    with pytest.raises(ValidationError, match="Merkle root"):
        chain.add_block(bad_body)
    bad_parent = replace(
        block,
        header=replace(block.header, previous_hash=b"\xaa" * 32),
    )
    with pytest.raises(ConsensusError, match="parent is unknown"):
        chain.add_block(bad_parent)


def test_mempool_rejects_replay_conflict_and_insufficient_balance(alice, bob):
    chain = Blockchain()
    chain.add_block(mined_on(chain, chain.tip_hash, alice.address))
    mempool = Mempool()
    transaction = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=20,
        nonce=0,
    )
    mempool.submit(transaction, chain.state)
    with pytest.raises(MempoolError, match="already pending"):
        mempool.submit(transaction, chain.state)
    conflict = replace(transaction, amount=21)
    conflict = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=21,
        nonce=0,
    )
    with pytest.raises(MempoolError, match="sender nonce"):
        mempool.submit(conflict, chain.state)
    too_much = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=100,
        nonce=1,
    )
    with pytest.raises(MempoolError, match="Insufficient"):
        mempool.submit(too_much, chain.state)


def test_shorter_heavier_branch_reorgs_and_repairs_mempool(alice, bob):
    chain = Blockchain()
    mempool = Mempool()
    genesis = chain.genesis_hash

    a1 = mined_on(chain, genesis, alice.address, difficulty=1, timestamp_offset=1)
    chain.add_block(a1)
    transaction = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=10,
        nonce=0,
    )
    a2 = mined_on(
        chain,
        a1.hash,
        alice.address,
        transactions=(transaction,),
        difficulty=1,
        timestamp_offset=1,
    )
    chain.add_block(a2)
    assert chain.height == 2
    assert chain.state.balances[bob.address] == 10

    b1 = mined_on(chain, genesis, alice.address, difficulty=3, timestamp_offset=3)
    result = chain.add_block(b1)
    assert result.reorg is not None and result.reorg.is_reorg
    assert chain.tip_hash == b1.hash
    assert chain.height == 1
    assert chain.metadata[b1.hash].cumulative_work > chain.metadata[a2.hash].cumulative_work

    repair = mempool.repair_after_tip_change(chain, result.reorg)
    assert transaction.tx_id in repair.accepted
    assert mempool.get(transaction.tx_id) == transaction

    b2 = mined_on(
        chain,
        b1.hash,
        alice.address,
        transactions=(transaction,),
        difficulty=3,
    )
    result2 = chain.add_block(b2)
    assert result2.reorg is not None
    mempool.repair_after_tip_change(chain, result2.reorg)
    assert mempool.get(transaction.tx_id) is None
    assert chain.state.balances[bob.address] == 10


def test_equal_work_tie_breaks_on_lowest_hash(alice, bob):
    chain = Blockchain()
    genesis = chain.genesis_hash
    left = mined_on(chain, genesis, alice.address, difficulty=2, timestamp_offset=1)
    right = mined_on(chain, genesis, bob.address, difficulty=2, timestamp_offset=2)
    chain.add_block(left)
    chain.add_block(right)
    assert chain.tip_hash == min(left.hash, right.hash)


def test_double_spend_inside_block_fails(alice, bob):
    chain = Blockchain()
    chain.add_block(mined_on(chain, chain.tip_hash, alice.address))
    first = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=20,
        nonce=0,
    )
    second = create_signed_transaction(
        private_key=alice.private_key,
        public_key=alice.public_key,
        sender=alice.address,
        recipient=bob.address,
        amount=20,
        nonce=0,
    )
    block = mined_on(
        chain,
        chain.tip_hash,
        alice.address,
        transactions=(first, second),
    )
    with pytest.raises(ValidationError, match="already confirmed"):
        chain.add_block(block)


def test_coinbase_must_be_first_unique_and_exact_reward(alice):
    chain = Blockchain()
    valid = mined_on(chain, chain.tip_hash, alice.address)
    excessive_coinbase = replace(valid.transactions[0], amount=51)
    bad_reward_candidate = replace(
        valid,
        transactions=(excessive_coinbase,),
    )
    from toychain.merkle import build_merkle_root

    bad_reward_candidate = replace(
        bad_reward_candidate,
        header=replace(
            bad_reward_candidate.header,
            merkle_root=build_merkle_root((excessive_coinbase.tx_id_bytes(),)),
            nonce=0,
        ),
    )
    bad_reward = mine_block(bad_reward_candidate)[0]
    with pytest.raises(ValidationError, match="fixed block reward"):
        chain.add_block(bad_reward)

    duplicate_candidate = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=1,
        transactions=(valid.transactions[0],),
        difficulty_bits=1,
        timestamp=chain.tip.header.timestamp + 1,
    )
    duplicate = mine_block(duplicate_candidate)[0]
    with pytest.raises(ValidationError, match="exactly one coinbase"):
        chain.add_block(duplicate)


def test_non_positive_amounts_are_rejected_clearly(alice, bob):
    for amount in (-1, 0):
        with pytest.raises(ValidationError, match="positive integer"):
            create_signed_transaction(
                private_key=alice.private_key,
                public_key=alice.public_key,
                sender=alice.address,
                recipient=bob.address,
                amount=amount,
                nonce=0,
            )


def test_coinbase_extranonce_makes_sibling_forks_unique(alice):
    chain = Blockchain()
    genesis = chain.genesis_hash
    left = mined_on(chain, genesis, alice.address, difficulty=1, timestamp_offset=1)
    right = mined_on(chain, genesis, alice.address, difficulty=1, timestamp_offset=2)
    # Same miner, same height, different timestamps -> distinct coinbase ids.
    assert left.transactions[0].is_coinbase
    assert right.transactions[0].is_coinbase
    assert left.transactions[0].tx_id != right.transactions[0].tx_id


def test_future_timestamp_beyond_drift_is_rejected_on_acceptance(alice):
    chain = Blockchain()
    now = int(time.time())
    far_future = now + 100_000
    candidate = make_block_candidate(
        previous_hash=bytes.fromhex(chain.tip_hash),
        miner_address=alice.address,
        height=1,
        difficulty_bits=1,
        timestamp=far_future,
    )
    block = mine_block(candidate)[0]
    # Drift is enforced only when a current time is supplied (fresh acceptance).
    with pytest.raises(ValidationError, match="too far in the future"):
        chain.add_block(block, now=now)


def test_chain_replay_ignores_wall_clock(alice):
    # Replaying/validating an already-accepted chain must be deterministic and
    # clock-independent. A block timestamped far in the "future" is accepted on
    # the replay path (now=None) and validates, independent of any clock.
    chain = Blockchain()
    future = int(time.time()) + 10_000_000
    block = mine_block(
        make_block_candidate(
            previous_hash=bytes.fromhex(chain.tip_hash),
            miner_address=alice.address,
            height=1,
            difficulty_bits=1,
            timestamp=future,
        )
    )[0]
    chain.add_block(block)  # now=None -> historical, no drift check
    assert chain.height == 1
    assert chain.validate_canonical_chain().valid


def test_validate_chain_explain_emits_trace(alice):
    chain = Blockchain()
    chain.add_block(mined_on(chain, chain.tip_hash, alice.address))
    report = chain.validate_canonical_chain(explain=True)
    assert report.valid
    assert any("genesis" in step for step in report.steps)
    assert len(report.steps) == report.checked_blocks
    # The trace is opt-in: it must not leak when --explain is not requested.
    assert chain.validate_canonical_chain().steps == ()


def test_true_double_spend_same_nonce_is_rejected(alice, bob):
    # Two genuinely different transactions (distinct tx ids) reusing one
    # sender+nonce -- a real double-spend, not a duplicate transaction.
    chain = Blockchain()
    chain.add_block(mined_on(chain, chain.tip_hash, alice.address))
    spend_a = create_signed_transaction(
        private_key=alice.private_key, public_key=alice.public_key,
        sender=alice.address, recipient=bob.address, amount=20, nonce=0,
    )
    spend_b = create_signed_transaction(
        private_key=alice.private_key, public_key=alice.public_key,
        sender=alice.address, recipient=bob.address, amount=21, nonce=0,
    )
    assert spend_a.tx_id != spend_b.tx_id
    block = mined_on(chain, chain.tip_hash, alice.address, transactions=(spend_a, spend_b))
    with pytest.raises(ValidationError, match="nonce is already confirmed"):
        chain.add_block(block)


def test_wrong_nonce_is_rejected(alice, bob):
    chain = Blockchain()
    chain.add_block(mined_on(chain, chain.tip_hash, alice.address))
    skipped = create_signed_transaction(
        private_key=alice.private_key, public_key=alice.public_key,
        sender=alice.address, recipient=bob.address, amount=10, nonce=5,
    )
    block = mined_on(chain, chain.tip_hash, alice.address, transactions=(skipped,))
    with pytest.raises(ValidationError, match="Wrong nonce"):
        chain.add_block(block)


def test_coinbase_cannot_enter_mempool(alice):
    from toychain.transactions import create_coinbase

    chain = Blockchain()
    chain.add_block(mined_on(chain, chain.tip_hash, alice.address))
    coinbase = create_coinbase(alice.address, height=1)
    with pytest.raises(MempoolError, match="Coinbase"):
        Mempool().submit(coinbase, chain.state)


def test_send_rejects_malformed_recipient(alice, bob):
    for bad in (
        "not-an-address",
        "tc1" + "z" * 40,
        "tc1abc",
        "",
        bob.address.upper(),
        bob.address[:5].upper() + bob.address[5:],
        "tc1" + bob.address[3:-1],
    ):
        with pytest.raises(ValidationError, match="valid toychain address"):
            create_signed_transaction(
                private_key=alice.private_key, public_key=alice.public_key,
                sender=alice.address, recipient=bad, amount=5, nonce=0,
            )


def test_make_block_candidate_rejects_invalid_miner(alice):
    chain = Blockchain()
    with pytest.raises(ValidationError, match="valid toychain address"):
        make_block_candidate(
            previous_hash=bytes.fromhex(chain.tip_hash),
            miner_address="not-an-address",
            height=1,
        )
    with pytest.raises(ValidationError, match="valid toychain address"):
        make_block_candidate(
            previous_hash=bytes.fromhex(chain.tip_hash),
            miner_address="GENESIS",
            height=1,
        )


def test_select_best_chain_never_picks_an_invalid_branch():
    from toychain.consensus import ChainScore, select_best_chain

    scores = {
        "aaaa": ChainScore(cumulative_work=100, block_hash="aaaa", valid=False),
        "bbbb": ChainScore(cumulative_work=10, block_hash="bbbb", valid=True),
    }
    assert select_best_chain(scores) == "bbbb"  # heavier-but-invalid branch skipped
    with pytest.raises(ValueError, match="No valid"):
        select_best_chain({"aaaa": scores["aaaa"]})
