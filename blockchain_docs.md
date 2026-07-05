# Architecture Decision Record
## App — Blockchain
**Distributed Ledger Systems Group | Document 1 of 5**  
**Status: Accepted**

---

## Context

Toychain is an educational, CLI-first blockchain implemented in Python. It demonstrates deterministic consensus bytes, Ed25519 authorization, replay-derived account state, Merkle commitments, proof-of-work, cumulative-work fork choice, reorganizations, persistence, and local node process management.

The project is deliberately **not money**, not a production cryptocurrency, and not a hardened distributed consensus network. Its value is architectural: each rule is inspectable, deterministic, and testable.

## Decisions

### 1. Canonical binary consensus bytes

Transactions and block headers use strict versioned binary encodings with fixed field order, big-endian integers, and length-prefixed text/byte fields. JSON is never signed or hashed.

**Rationale:** Consensus identity must not depend on JSON spacing, key order, number formatting, or parser differences.

### 2. Domain-separated cryptography

Distinct prefixes are used for unsigned transaction signatures, signed transaction IDs, Merkle leaves, Merkle nodes, block headers, and address derivation.

**Rationale:** The same byte string must not be reusable across different cryptographic purposes.

### 3. Real Ed25519 signatures and SHA-256

Wallets use Ed25519 key pairs. Addresses are derived from a domain-separated SHA-256 digest of the public key. Transactions are authorized by signing their canonical unsigned representation.

**Trade-off:** Wallet files store private keys unencrypted. This is acceptable only for an educational toy.

### 4. Account model with replay-derived state

Balances, next nonces, confirmed transaction IDs, and used sender/nonces are derived by replaying the canonical chain.

**Rejected:** Trusting persisted balances as consensus state or using a UTXO model.

### 5. Strict integer transfers and sequential nonces

Amounts must be positive integers. Booleans, floats, and silent coercion are rejected. Each sender must use the exact next nonce.

**Rationale:** This prevents replay, ambiguous numeric behavior, and sender-local transaction reordering.

### 6. One fixed-reward coinbase per block

Every block contains exactly one coinbase transaction, first in the list. Its reward is fixed, and its nonce equals the block height.

The coinbase public-key field carries an eight-byte extranonce derived from the parent hash and timestamp, making competing blocks produce distinct coinbase transaction IDs.

### 7. Merkle commitment to ordered transactions

Each transaction ID is domain-hashed into a leaf. Internal nodes are domain-hashed pairs. Odd levels duplicate the final node.

**Consequence:** Membership proofs require one sibling per level and verify in `O(log n)` time.

### 8. Leading-zero-bit proof of work

A block is valid when the integer value of its SHA-256 header hash is below the target implied by `difficulty_bits`. Block work is `2 ** difficulty_bits`.

**Rationale:** Mining may require many hashes, while verification requires one.

### 9. Greatest cumulative work fork choice

Known blocks form a tree. The canonical tip is the valid branch with greatest cumulative work. Equal-work branches use the lexicographically lowest block hash.

**Rejected:** Height-only selection or arrival-order tie-breaking.

### 10. State cached per accepted block

Every accepted block stores a derived `ChainState` based on its parent branch.

**Rationale:** Side branches must be validated against their own parent state, and reorganizations can adopt the new tip state without replaying from genesis each time.

### 11. Reorganization-aware mempool repair

On a tip change, the node finds the lowest common ancestor, identifies disconnected and connected blocks, removes transactions confirmed on the new branch, and revalidates normal transactions from orphaned blocks.

### 12. Clock-relative rules only at fresh acceptance

A block earlier than its parent is always invalid. A fresh mined/imported block more than two hours ahead of the accepting clock is rejected. Historical replay does not consult wall-clock time.

**Rationale:** Full-chain verification must be deterministic and clock-independent.

### 13. Derived canonical tip

