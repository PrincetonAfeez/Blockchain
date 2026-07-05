# 0002 — Account model instead of UTXO

Status: Accepted

## Context

A transaction model has to prevent double-spends and replay. Bitcoin uses UTXO
(transactions consume and create discrete outputs); Ethereum uses accounts
(balances plus a per-sender nonce). UTXO is more authentic to Bitcoin but adds
an output set, input selection, and change handling — significant complexity for
an educational capstone.

## Decision

Use the account model. A transaction is `(sender, recipient, amount, nonce,
public_key, signature)`. State is `balances`, `nonces`, and confirmed-id/
sender-nonce sets, all derived by replaying the canonical chain
(`transactions.py`, `ChainState`). A transaction is valid only at its sender's
current nonce, which gives replay protection and a total order per sender.

## Consequences

- Validation is simple and deterministic: check the signature, the
  public-key/address relationship, canonical `tc1` sender/recipient form (ADR
  0007), `amount > 0`, the exact nonce, and sufficient balance.
- Double-spend within a block and replay across the chain are both caught by the
  nonce rule plus the confirmed-id/sender-nonce sets.
- UTXO-specific features (coin selection, change, script-like ownership) are out
  of scope; UTXO remains a documented stretch goal.
