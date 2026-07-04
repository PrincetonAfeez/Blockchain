# 0001 — Canonical binary serialization with domain separation

Status: Accepted

## Context

Hashes and signatures are only meaningful if everyone agrees on the exact bytes
being hashed or signed. JSON is convenient for storage but is not canonical:
key order, whitespace, and number formatting vary between encoders, so using it
for hashing/signing invites mismatches — the single most common bug class in a
toy blockchain. Separately, hashing different object types (a transaction, a
Merkle leaf, a block header) with no context invites cross-protocol confusion,
where bytes valid in one role are reinterpreted in another.

## Decision

Consensus-critical objects use one strict binary encoding (`codec.py`): a magic
tag, a 1-byte record version, fixed field order, big-endian unsigned integers,
and 32-bit length-prefixed byte/UTF-8 fields. Parsers reject truncation, extra
bytes, unknown versions, invalid UTF-8, and oversized fields.

Every hash/signature input is prefixed with an explicit, versioned domain label
(`constants.py`): `TX_UNSIGNED_V1`, `TX_SIGNED_V1`, `MERKLE_LEAF_V1`,
`MERKLE_NODE_V1`, `BLOCK_HEADER_V1`, `ADDRESS_V1`. JSON is used only for
human-readable persistence and is never hashed or signed.

## Consequences

- Bytes are deterministic and reproducible; encode/parse is tested for stability
  and for clean rejection of malformed input.
- Domain prefixes make a leaf hash and an internal-node hash structurally
  distinct, closing the classic Merkle second-preimage ambiguity.
- The cost is two representations (binary for consensus, JSON for disk), kept
  deliberately separate.