The authoritative tip is recomputed from the loaded block set using deterministic fork choice. `canonical_tip.txt` is only a human-readable mirror.

### 14. Trusted fast load plus explicit full verifier

Ordinary loading trusts blocks previously accepted by the node, rebuilds the tree, and replays effects without rechecking every signature, Merkle root, or proof of work. `validate-chain` performs full verification from genesis.

**Trade-off:** Fast load is efficient but assumes the local accepted store has not been maliciously altered.

### 15. Serialized writers and isolated node processes

A running node owns its directory with `node.lock`. One-off state-changing CLI commands use `node.writelock`; stale locks are reclaimed. Read-only commands can operate alongside the daemon.

### 16. Explicit local process orchestration, not P2P gossip

Multiple isolated node subprocesses can be started and stopped. Blocks and transactions move through import/export commands.

**Rejected:** Claiming peer discovery, gossip, adversarial networking, or distributed finality.

## Consequences

**Benefits**

- Deterministic transaction and block identities.
- Auditable state replay.
- Work-based fork choice and deterministic ties.
- Logarithmic Merkle proofs.
- Reorg-safe pending transaction handling.
- Readable persistence without contaminating consensus bytes.
- Clear trust boundary between fast loading and full validation.
- Safe single-writer behavior per data directory.

**Costs and limits**

- Unencrypted private keys.
- No fees, difficulty adjustment, smart contracts, pruning, or P2P gossip.
- Educational proof-of-work has no economic security.
- Fast loading trusts previously accepted local files.
- Account model is simpler but less Bitcoin-like than UTXO accounting.

## Alternatives Not Explored

UTXO accounting, proof of stake, fee markets, smart contracts, peer discovery, compact block relay, encrypted wallets, hardware signers, database-backed storage, pruning, and production monetary policy.

---

*Constitution reference: architecture, scope discipline, proportional quality, explicit trade-offs, verification, and progressive complexity.*

---

# Technical Design Document
## App — Blockchain
**Distributed Ledger Systems Group | Document 2 of 5**

---

## Overview

**Package:** `toychain-capstone`  
**Module:** `toychain`  
**CLI:** `toychain`  
**Python:** 3.11+  
**Runtime dependency:** `cryptography>=42`  
**Hash:** SHA-256  
**Signature:** Ed25519  
**Format version:** 1

## Architecture

```text
CLI
 └── Node service
      ├── Blockchain
      │    ├── block tree + metadata
      │    ├── branch ChainState cache
      │    └── cumulative-work fork choice
      ├── Mempool
      ├── Wallet
      └── DataStore

Consensus core
 ├── canonical codecs
 ├── domain-separated crypto
 ├── transaction validation/replay
 ├── Merkle roots/proofs
 ├── proof-of-work
 └── reorganization analysis

Host/process layer
 ├── node daemon
 ├── PID/lock/stop files
 ├── heartbeats and logs
 └── local multi-node orchestration
```

## Core Models

### Transaction

```python
Transaction(
    sender: str,
    recipient: str,
    amount: int,
    nonce: int,
    public_key: bytes,
    signature: bytes,
    version: int,
)
```

A normal transaction is authenticated by its 32-byte public key and 64-byte Ed25519 signature. A coinbase uses a reserved sender, has no signature, and may carry an eight-byte extranonce.

### BlockHeader

```python
BlockHeader(
    previous_hash: bytes,
    merkle_root: bytes,
    timestamp: int,
    difficulty_bits: int,
    nonce: int,
    version: int,
)
```

### Block

```python
Block(header: BlockHeader, transactions: tuple[Transaction, ...])
```

### ChainState

```python
ChainState(
    balances: dict[str, int],
    nonces: dict[str, int],
    confirmed_tx_ids: set[str],
    confirmed_sender_nonces: set[tuple[str, int]],
)
```

### BlockMetadata

Parent hash, height, and cumulative work.

## Canonical Encoding

Primitive rules:

