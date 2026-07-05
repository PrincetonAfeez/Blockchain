# 0006 — Data format compatibility and migration policy

Status: Accepted

## Context

Toychain uses versioned binary records for consensus (`FORMAT_VERSION` in
`codec.py`) and human-readable JSON for persistence (`persistence.py`). Binary
parsers already reject unknown record versions, but the project did not document
how wallet files, chain indexes, mempools, or future format changes should be
handled across releases. Without an explicit policy, operators cannot tell
whether a data directory from an older or newer build is safe to open.

## Decision

- **Binary consensus records** stay at `FORMAT_VERSION = 1` for this release.
  A incompatible layout requires a new version byte and, where applicable, a new
  domain-separated label (see ADR 0001).
- **JSON persistence** records carry an integer `schema_version` field in
  `wallet.json`, `chain/index.json`, `mempool/transactions.json`, and
  `config.json`. The supported version is `PERSISTENCE_SCHEMA_VERSION = 1` in
  `constants.py`.
- **Load rules:**
  - Missing `schema_version` is treated as version 1 (backward compatibility).
  - A schema version **newer** than the running release raises
    `PersistenceError` and does **not** modify any file.
  - A schema version **older** than the newest supported version is upgraded
    only through an explicit future migration command, not implicitly on load.
  - If a migration command is added later and fails, it must leave the original
    files unchanged.

## Consequences

- Operators get predictable failure modes when opening unsupported data.
- Version 1 on-disk layouts are documented as stable for this release; breaking
  changes require bumping `schema_version` or `FORMAT_VERSION` and recording the
  migration path here.
- The cost is a second version field beside binary record versions, kept in
  sync with README's "Data format compatibility" section.
