"""Merkle tree-related functionality."""

from __future__ import annotations

from .constants import MERKLE_LEAF_DOMAIN, MERKLE_NODE_DOMAIN
from .crypto import sha256
from .errors import ValidationError
from .models import MerkleProof, MerkleProofStep


def merkle_leaf(tx_id: bytes) -> bytes:
    if len(tx_id) != 32:
        raise ValidationError("Transaction ID must be exactly 32 bytes")
    return sha256(MERKLE_LEAF_DOMAIN + tx_id)


def merkle_parent(left: bytes, right: bytes) -> bytes:
    if len(left) != 32 or len(right) != 32:
        raise ValidationError("Merkle child hashes must be exactly 32 bytes")
    return sha256(MERKLE_NODE_DOMAIN + left + right)


def build_merkle_root(tx_ids: list[bytes] | tuple[bytes, ...]) -> bytes:
    if not tx_ids:
        raise ValidationError("Cannot build a Merkle tree with no transactions")
    level = [merkle_leaf(tx_id) for tx_id in tx_ids]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            merkle_parent(level[index], level[index + 1])
            for index in range(0, len(level), 2)
        ]
    return level[0]


def create_merkle_proof(
    tx_ids: list[bytes] | tuple[bytes, ...], transaction_index: int
) -> MerkleProof:
    if not tx_ids:
        raise ValidationError("Cannot prove membership in an empty Merkle tree")
    if transaction_index < 0 or transaction_index >= len(tx_ids):
        raise ValidationError("Transaction index is outside the Merkle tree")

    original_index = transaction_index
    level = [merkle_leaf(tx_id) for tx_id in tx_ids]
    steps: list[MerkleProofStep] = []
    index = transaction_index
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling_index = index - 1 if index % 2 else index + 1
        steps.append(
            MerkleProofStep(
                sibling=level[sibling_index],
                sibling_on_left=sibling_index < index,
            )
        )
        level = [
            merkle_parent(level[position], level[position + 1])
            for position in range(0, len(level), 2)
        ]
        index //= 2

    return MerkleProof(
        root=level[0],
        tx_id=tx_ids[original_index],
        transaction_index=original_index,
        transaction_count=len(tx_ids),
        steps=tuple(steps),
    )


def verify_merkle_proof(proof: MerkleProof) -> bool:
    if len(proof.root) != 32 or len(proof.tx_id) != 32:
        return False
    if proof.transaction_count <= 0:
        return False
    if not 0 <= proof.transaction_index < proof.transaction_count:
        return False

    current = merkle_leaf(proof.tx_id)
    index = proof.transaction_index
    width = proof.transaction_count
    step_index = 0
    while width > 1:
        if step_index >= len(proof.steps):
            return False
        step = proof.steps[step_index]
        expected_left = index % 2 == 1
        if step.sibling_on_left != expected_left or len(step.sibling) != 32:
            return False
        if step.sibling_on_left:
            current = merkle_parent(step.sibling, current)
        else:
            current = merkle_parent(current, step.sibling)
        index //= 2
        width = (width + 1) // 2
        step_index += 1

    return step_index == len(proof.steps) and current == proof.root