- big-endian unsigned integers;
- explicit magic/version bytes;
- UTF-8 strings prefixed by `u32` length;
- byte fields prefixed by `u32` length;
- strict full-consumption parsing;
- maximum field size: 1,000,000 bytes;
- maximum block transaction count: 100,000.

Unsigned transaction:

```text
TX_UNSIGNED_MAGIC | version:u8 | sender:text | recipient:text
| amount:u64 | nonce:u64 | public_key:bytes
```

Signed transaction adds:

```text
signature:bytes
```

Block header:

```text
BLOCK_HEADER_MAGIC | version:u8 | previous_hash:32 | merkle_root:32
| timestamp:u64 | difficulty_bits:u8 | nonce:u64
```

Block record:

```text
BLOCK_MAGIC | record_version:u8 | header:bytes
| transaction_count:u32 | transaction:bytes[]
```

Parsers reject unknown versions, malformed magic, truncation, oversized fields, invalid UTF-8, and trailing bytes.

## Cryptographic Pipeline

```text
signing payload = TX_UNSIGNED_DOMAIN || unsigned_tx_bytes
transaction ID  = SHA256(TX_SIGNED_DOMAIN || signed_tx_bytes)
Merkle leaf     = SHA256(MERKLE_LEAF_DOMAIN || tx_id)
Merkle node     = SHA256(MERKLE_NODE_DOMAIN || left || right)
block hash      = SHA256(BLOCK_HEADER_DOMAIN || header_bytes)
address         = "tc1" || first_20_bytes(SHA256(ADDRESS_DOMAIN || public_key))
```

## Transaction Validation

A normal transaction must:

1. use the supported version;
2. have non-empty sender and recipient;
3. have a positive integer amount;
4. contain a 32-byte public key and 64-byte signature;
5. derive the sender address from that public key;
6. pass Ed25519 signature verification;
7. have an unconfirmed transaction ID;
8. use an unused sender/nonce pair;
9. equal the sender's expected next nonce;
10. have sufficient balance.

Application debits sender, credits recipient, increments nonce, and records duplicate-prevention sets.

## Coinbase Rules

- exactly one coinbase per block;
- coinbase appears first;
- no signature;
- fixed block reward;
- nonce equals block height;
- public-key slot is empty or the defined extranonce length;
- transaction ID must not already be confirmed.

## Merkle Tree

- leaves commit transaction IDs;
- transaction order matters;
- odd levels duplicate the last node;
- proofs carry root, transaction ID, original index/count, sibling hashes, and side flags;
- verifier reconstructs expected width and sibling orientation.

## Proof of Work

```text
valid if int(block_hash, big_endian) < 2 ** (256 - difficulty_bits)
work = 2 ** difficulty_bits
```

Mining increments the unsigned 64-bit nonce until the target is met. Returned statistics include attempts, elapsed time, hash rate, transaction count, root, hash, and nonce.

## Genesis

Genesis is deterministic: fixed timestamp, fixed recipient, all-zero parent, one coinbase, and difficulty zero. Any chain with a different genesis is rejected.

## Block Validation

```text
validate_block(block, parent, parent_state, height, now)
 ├── header version and hash lengths
 ├── parent link
 ├── difficulty range and proof of work
 ├── parent-relative timestamp
 ├── optional acceptance-time future drift
 ├── exactly one first-position coinbase
 ├── recomputed Merkle root
 ├── coinbase validation/application
 └── sequential normal transaction validation/application
```

## Block Tree and Fork Choice

`Blockchain` stores blocks, metadata, children, and derived state by block hash. New blocks are validated against their actual parent branch. Tip selection uses:

```text
minimum key (-cumulative_work, block_hash)
```

This means maximum cumulative work, with lowest hash as tie-breaker.

## Reorganizations

A tip change computes the lowest common ancestor, orphaned path, and connected path. The chain adopts the cached new-tip state. The mempool revalidates its existing transactions plus normal transactions from orphaned blocks, skipping newly confirmed IDs.

