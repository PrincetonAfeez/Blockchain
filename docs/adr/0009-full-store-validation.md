# 0009 — Full-store validation for validate-chain

Status: Accepted

## Context

Persistence stores every known block in the block tree, including noncanonical
fork branches. `validate-chain` was documented as the full verifier for a data
directory, but it only replayed the current canonical branch. A corrupted fork
block could remain on disk while the command returned `"valid": true`.

## Decision

- `validate-chain` calls `Blockchain.validate_all_blocks()`.
- Every indexed block is validated independently in parent-before-child order.
- Each block gets a freshly replayed `ChainState`; stored metadata
  (`parent_hash`, `height`, `cumulative_work`) is recomputed and compared.
- After all blocks pass, fork choice is rerun from recomputed metadata and must
  match the loaded canonical tip; canonical tip state must match cached state.
- Validation reports include canonical/fork counts and `invalid_block_hash`
  when a failure occurs.

## Consequences

- `validate-chain` matches the README promise of whole-store verification.
- Invalid fork branches are detected even when they are not on the canonical
  path.
- The command costs O(all stored transactions), which is acceptable for an
  explicit integrity audit.
