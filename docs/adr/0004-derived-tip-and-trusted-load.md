# 0004 — Derived canonical tip and trusted fast-load

Status: Accepted

## Context

Two early designs caused real bugs. (1) Storing the canonical tip in a separate
file and cross-checking it on load raced with the running node: a read landing
between the index write and the tip-file write saw them disagree and failed
spuriously. (2) Fully re-validating the whole chain (proof-of-work, Merkle,
every signature) on every command — including read-only ones like `balance` —
cost O(total transactions) per invocation.

## Decision

The canonical tip is **derived** from the validated block set by fork choice, not
read from disk; `canonical_tip.txt` is a human-readable mirror only, so there is
no separate-file load race. On load, the node **trusts** its own previously
validated store: `from_blocks(validate=False)` rebuilds the block tree and
replays transactions for balances/nonces but skips proof-of-work, Merkle, and
signature re-checks (`chain.py`, `_attach_trusted`). Full from-genesis
re-verification is available on demand via `validate-chain`, which also
cross-checks the trusted state against a clean replay.

## Consequences

- No read-vs-write race; commands are cheap (no per-command crypto re-check).
- This is a deliberate trust boundary: a hand-tampered store can show wrong data
  on a read until `validate-chain` is run. This mirrors how real nodes trust a
  local validated database and is documented in the README.
- Loaded index keys are validated as hex hashes before use, so a tampered index
  cannot traverse the filesystem on read.