## Mempool

The mempool is insertion-ordered by transaction ID. Submission rejects coinbase, duplicate IDs, duplicate pending sender/nonces, and transactions invalid against the state projected through all earlier pending transactions.

## Persistence

```text
data-dir/
  wallet.json
  config.json
  node.pid
  node.lock
  node.writelock
  node.stop
  node.log
  chain/
    index.json
    canonical_tip.txt
    blocks/<hash>.json
  mempool/
    transactions.json
```

JSON writes use a temporary file plus `os.replace`. Block hash path components are validated before file access. Filename/hash agreement is checked. The canonical tip is re-derived during load.

## Node Runtime

`Node.open()` supports writable and read-only modes. Writable commands reject daemon ownership and acquire a one-off writer lock. Read-only commands never persist.

The daemon:

- owns `node.lock`;
- writes PID and config;
- mines pending transactions when a wallet exists;
- logs mining results/errors;
- emits five-second heartbeats;
- stops on signal or stop file;
- flushes and removes lifecycle files on exit.

## Local Network Orchestration

`network run-local` starts isolated subprocesses, records their directories/ports, and rolls back started processes if later startup fails. Ports are advisory; no core gossip protocol exists.

## Limits

No fees, difficulty adjustment, encrypted wallet, smart contracts, P2P gossip, database, pruning, production key management, or economic security.

## Verification

CI installs the package with dev dependencies, runs pytest, and runs mypy on Python 3.11, 3.12, and 3.13.

---

# Interface Design Specification
## App — Blockchain
**Distributed Ledger Systems Group | Document 3 of 5**

---

## Command Form

```powershell
toychain [--data-dir PATH] <command> [options]
```

Default data directory: `.toychain`

## Wallet

```powershell
toychain --data-dir demo create-wallet
toychain --data-dir demo address
toychain --data-dir demo wallet-info
toychain --data-dir demo export-public-key [--out public.json]
```

`create-wallet` warns that the private key is unencrypted.

## Transactions

```powershell
toychain --data-dir demo send <tc1-address> 12 [--out tx.json] [--no-submit]
toychain --data-dir demo submit-tx tx.json
toychain --data-dir demo import-tx tx.json
toychain --data-dir demo inspect-tx <tx-id>
toychain --data-dir demo export-tx <tx-id> --out tx.json
```

`inspect-tx` exposes JSON, unsigned bytes, signing payload, signed bytes, and transaction ID.

## Mining and Blocks

```powershell
toychain --data-dir demo mine [--miner ADDRESS] [--difficulty BITS]
toychain --data-dir demo inspect-block <block-hash>
toychain --data-dir demo export-block <block-hash> --out block.json
toychain --data-dir demo import-block block.json
```

## Chain and Accounts

```powershell
toychain --data-dir demo show-chain
toychain --data-dir demo show-forks
toychain --data-dir demo show-canonical-tip
toychain --data-dir demo validate-chain [--explain]
toychain --data-dir demo balance [address]
toychain --data-dir demo account-nonce [address]
```

## Merkle Proofs

```powershell
toychain --data-dir demo merkle-proof <block-hash> <tx-id> [--out proof.json]
toychain verify-proof proof.json [--expect-root ROOT] [--expect-tx TXID]
```

## Mempool

```powershell
toychain --data-dir demo mempool show
toychain --data-dir demo mempool validate
toychain --data-dir demo mempool clear --dangerous
```

## Node Process

```powershell
toychain --data-dir demo node start [--port 9001]
toychain --data-dir demo node status
toychain --data-dir demo node stop
```

## Local Network

```powershell
toychain --data-dir localnet network run-local --nodes 3 [--base-port 9001]
toychain --data-dir localnet network status
toychain --data-dir localnet network stop-local
```

## Debugging

