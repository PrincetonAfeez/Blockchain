# 0005 — Acceptance-only timestamp drift

Status: Accepted

## Context

A block timestamp should not be wildly in the future, so a node rejects blocks
too far ahead of its clock. But validation is also required to be deterministic:
the same chain must validate identically everywhere. An earlier version applied
the future-drift check during full-chain replay, which read the wall clock — so
the same stored chain could validate on one machine and fail on another whose
clock lagged, and even produce different `validate-chain` verdicts over time.

## Decision

Treat the future-drift bound as a **tip-acceptance policy, not a replay rule**.
`validate_block(now=None)` skips the drift check; only the fresh-block acceptance
path passes the current time (`Node.add_block(now=int(time.time()))`), so a
newly mined or imported tip is bounded but historical replay is not
(`chain.py`). The "not earlier than parent" rule still always applies.

## Consequences

- Full-chain validation is deterministic and clock-independent; a valid chain is
  never rejected because the validator's clock lags the chain's timestamps.
- The drift bound still does its job where it matters: rejecting a too-far-future
  block at the moment it is accepted.
- The drift window is a single documented constant
  (`MAX_TIMESTAMP_DRIFT_SECONDS`, two hours).
