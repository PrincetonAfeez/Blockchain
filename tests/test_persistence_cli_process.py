"""Tests for the persistence, CLI, and process functionality."""

from __future__ import annotations

import json
import time

import pytest

from toychain.block import make_block_candidate, mine_block
from toychain.chain import Blockchain
from toychain.debug import disassemble_target
from toychain.errors import NodeRuntimeError, PersistenceError
from toychain.merkle import create_merkle_proof
from toychain.node import Node
from toychain.persistence import DataStore
from toychain.transactions import create_signed_transaction
from toychain.process import (
    network_status,
    node_status,
    run_local_network,
    start_node,
    stop_local_network,
    stop_node,
)


def test_node_persists_wallet_chain_and_mempool(tmp_path):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    result = node.mine(wallet.address, difficulty_bits=1)
    assert result.chain_result.became_canonical

    reopened = Node.open(tmp_path)
    assert reopened.wallet().address == wallet.address
    assert reopened.chain.tip_hash == result.block.hash
    assert reopened.chain.state.balances[wallet.address] == 50


def test_export_import_block_between_isolated_nodes(tmp_path, alice):
    first = Node.open(tmp_path / "first")
    mined = first.mine(alice.address, difficulty_bits=1)
    output = tmp_path / "block.json"
    first.store.export_block(mined.block, output)

    second = Node.open(tmp_path / "second")
    imported = second.store.import_block_file(output)
    result, _ = second.add_block(imported)
    assert result.became_canonical
    assert second.chain.tip_hash == first.chain.tip_hash


def test_debug_disassembly():
    output = disassemble_target("build-merkle-root")
    assert "build_merkle_root" in output
    assert "RETURN_VALUE" in output


def test_debug_disassembly_unsupported_target_fails_cleanly():
    from toychain.errors import ValidationError

    with pytest.raises(ValidationError, match="Unsupported disassembly target"):
        disassemble_target("not-a-real-target")


def test_read_only_open_is_inert_and_refuses_mutation(tmp_path):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)

    reader = Node.open(tmp_path, writable=False)
    assert reader.chain.height == 1
    with pytest.raises(NodeRuntimeError):
        reader.mine(reader.wallet().address, difficulty_bits=1)
    with pytest.raises(NodeRuntimeError):
        reader.create_wallet()


def test_mine_pending_drains_mempool(tmp_path, bob):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    node.create_transaction(bob.address, 5)
    assert len(node.mempool) == 1

    result = node.mine_pending(difficulty_bits=1)
    assert result is not None
    assert result.chain_result.became_canonical
    assert len(node.mempool) == 0
    assert node.chain.height == 2
    # Nothing left to do, and no wallet/mempool means no work.
    assert node.mine_pending(difficulty_bits=1) is None


def test_canonical_tip_is_derived_not_trusted(tmp_path):
    # The tip is recomputed from the validated block set by fork choice, so the
    # canonical_tip.txt mirror is not a trusted input: corrupting it does not
    # change the derived tip (and there is no read-vs-flush race on a tip file).
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    node.mine(wallet.address, difficulty_bits=1)
    real_tip = node.chain.tip_hash

    node.store.tip_path.write_text("deadbeef\n", encoding="ascii")
    assert node.store.load_chain().tip_hash == real_tip


def test_corrupt_chain_index_raises_persistence_error(tmp_path):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    for _ in range(3):
        node.mine(wallet.address, difficulty_bits=1)
    index = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    # Drop a middle block from the index, orphaning its child on reload.
    victim = next(
        h for h, m in index["blocks"].items()
        if m["parent_hash"] is not None and m["height"] == 1
    )
    del index["blocks"][victim]
    node.store.index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(PersistenceError):
        node.store.load_chain()


def test_index_with_traversal_hash_is_rejected_without_reading_outside(tmp_path):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    secret = node.store.data_dir / "secret.json"
    secret.write_text('{"top":"secret"}', encoding="utf-8")

    index = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    index["blocks"]["../../secret"] = {"parent_hash": None, "height": 0, "cumulative_work": 1}
    node.store.index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(PersistenceError, match="Invalid block hash"):
        node.store.load_chain()


def test_mine_rejects_invalid_miner_and_leaves_chain_unchanged(tmp_path):
    from toychain.cli import main

    data_dir = tmp_path / "demo"
    node = Node.open(data_dir)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    tip_before = node.chain.tip_hash
    height_before = node.chain.height

    exit_code = main(
        [
            "--data-dir",
            str(data_dir),
            "mine",
            "--miner",
            "not-an-address",
            "--difficulty",
            "1",
        ]
    )
    assert exit_code == 1

    reopened = Node.open(data_dir)
    assert reopened.chain.tip_hash == tip_before
    assert reopened.chain.height == height_before


def test_persisted_json_includes_schema_version(tmp_path, bob):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    node.create_transaction(bob.address, 1)
    node.store.save_mempool(node.mempool)

    wallet_data = json.loads(node.store.wallet_path.read_text(encoding="utf-8"))
    index_data = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    mempool_data = json.loads(node.store.mempool_path.read_text(encoding="utf-8"))
    assert wallet_data["schema_version"] == 1
    assert index_data["schema_version"] == 1
    assert mempool_data["schema_version"] == 1