```powershell
toychain --data-dir demo debug-bytes tx <tx-id>
toychain --data-dir demo debug-bytes block <block-hash>
toychain debug-disassemble validate-block
toychain debug-consensus block-tree.json
```

`debug-disassemble` displays Python bytecode only; it is not a cryptographic or consensus proof.

## Public Python Surface

```python
from toychain.models import Transaction, BlockHeader, Block, MerkleProof
from toychain.codec import encode_transaction, parse_transaction, encode_block, parse_block
from toychain.crypto import generate_keypair, address_from_public_key
from toychain.transactions import ChainState, create_signed_transaction
from toychain.merkle import build_merkle_root, create_merkle_proof, verify_merkle_proof
from toychain.block import make_block_candidate, mine_block
from toychain.chain import Blockchain, validate_block
from toychain.mempool import Mempool
from toychain.node import Node
```

## Exit Codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | Runtime blockchain error |
| 2 | Usage/argument error |

## Write Boundaries

- Wallet creation writes `wallet.json`.
- Send/submit/import mutates the mempool.
- Mining/import-block mutates chain and mempool.
- Node/network commands start or stop subprocesses.
- Read-only inspection commands do not mutate node state.

---

# Runbook
## App — Blockchain
**Distributed Ledger Systems Group | Document 4 of 5**

---

## Install

```powershell
python -m pip install -e ".[dev]"
```

For pinned dependencies:

```powershell
python -m pip install -r requirements.lock
python -m pip install -e .
```

## First Chain

```powershell
toychain --data-dir demo create-wallet
toychain --data-dir demo mine --difficulty 8
toychain --data-dir demo balance
toychain --data-dir demo show-chain
toychain --data-dir demo validate-chain --explain
```

Expected: first normal block pays reward 50 and full validation passes.

## Transfer Demo

```powershell
toychain --data-dir receiver create-wallet
toychain --data-dir receiver address
toychain --data-dir demo send <receiver-address> 12 --out tx.json
toychain --data-dir demo mempool show
toychain --data-dir demo mine --difficulty 8
```

Core nodes do not gossip. Export/import blocks explicitly when demonstrating state transfer between directories.

## Merkle Proof Demo

```powershell
toychain --data-dir demo inspect-block <block-hash>
toychain --data-dir demo merkle-proof <block-hash> <tx-id> --out proof.json
toychain verify-proof proof.json
```

Tampering with the root, ID, index, sibling, or side flag must fail verification.

## Fork/Reorg Demo

Create two isolated directories from a shared history, mine competing branches, import both branches into one node, then run:

```powershell
toychain --data-dir node1 show-forks
toychain --data-dir node1 show-canonical-tip
toychain --data-dir node1 validate-chain --explain
```

Expected: greatest cumulative work wins; equal work uses lowest hash; orphaned valid transactions may re-enter the mempool.

## Daemon Demo

```powershell
toychain --data-dir demo node start
toychain --data-dir demo node status
toychain --data-dir demo show-chain
toychain --data-dir demo node stop
```

Read-only commands are allowed while running. State-changing commands are rejected because the daemon owns the directory.

## Local Network Demo

```powershell
toychain --data-dir localnet network run-local --nodes 3 --base-port 9001
toychain --data-dir localnet network status
toychain --data-dir localnet network stop-local
```

Each node has isolated wallet, chain, mempool, lock, PID, and log files.

## Full Verification

```powershell
toychain --data-dir demo validate-chain
toychain --data-dir demo validate-chain --explain
```

The verifier checks genesis, parent links, PoW, Merkle roots, coinbase rules, signatures, sender derivation, balances, nonces, duplicate prevention, and cached-state equivalence.

## Tests and CI

```powershell
python -m pytest
python -m mypy
```

CI runs both on Python 3.11, 3.12, and 3.13.

## Troubleshooting

### No wallet

```powershell
toychain --data-dir demo create-wallet
```

### Insufficient balance

Inspect wallet and pending reservations:

