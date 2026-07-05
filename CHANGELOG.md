# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-19

### Added

- Initial public release of the **toychain** educational blockchain CLI and
  Python library (`toychain-capstone` 1.0.0).
- CLI commands for wallet management, signed transfers, mining, chain inspection,
  Merkle proofs, mempool management, local node processes, and debug tooling.
- Public library API exported from `toychain`: `Block`, `BlockHeader`,
  `Blockchain`, `Transaction`, and `create_genesis_block`.
- Canonical binary codecs for version 1 transactions and block headers with
  domain-separated SHA-256 and Ed25519 signatures.
- Account-model ledger with replayed balances and nonces, fixed coinbase reward
  of 50, and proof-of-work mining.
- Merkle transaction commitments with logarithmic inclusion proofs.
- Block tree storage, cumulative-work fork choice, and reorg mempool repair.
- JSON persistence for wallets, block indexes, and mempools with
  `schema_version` 1; consensus bytes remain separate from on-disk JSON.
- Local node subprocess lifecycle (`node start` / `stop`, `network run-local`)
  with PID, lock, config, and log files.
- Architecture decision records in `docs/adr/`.
- Test suite covering codecs, crypto, consensus, persistence, CLI, and process
  lifecycle.

### Known limitations

- **Not money.** No production security, key management, networking gossip, or
  financial guarantees.
- Wallet private keys are stored **unencrypted** in `wallet.json`.
- Toychain addresses have **no checksum**; only malformed `tc1` strings are
  rejected. A syntactically valid typo can still send coins to an uncontrolled
  address.
- Loading a data directory **trusts** previously validated local state; run
  `validate-chain` for a full from-genesis re-verification.
- Nodes do not gossip; blocks and transactions move via explicit import/export.
- Binary `FORMAT_VERSION` 1 and persistence `schema_version` 1 are stable for
  this release; newer on-disk schemas from a future release are rejected
  without modifying files.
- Operational bounds (field sizes, transaction counts, difficulty, timestamp
  drift) are documented in the README **Limits** section.
- JSON import/export formats are defined by Draft 2020-12 schemas in
  `schema/`; strict `from_dict()` loading rejects unknown keys and coerced
  types.

### Fixed

- Consensus validation now rejects malformed `tc1` recipients on imported normal
  transactions and malformed coinbase recipients on imported non-genesis blocks.
- `local-network.json` stores relative node names only; registry paths are
  resolved and contained under the network root before any stop/status side
  effects.

[1.0.0]: #
