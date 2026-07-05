# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-06-20

### Changed

- `local-network.json` stores relative node names only; legacy absolute-path
  registry records are rejected.
- `config.json` is now a versioned persistence record validated by
  `schema/node-config.schema.json`.
- `validate-chain` verifies every stored block, not only the canonical branch.
- `node stop` verifies process identity from `node.lifecycle.json` before
  signaling a PID; use `node cleanup-stale` for stale files after manual review.
- Node and local-network startup are failure-atomic: partial startup removes
  lifecycle files once every spawned child is confirmed dead, and
  local-network registry writes happen only after all nodes are ready. Failed
  local-network startup preserves `local-network.starting.json` as a recovery
  registry whenever any child PID remains live (including `live_unverified` or
  `malformed` states).
- `node cleanup-stale` removes lifecycle files only when the node is not running;
  verified live nodes are never cleaned (`--dangerous` is for live unverified PIDs).
- Process identity verification is fail-closed: executable, command line,
  `process_start_token`, and `--instance-id` must match before stop signals.
- `config.json`, `node.lifecycle.json`, and `node.ready.json` are validated on
  read and write; CLI port arguments enforce the 0–65535 range before startup.
- `validate-chain` compares recomputed metadata against persisted
  `chain/index.json` records.
- `node status` reports verified lifecycle state (`running_verified`,
  `live_unverified`, `stale`, `malformed`).

### Fixed

- Local-network startup rollback uses physical PID liveness (`pid_is_live`) and
  parent-held `Popen` handles instead of `ProcessStatus.running`, preserving
  `local-network.starting.json` when stop is refused or termination fails and
  any child remains alive.
- `network run-local` preflight refuses to overwrite an active
  `local-network.json`, an existing recovery registry, or blocked node
  directories; only attempt-scoped starting files created by the current run
  are deleted on rollback.
- `network stop-local` removes `local-network.json` after every registered node
  PID is confirmed dead; the registry is preserved when shutdown is incomplete.
- Consensus validation rejects malformed `tc1` recipients on imported normal
  transactions and malformed coinbase recipients on imported non-genesis blocks.
- `local-network.json` paths are resolved and contained under the network root
  before any stop/status side effects.
- Lockfile completeness for reproducible installs.

### Migration

- Delete or archive an old `local-network.json` that stores `data_dir` paths and
  rerun `network run-local` to create the new name-only format.
- Existing `config.json` files without `schema_version` are treated as version 1
  when loaded; new writes include `"schema_version": 1`.

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

[2.0.0]: https://github.com/PrincetonAfeez/Blockchain/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/PrincetonAfeez/Blockchain/releases/tag/v1.0.0