```powershell
toychain --data-dir demo wallet-info
toychain --data-dir demo mempool show
```

### Wrong nonce

Create a new transaction from current state. Stale exported transactions, pending conflicts, confirmations, or reorgs can invalidate a nonce.

### Invalid signature

Do not edit transaction fields after signing. Rebuild and sign from the original wallet.

### Unknown parent

Import parent blocks before children.

### Future timestamp

Correct the clock or use a block whose timestamp is within the acceptance drift bound.

### Directory locked

```powershell
toychain --data-dir demo node status
toychain --data-dir demo node stop
```

### Fast load succeeds but validation fails

The persisted store may have been altered. Use `validate-chain --explain`, inspect the failing block, and restore from a known-good export.

## Maintenance Rules

- Never sign or hash JSON.
- Preserve codec versioning and domain prefixes.
- Keep equal-work tie-breaking deterministic.
- Keep replay clock-independent.
- Preserve one first-position coinbase.
- Preserve reorg-aware mempool repair.
- Preserve the fast-load/full-verification trust boundary.
- Add an ADR before introducing gossip, fees, difficulty adjustment, smart contracts, or encrypted wallets.

---

# Lessons Learned
## App — Blockchain
**Distributed Ledger Systems Group | Document 5 of 5**

---

## Why This Design Works

A blockchain is not merely a list of hashes. It is a deterministic state machine whose inputs are validated blocks. The core design succeeds because representation, authorization, state transition, commitment, work scoring, branch selection, and persistence are separated.

Canonical bytes were the most important choice. If equivalent transactions can serialize differently, signatures, IDs, Merkle roots, and block hashes become ambiguous.

Caching state per block was equally important. A side-branch block must be validated against its own parent state, not the current canonical tip.

Reorganization handling showed that consensus changes affect application state: pending transactions must be removed, recovered, or rejected after the canonical branch changes.

## What Was Intentionally Omitted

Financial value, production security, P2P gossip, fees, difficulty retargeting, UTXO accounting, smart contracts, encrypted wallets, seed phrases, hardware signers, pruning, and adversarial network defenses.

## Biggest Weaknesses

1. **Wallet security:** Private keys are unencrypted local JSON.
2. **No real network:** Local subprocesses do not exchange blocks automatically.
3. **Trusted fast load:** A tampered store may load until full validation is requested.
4. **No economics:** Educational work values do not create real security.

## Scaling Considerations

For larger chains:

- use an embedded database for block metadata;
- add validated snapshots/checkpoints;
- prune old branch state;
- avoid holding every branch state fully in memory.

For larger mempools:

- index sender/nonces directly;
- add size bounds and deterministic eviction;
- introduce fee policy only through an explicit design change.

For networking:

- define a versioned wire protocol;
- validate all received objects before storage;
- add orphan handling, rate limits, and peer identity;
- keep consensus and relay policy separate.

For wallet safety:

- encrypt keys with a passphrase-derived key;
- separate signing from node storage;
- support external or hardware signers.

## Next Refactors

1. Validated state snapshots.
2. Versioned transaction/block network protocol.
3. Encrypted wallet vault.
4. Orphan block pool.
5. Published consensus test vectors for bytes, hashes, signatures, Merkle proofs, and block validity.

## Key Lessons

- Serialization is part of consensus.
- Hashes provide identity/integrity; signatures provide authorization.
- State should be replayable from canonical history.
- Merkle trees commit transaction order and support compact proofs.
- Fork choice requires a deterministic tie-breaker.
- Reorganizations require mempool repair.
- Acceptance-time clock rules must not contaminate historical replay.
- Fast persistence paths need an explicit trust boundary.
- A local process cluster is not a distributed network.
- Real cryptography does not automatically make a project production-safe.

---

*Constitution v2.0 checklist: explicit trade-offs, verification, progressive complexity, honest scope, and reproducible behavior.*
