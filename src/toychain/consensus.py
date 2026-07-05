"""Consensus-related functionality."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChainScore:
    cumulative_work: int
    block_hash: str
    valid: bool = True


def select_best_chain(scores: Mapping[str, ChainScore]) -> str:
    valid = [score for score in scores.values() if score.valid]
    if not valid:
        raise ValueError("No valid chain tips are available")
    best = min(valid, key=lambda score: (-score.cumulative_work, score.block_hash))
    return best.block_hash


def lowest_common_ancestor(
    first_hash: str,
    second_hash: str,
    parents: Mapping[str, str | None],
) -> str:
    first_ancestors: set[str] = set()
    cursor: str | None = first_hash
    while cursor is not None:
        first_ancestors.add(cursor)
        cursor = parents.get(cursor)
    cursor = second_hash
    while cursor is not None:
        if cursor in first_ancestors:
            return cursor
        cursor = parents.get(cursor)
    raise ValueError("Branches do not share a common ancestor")