def test_unsupported_schema_version_fails_without_modifying_files(tmp_path):
    node = Node.open(tmp_path)
    node.create_wallet()
    node.mine(node.wallet().address, difficulty_bits=1)

    original_index = node.store.index_path.read_text(encoding="utf-8")
    index = json.loads(original_index)
    index["schema_version"] = 99
    node.store.index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(PersistenceError, match="Unsupported schema version"):
        node.store.load_chain()

    reloaded = json.loads(node.store.index_path.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == 99
    assert "blocks" in reloaded


def test_internal_node_run_help(capsys):
    from toychain.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["_node-run", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--port" in help_text
    assert "advisory port" in help_text


def test_version_flag_prints_and_exits_zero(capsys):
    from toychain import __version__
    from toychain.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_trusted_fast_load_matches_full_validation(tmp_path, alice, bob):
    node = Node.open(tmp_path)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)
    node.create_transaction(bob.address, 9)
    node.mine(wallet.address, difficulty_bits=1)

    blocks = list(node.chain.blocks.values())
    trusted = Blockchain.from_blocks(blocks, validate=False)
    verified = Blockchain.from_blocks(blocks, validate=True)

    assert trusted.tip_hash == verified.tip_hash == node.chain.tip_hash
    assert trusted.state.balances == verified.state.balances
    assert trusted.state.nonces == verified.state.nonces
    assert trusted.state.confirmed_tx_ids == verified.state.confirmed_tx_ids
    assert trusted.state.confirmed_sender_nonces == verified.state.confirmed_sender_nonces
    # A trusted load still passes a full from-genesis re-validation.
    assert trusted.validate_canonical_chain().valid


def test_verify_proof_cross_checks_expectations(tmp_path, alice, bob):
    from toychain.cli import main

    transactions = [
        create_signed_transaction(
            private_key=alice.private_key,
            public_key=alice.public_key,
            sender=alice.address,
            recipient=bob.address,
            amount=index + 1,
            nonce=index,
        )
        for index in range(3)
    ]
    tx_ids = [tx.tx_id_bytes() for tx in transactions]
    proof = create_merkle_proof(tx_ids, 1)
    proof_file = tmp_path / "proof.json"
    proof_file.write_text(json.dumps(proof.to_dict()), encoding="utf-8")

    assert main(["verify-proof", str(proof_file)]) == 0
    assert main(["verify-proof", str(proof_file), "--expect-root", proof.root.hex()]) == 0
    # A bad proof is a runtime error -> exit 1 (usage errors are exit 2).
    assert main(["verify-proof", str(proof_file), "--expect-tx", "00" * 32]) == 1


def test_local_node_process_start_status_stop(tmp_path):
    data_dir = tmp_path / "process-node"
    started = start_node(data_dir, port=9876)
    try:
        assert started.running
        assert started.pid is not None
        assert node_status(data_dir).running
        assert DataStore(data_dir).log_path.exists()
        try:
            Node.open(data_dir)
        except NodeRuntimeError:
            pass
        else:
            raise AssertionError("A second owner should not open a locked node directory")
    finally:
        stopped = stop_node(data_dir, timeout=8)
    assert not stopped.running


def test_running_node_mines_pending_transactions(tmp_path, bob):
    data_dir = tmp_path / "miner"
    node = Node.open(data_dir)
    wallet = node.create_wallet()
    node.mine(wallet.address, difficulty_bits=1)   # fund the wallet
    node.create_transaction(bob.address, 5)         # leave a pending tx
    assert len(node.mempool) == 1
    height_before = node.chain.height

    start_node(data_dir, port=9890)
    try:
        deadline = time.time() + 15
        mined = False
        while time.time() < deadline:
            reader = Node.open(data_dir, writable=False)   # read alongside the daemon
            if reader.chain.height > height_before and len(reader.mempool) == 0:
                mined = True
                break
            time.sleep(0.1)
        assert mined, "the running node did not mine its pending transaction"
    finally:
        stop_node(data_dir, timeout=8)


def test_concurrent_cli_writer_is_serialized(tmp_path, monkeypatch):
    import subprocess
    import sys

    node = Node.open(tmp_path)
    node.create_wallet()

    # Re-opening within the same process reclaims its own lock (no conflict).
    assert Node.open(tmp_path).writable

    # A different, live process holding the write lock blocks a second writer.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        node.store.writelock_path.write_text(str(sleeper.pid), encoding="ascii")
        with pytest.raises(NodeRuntimeError, match="another process"):
            Node.open(tmp_path)
    finally:
        sleeper.terminate()
        sleeper.wait()

    # A stale lock (holder no longer running) is reclaimed automatically.
    node.store.writelock_path.write_text("424242", encoding="ascii")
    monkeypatch.setattr("toychain.node.process_is_running", lambda pid: False)
    assert Node.open(tmp_path).writable

    # A read-only open never contends for the write lock.
    assert Node.open(tmp_path, writable=False).writable is False


def test_three_node_local_network_is_isolated(tmp_path):
    root = tmp_path / "network"
    statuses = run_local_network(root, nodes=3, base_port=9900)
    try:
        assert len(statuses) == 3
        assert all(status.running for status in statuses)
        assert len({status.data_dir for status in statuses}) == 3
        assert [status.port for status in network_status(root)] == [9900, 9901, 9902]
    finally:
        stopped = stop_local_network(root)
    assert len(stopped) == 3
    assert not any(status.running for status in stopped)
