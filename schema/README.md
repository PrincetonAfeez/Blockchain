# Toychain JSON schemas

Draft 2020-12 JSON Schemas for the human-readable JSON formats used by import,
export, and persistence. Binary consensus records remain defined by
`src/toychain/codec.py`; these schemas cover JSON only.

| Schema | File | Purpose |
| --- | --- | --- |
| Transaction | [`transaction.schema.json`](transaction.schema.json) | Signed transaction export/import |
| Normal transaction | [`normal-transaction.schema.json`](normal-transaction.schema.json) | Signed transfer (`sender`/`recipient` are `tc1` addresses) |
| Coinbase transaction | [`coinbase-transaction.schema.json`](coinbase-transaction.schema.json) | Block reward (`sender` is `COINBASE`) |
| Block header | [`block-header.schema.json`](block-header.schema.json) | Header section of a block JSON file |
| Block | [`block.schema.json`](block.schema.json) | Block export/import (`chain/blocks/*.json`) |
| Merkle proof | [`merkle-proof.schema.json`](merkle-proof.schema.json) | Inclusion proof files |
| Wallet | [`wallet.schema.json`](wallet.schema.json) | `wallet.json` on disk |
| Chain index | [`chain-index.schema.json`](chain-index.schema.json) | `chain/index.json` metadata |
| Mempool | [`mempool.schema.json`](mempool.schema.json) | `mempool/transactions.json` |
| Local network | [`local-network.schema.json`](local-network.schema.json) | `local-network.json` registry (`name` + advisory `port` only) |
| Node config | [`node-config.schema.json`](node-config.schema.json) | `config.json` in each node data directory |

Versioning:

- Consensus objects use `"version": 1` (binary `FORMAT_VERSION`).
- Persistence files use `"schema_version": 1` (`PERSISTENCE_SCHEMA_VERSION`).

Examples of valid serialized objects live in [`examples/`](examples/). The same
schemas are bundled with the Python package under `src/toychain/schema/` for
runtime validation.

**Address rules in JSON:** normal transactions require `tc1` + 40 lowercase hex
digits for `sender` and `recipient`. Coinbase transactions use
`sender="COINBASE"` and `recipient` either `GENESIS` (genesis only) or a valid
`tc1` address. Consensus code re-checks these rules when blocks and transactions
are validated.
