"""Command-line interface for the toychain package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .consensus import ChainScore, select_best_chain
from .debug import DISASSEMBLY_TARGETS, disassemble_target
from .errors import MempoolError, ToychainError, ValidationError
from .merkle import create_merkle_proof, verify_merkle_proof
from .models import MerkleProof, Transaction
from .node import Node, run_node_process
from .persistence import read_json, write_json
from .process import (
    network_status,
    node_status,
    run_local_network,
    start_node,
    stop_local_network,
    stop_node,
)
from .transactions import signing_payload


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _node(args: argparse.Namespace) -> Node:
    """Open a node for a state-changing command (rejected if a node owns the dir)."""
    return Node.open(args.data_dir, writable=True)


def _reader(args: argparse.Namespace) -> Node:
    """Open a node read-only: never writes to disk and tolerates a running node."""
    return Node.open(args.data_dir, writable=False)


def _find_transaction(node: Node, tx_id: str) -> Transaction:
    pending = node.mempool.get(tx_id)
    if pending is not None:
        return pending
    for block in node.chain.blocks.values():
        for transaction in block.transactions:
            if transaction.tx_id == tx_id:
                return transaction
    raise ValidationError(f"Unknown transaction: {tx_id}")


_EXAMPLES = """\
examples:
  toychain --data-dir demo create-wallet
  toychain --data-dir demo mine --difficulty 8
  toychain --data-dir demo send <tc1-address> 12 --out tx.json
  toychain --data-dir demo validate-chain --explain
  toychain --data-dir demo merkle-proof <block-hash> <tx-id> --out proof.json
  toychain --data-dir localnet network run-local --nodes 3

Each --data-dir is an isolated node with its own wallet, chain, and mempool.
This is an educational toy: it is not money and has no real security.

exit codes:
  0  success
  1  runtime error (validation, crypto, consensus, mempool, persistence, ...)
  2  usage error (unknown command, bad/missing arguments)
