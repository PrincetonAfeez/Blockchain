# 0007 — Canonical address validity at consensus validation

Status: Accepted

## Context

Toychain accounts are identified by `tc1` addresses derived from Ed25519 public
keys. Local helpers (`send`, explicit `mine --miner`) already rejected malformed
addresses, but imported JSON transactions and imported blocks could still commit
balances to arbitrary strings when signatures and proof-of-work were otherwise
valid. Documentation described the `tc1` form as consensus-critical while
enforcement was split across code paths.

## Decision

- A **normal signed transaction** must have `sender` and `recipient` that pass
  `is_valid_address()` during `validate_transaction_authenticity()`.
- A **coinbase transaction** must have:
  - `recipient == GENESIS` at block height 0 only;
  - a valid `tc1` address at every later height (`apply_coinbase()`).
- JSON schemas (`normal-transaction.schema.json`,
  `coinbase-transaction.schema.json`) reject malformed addresses before object
  construction; consensus validation remains the final authority for bytes
  loaded without JSON or for defense in depth after import.

## Consequences

- `send`, `submit-tx`, `import-tx`, `import-block`, and mempool submission share
  the same account-identity rule for normal transfers and mining rewards.
- Genesis keeps its explicit `GENESIS` label; arbitrary coinbase labels on later
  blocks are rejected.
- Addresses still have **no checksum**; syntactically valid typos can move value to
  uncontrolled keys. Schema and consensus checks enforce format, not ownership.
