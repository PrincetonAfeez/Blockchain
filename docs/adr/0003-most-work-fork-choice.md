# 0003 — Most-work fork choice with a deterministic tie-break

Status: Accepted

## Context

When blocks fork, a node must pick one canonical branch. "First seen" depends on
arrival order and is not reproducible. "Longest by block count" is gameable with
many low-difficulty blocks. The known blocks form a tree, so selecting the
canonical tip is a graph problem and must be deterministic so every node that
holds the same blocks agrees.

## Decision

Score each block by cumulative work, where `work(block) = 2 ** difficulty_bits`
and a branch's work is the sum from genesis (`block.py`, `chain.py`). The
canonical tip is the valid tip with the greatest cumulative work, even if it has
fewer blocks. Equal-work tips are broken by the lexicographically lowest block
hash (`consensus.py`, `select_best_chain`). On a tip change, find the lowest
common ancestor, recompute state along the new branch, remove newly confirmed
mempool transactions, and return still-valid orphaned transactions to the
mempool.

## Consequences

- Selection is deterministic and reproducible: same blocks → same tip.
- Heaviest, not longest, wins, so a short high-difficulty branch can correctly
  reorganize a longer low-difficulty one (tested).
- Validity is filtered in `select_best_chain`, which keeps the door open for
  `debug-consensus` to score hypothetical invalid tips even though stored blocks
  are always valid.