"""


def _add_global_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toychain",
        description="Educational CLI blockchain: Ed25519-signed transactions, "
        "canonical bytes, Merkle proofs, proof-of-work, and most-work fork choice.",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"toychain {__version__}",
        help="print the version and exit",
    )
    parser.add_argument(
        "--data-dir",
        default=".toychain",
        help="isolated node data directory (default: .toychain)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    sub.add_parser("create-wallet", help="create an Ed25519 wallet in the data directory")
    sub.add_parser("address", help="print the local wallet address")

    export_public = sub.add_parser("export-public-key", help="print/export the public key")
    export_public.add_argument("--out", help="write the public key JSON to this file")
    sub.add_parser("wallet-info", help="show address, balance, and next nonce")

    send = sub.add_parser("send", help="build and submit a signed transaction")
    send.add_argument("recipient", help="recipient toychain address (tc1...)")
    send.add_argument("amount", type=int, help="integer amount to transfer")
    send.add_argument("--out", help="also write the signed transaction to this file")
    send.add_argument("--no-submit", action="store_true", help="build without submitting")

    submit = sub.add_parser("submit-tx", help="submit a signed transaction file to the mempool")
    submit.add_argument("tx_file", help="path to a signed transaction JSON file")
    inspect_tx = sub.add_parser("inspect-tx", help="show a transaction and its canonical bytes")
    inspect_tx.add_argument("tx_id", help="transaction id (hex)")
    export_tx = sub.add_parser("export-tx", help="write a known transaction to a file")
    export_tx.add_argument("tx_id", help="transaction id (hex)")
    export_tx.add_argument("--out", required=True, help="output file path")
    import_tx = sub.add_parser("import-tx", help="import a transaction file into the mempool")
    import_tx.add_argument("tx_file", help="path to a transaction JSON file")

    mine = sub.add_parser("mine", help="mine a block from the mempool")
    mine.add_argument("--miner", help="miner address for the coinbase (default: local wallet)")
    mine.add_argument("--difficulty", type=int, default=8, help="difficulty bits (default: 8)")
    inspect_block = sub.add_parser("inspect-block", help="show a block and its header bytes")
    inspect_block.add_argument("block_hash", help="block hash (hex)")
    export_block = sub.add_parser("export-block", help="write a known block to a file")
    export_block.add_argument("block_hash", help="block hash (hex)")
    export_block.add_argument("--out", required=True, help="output file path")
    import_block = sub.add_parser("import-block", help="import and validate a block file")
    import_block.add_argument("block_file", help="path to a block JSON file")

    sub.add_parser("show-chain", help="list the canonical chain from genesis to tip")
    validate = sub.add_parser("validate-chain", help="re-verify the whole chain from genesis")
    validate.add_argument("--explain", action="store_true", help="print a per-block rule trace")
    balance = sub.add_parser("balance", help="show an account balance (default: local wallet)")
    balance.add_argument("address", nargs="?", help="address to query (default: local wallet)")
    nonce = sub.add_parser("account-nonce", help="show an account's next nonce")
    nonce.add_argument("address", nargs="?", help="address to query (default: local wallet)")
    sub.add_parser("show-forks", help="list every known block and mark the canonical branch")
    sub.add_parser("show-canonical-tip", help="show the current tip hash, height, and work")

    proof = sub.add_parser("merkle-proof", help="produce an O(log n) inclusion proof")
    proof.add_argument("block_hash", help="block containing the transaction")
    proof.add_argument("tx_id", help="transaction id to prove (hex)")
    proof.add_argument("--out", help="write the proof JSON to this file")
    verify = sub.add_parser("verify-proof", help="verify a Merkle inclusion proof file")
    verify.add_argument("proof_file", help="path to a proof JSON file (self-contained)")
    verify.add_argument("--expect-root", help="cross-check the proof against a known root")
    verify.add_argument("--expect-tx", help="cross-check the proof against a known tx id")

    mempool = sub.add_parser("mempool", help="inspect or manage the mempool")
    mempool_sub = mempool.add_subparsers(dest="mempool_command", required=True, metavar="<action>")
    mempool_sub.add_parser("show", help="list pending transactions")
    mempool_sub.add_parser("validate", help="revalidate the mempool against canonical state")
    clear = mempool_sub.add_parser("clear", help="drop all pending transactions")
    clear.add_argument("--dangerous", action="store_true", help="required confirmation flag")

    node = sub.add_parser("node", help="run a local node process")
    node_sub = node.add_subparsers(dest="node_command", required=True, metavar="<action>")
    node_start = node_sub.add_parser("start", help="start a background node process")
    node_start.add_argument("--port", type=int, default=0, help="advisory port for the node")
    node_sub.add_parser("stop", help="stop the running node process")
    node_sub.add_parser("status", help="report whether the node is running")

    network = sub.add_parser("network", help="orchestrate several local nodes")
    network_sub = network.add_subparsers(dest="network_command", required=True, metavar="<action>")
    run_local = network_sub.add_parser("run-local", help="start N isolated local nodes")
    run_local.add_argument("--nodes", type=int, default=3, help="number of nodes (default: 3)")
    run_local.add_argument("--base-port", type=int, default=9001, help="first port (default: 9001)")
    network_sub.add_parser("stop-local", help="stop every node in the local network")
    network_sub.add_parser("status", help="report the status of every local node")

    debug_bytes = sub.add_parser("debug-bytes", help="print the exact hashed/signed bytes")
    debug_bytes.add_argument("kind", choices=("tx", "block"), help="object kind")
    debug_bytes.add_argument("identifier", help="transaction id or block hash")

    disassemble = sub.add_parser("debug-disassemble", help="disassemble a core function (dis)")
    disassemble.add_argument(
        "target", choices=sorted(DISASSEMBLY_TARGETS), help="function to disassemble"
    )

    debug_consensus = sub.add_parser("debug-consensus", help="run fork choice over a tip file")
    debug_consensus.add_argument("block_tree_file", help="JSON mapping block hash -> score")

    # Internal entry point used by `node start` to spawn the daemon. Omitting
    # help= keeps it out of the command listing while remaining invokable.
    internal = sub.add_parser("_node-run")
    internal.add_argument("--port", type=int, default=0)
    return parser


def _wallet_address(node: Node, explicit: str | None) -> str:
    return explicit if explicit else node.wallet().address


def _handle_wallet(args: argparse.Namespace) -> bool:
    if args.command == "create-wallet":
        wallet = _node(args).create_wallet()
        _json(
            {
                "address": wallet.address,
                "public_key": wallet.public_key.hex(),
                "warning": (
                    "The private key is stored unencrypted in wallet.json. "
                    "This is an educational toy: never reuse a real key here."
                ),
            }
        )
    elif args.command == "address":
        print(_reader(args).wallet().address)
    elif args.command == "export-public-key":
        wallet = _reader(args).wallet()
        value = {"address": wallet.address, "public_key": wallet.public_key.hex()}
        if args.out:
            write_json(Path(args.out), value)
            _json({"written": str(Path(args.out).resolve())})
        else:
            _json(value)
    elif args.command == "wallet-info":
        node = _reader(args)
        wallet = node.wallet()
        state = node.chain.state
        _json(
            {
                "address": wallet.address,
                "public_key": wallet.public_key.hex(),
                "balance": state.balances.get(wallet.address, 0),
                "next_nonce": state.nonces.get(wallet.address, 0),
                "warning": "Private key material is intentionally not displayed.",
            }
        )
    else:
        return False
    return True


def _handle_transactions(args: argparse.Namespace) -> bool:
    if args.command == "send":
        node = _node(args)
        transaction = node.create_transaction(
            args.recipient, args.amount, submit=not args.no_submit
        )
        if args.out:
            node.store.export_transaction(transaction, args.out)
        _json(
            {
                "transaction": transaction.to_dict(),
                "tx_id": transaction.tx_id,
                "submitted": not args.no_submit,
                "output": str(Path(args.out).resolve()) if args.out else None,
            }
        )
    elif args.command in {"submit-tx", "import-tx"}:
        node = _node(args)
        transaction = node.store.import_transaction_file(args.tx_file)
        tx_id = node.submit_transaction(transaction)
        _json({"accepted": True, "tx_id": tx_id})
    elif args.command == "inspect-tx":
        transaction = _find_transaction(_reader(args), args.tx_id)
        _json(
            {
                "transaction": transaction.to_dict(),
                "tx_id": transaction.tx_id,
                "unsigned_bytes": transaction.unsigned_bytes().hex(),
                "signing_payload": signing_payload(transaction).hex(),
                "signed_bytes": transaction.signed_bytes().hex(),
            }
        )
    elif args.command == "export-tx":
        node = _reader(args)
        transaction = _find_transaction(node, args.tx_id)
        node.store.export_transaction(transaction, args.out)
        _json({"written": str(Path(args.out).resolve()), "tx_id": transaction.tx_id})
    else:
        return False
    return True


def _handle_blocks(args: argparse.Namespace) -> bool:
    if args.command == "mine":
        node = _node(args)
        mine_result = node.mine(
            _wallet_address(node, args.miner),
            difficulty_bits=args.difficulty,
        )
        _json(
            {
                "block": mine_result.block.to_dict(),
                "mining": mine_result.stats.to_dict(),
                "height": mine_result.chain_result.height,
                "became_canonical": mine_result.chain_result.became_canonical,
            }
        )
    elif args.command == "inspect-block":
        node = _reader(args)
        try:
            block = node.chain.blocks[args.block_hash]
        except KeyError as exc:
            raise ValidationError(f"Unknown block: {args.block_hash}") from exc
        metadata = node.chain.metadata[args.block_hash]
        _json(
            {
                "block": block.to_dict(),
                "metadata": metadata.to_dict(),
                "canonical_header_bytes": block.header.canonical_bytes().hex(),
            }
        )
    elif args.command == "export-block":
        node = _reader(args)
        try:
            block = node.chain.blocks[args.block_hash]
        except KeyError as exc:
            raise ValidationError(f"Unknown block: {args.block_hash}") from exc
        node.store.export_block(block, args.out)
        _json({"written": str(Path(args.out).resolve()), "block_hash": block.hash})
    elif args.command == "import-block":
        node = _node(args)
        block = node.store.import_block_file(args.block_file)
        add_result, repair = node.add_block(block)
        _json(
            {
                "block_hash": add_result.block_hash,
                "height": add_result.height,
                "became_canonical": add_result.became_canonical,
                "reorg": add_result.reorg.is_reorg if add_result.reorg else False,
                "mempool_rejected": list(repair.rejected) if repair else [],
            }
        )
    else:
        return False
    return True


def _handle_chain(args: argparse.Namespace) -> bool:
    if args.command == "show-chain":
        node = _reader(args)
        _json(
            [
                {
                    "height": node.chain.metadata[block.hash].height,
                    "hash": block.hash,
                    "previous_hash": block.header.previous_hash.hex(),
                    "transactions": len(block.transactions),
                    "difficulty_bits": block.header.difficulty_bits,
                }
                for block in node.chain.canonical_blocks()
            ]
        )
    elif args.command == "validate-chain":
        report = _reader(args).chain.validate_canonical_chain(explain=args.explain)
        payload = {
            "valid": report.valid,
            "checked_blocks": report.checked_blocks,
            "tip_hash": report.tip_hash,
            "message": report.message,
        }
        if args.explain:
            payload["steps"] = list(report.steps)
        _json(payload)
        if not report.valid:
            raise ValidationError(report.message)
    elif args.command == "balance":
        node = _reader(args)
        address = _wallet_address(node, args.address)
        _json({"address": address, "balance": node.chain.state.balances.get(address, 0)})
    elif args.command == "account-nonce":
        node = _reader(args)
        address = _wallet_address(node, args.address)
        _json(
            {"address": address, "next_nonce": node.chain.state.nonces.get(address, 0)}
        )
    elif args.command == "show-forks":
        _json(_reader(args).chain.fork_summary())
    elif args.command == "show-canonical-tip":
        node = _reader(args)
        metadata = node.chain.metadata[node.chain.tip_hash]
        _json(
            {
                "hash": node.chain.tip_hash,
                "height": metadata.height,
                "cumulative_work": metadata.cumulative_work,
            }
        )
    else:
        return False
    return True


def _handle_merkle(args: argparse.Namespace) -> bool:
    if args.command == "merkle-proof":
        node = _reader(args)
        try:
            block = node.chain.blocks[args.block_hash]
        except KeyError as exc:
            raise ValidationError(f"Unknown block: {args.block_hash}") from exc
        tx_ids = tuple(tx.tx_id_bytes() for tx in block.transactions)
        try:
            index = [tx_id.hex() for tx_id in tx_ids].index(args.tx_id)
        except ValueError as exc:
            raise ValidationError("Transaction is not in the selected block") from exc
        proof = create_merkle_proof(tx_ids, index)
        if args.out:
            write_json(Path(args.out), proof.to_dict())
        _json(
            {
                "proof": proof.to_dict(),
                "valid": verify_merkle_proof(proof),
                "output": str(Path(args.out).resolve()) if args.out else None,
            }
        )
    elif args.command == "verify-proof":
        data = read_json(Path(args.proof_file))
        if not isinstance(data, dict):
            raise ValidationError("Proof file must contain a JSON object")
        proof = MerkleProof.from_dict(data)
        valid = verify_merkle_proof(proof)
        if args.expect_root and proof.root.hex() != args.expect_root.lower():
            valid = False
        if args.expect_tx and proof.tx_id.hex() != args.expect_tx.lower():
            valid = False
        _json({"valid": valid, "root": proof.root.hex(), "tx_id": proof.tx_id.hex()})
        if not valid:
            raise ValidationError("Merkle proof is invalid")
    else:
        return False
    return True


def _handle_mempool(args: argparse.Namespace) -> bool:
    if args.command != "mempool":
        return False
    node = _reader(args) if args.mempool_command == "show" else _node(args)
    if args.mempool_command == "show":
        _json(
            {
                "count": len(node.mempool),
                "transactions": [tx.to_dict() | {"tx_id": tx.tx_id} for tx in node.mempool],
            }
        )
    elif args.mempool_command == "validate":
        report = node.mempool.revalidate(node.chain.state)
        node.store.save_mempool(node.mempool)
        _json(
            {
                "valid_count": len(report.accepted),
                "accepted": list(report.accepted),
                "rejected": list(report.rejected),
            }
        )
    elif args.mempool_command == "clear":
        if not args.dangerous:
            raise MempoolError("Refusing to clear without --dangerous")
        node.mempool.clear()
        node.store.save_mempool(node.mempool)
        _json({"cleared": True})
    return True


def _handle_process(args: argparse.Namespace) -> bool:
    if args.command == "node":
        if args.node_command == "start":
            _json(start_node(args.data_dir, args.port).to_dict())
        elif args.node_command == "stop":
            _json(stop_node(args.data_dir).to_dict())
        else:
            _json(node_status(args.data_dir).to_dict())
        return True
    if args.command == "network":
        if args.network_command == "run-local":
            statuses = run_local_network(args.data_dir, args.nodes, args.base_port)
        elif args.network_command == "stop-local":
            statuses = stop_local_network(args.data_dir)
        else:
            statuses = network_status(args.data_dir)
        _json([status.to_dict() for status in statuses])
        return True
    return False


def _handle_debug(args: argparse.Namespace) -> bool:
    if args.command == "debug-disassemble":
        print(disassemble_target(args.target), end="")
    elif args.command == "debug-bytes":
        node = _reader(args)
        if args.kind == "tx":
            transaction = _find_transaction(node, args.identifier)
            _json(
                {
                    "unsigned_canonical": transaction.unsigned_bytes().hex(),
                    "signing_payload": signing_payload(transaction).hex(),
                    "signed_canonical": transaction.signed_bytes().hex(),
                    "tx_id": transaction.tx_id,
                }
            )
        else:
            try:
                block = node.chain.blocks[args.identifier]
            except KeyError as exc:
                raise ValidationError(f"Unknown block: {args.identifier}") from exc
            _json(
                {
                    "canonical_header": block.header.canonical_bytes().hex(),
                    "block_hash": block.hash,
                    "merkle_root": block.header.merkle_root.hex(),
                }
            )
    elif args.command == "debug-consensus":
        data = read_json(Path(args.block_tree_file))
        if not isinstance(data, dict):
            raise ValidationError("Consensus input must be a JSON object")
        scores = {
            block_hash: ChainScore(
                cumulative_work=int(value["cumulative_work"]),
                block_hash=block_hash,
                valid=bool(value.get("valid", True)),
            )
            for block_hash, value in data.items()
        }
        _json({"selected_tip": select_best_chain(scores)})
    else:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = _add_global_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "_node-run":
            return run_node_process(args.data_dir, args.port)
        handlers = (
            _handle_wallet,
            _handle_transactions,
            _handle_blocks,
            _handle_chain,
            _handle_merkle,
            _handle_mempool,
            _handle_process,
            _handle_debug,
        )
        if not any(handler(args) for handler in handlers):
            parser.error(f"Unhandled command: {args.command}")
        return 0
    except (ToychainError, ValueError, KeyError, TypeError) as exc:
        # Exit 1 = runtime error; argparse uses exit 2 for usage/CLI misuse, so
        # the two are distinguishable. 0 = success.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
