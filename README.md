# Toychain

Toychain is a complete educational blockchain CLI written in Python. It uses
real Ed25519 signatures and SHA-256, but it is intentionally **not money** and
has no production security or financial value.

Its systems-programming focus is deterministic representation and validation:

- strict, versioned binary transaction and block-header codecs;
- domain-separated hashes and signatures;
- account balances and nonces derived by replay;
- Merkle roots and logarithmic membership proofs;
- proof-of-work mining and cheap verification;
- a block tree with cumulative-work fork choice and deterministic tie-breaking;
- reorganization handling that repairs the mempool;
- JSON persistence kept separate from consensus bytes;
- isolated local node subprocesses with PID, lock, config, and log files;
- bytecode inspection for selected core functions.

## Install and start

```powershell
python -m pip install -e ".[dev]"
toychain --version
toychain --data-dir demo create-wallet
toychain --data-dir demo mine --difficulty 8
toychain --data-dir demo balance
```

For a reproducible runtime environment, install the pinned dependency closure
with `python -m pip install -r requirements.lock` before `pip install -e .`.

Mining the first normal block pays the local wallet the fixed reward of 50.
You can then create another wallet in a different data directory and transfer
integer units (`send` rejects a recipient that is not a well-formed `tc1`
address, so a typo fails instead of burning coins):

```powershell
toychain --data-dir receiver create-wallet
toychain --data-dir demo send <receiver-address> 12 --out tx.json
toychain --data-dir demo mine --difficulty 8
toychain --data-dir demo validate-chain --explain
```

Use `toychain --help` and each subcommand's `--help` for the complete command
surface. The CLI uses standard exit codes: `0` success, `1` a runtime error
(validation/crypto/consensus/mempool/persistence), `2` a usage error (unknown
command or bad arguments). Useful demonstrations include:

```powershell
toychain --data-dir demo show-chain
toychain --data-dir demo show-forks
toychain --data-dir demo debug-bytes block <block-hash>
toychain debug-disassemble validate-block
toychain --data-dir localnet network run-local --nodes 3
toychain --data-dir localnet network status
toychain --data-dir localnet network stop-local
```

## Consensus-critical bytes

Persistence is readable JSON, but JSON is never signed or hashed. Consensus
objects use one strict binary encoding with fixed field order, big-endian
unsigned integers, UTF-8 strings and byte strings prefixed by 32-bit lengths,
and explicit record/version bytes. Parsers reject truncation, extra bytes,
unknown versions, invalid UTF-8, and oversized fields.

Domain prefixes distinguish cryptographic jobs:

```text
signature = Ed25519.sign("TX_UNSIGNED_V1\0" || unsigned_tx_bytes)
tx_id     = SHA256("TX_SIGNED_V1\0" || signed_tx_bytes)
leaf      = SHA256("MERKLE_LEAF_V1\0" || tx_id)
node      = SHA256("MERKLE_NODE_V1\0" || left || right)
block     = SHA256("BLOCK_HEADER_V1\0" || canonical_header_bytes)
address   = "tc1" || first_20_bytes(SHA256("ADDRESS_V1\0" || public_key))
```

Hashing provides integrity, addressing, and block linking. Signatures provide
authorization: mutating a signed transaction invalidates it. Proof of work
adds computational cost to a block. Mining may require many hashes, while
verification recomputes one header hash.

The coinbase transaction has no signing key, so (like Bitcoin's coinbase
`scriptSig`) its public-key slot carries 8 bytes of *extranonce*, derived from
the block's parent hash and timestamp. This keeps the coinbase transaction ID
distinct across competing blocks: blocks on different parents or with different
timestamps never collide, so only re-mining byte-identical block content could.

A block whose timestamp is earlier than its parent is always rejected. A block
more than two hours ahead of the accepting node's clock is also rejected, but
**only when a fresh block is accepted** (mined or imported): this future-drift
bound is the single clock-relative rule. Replaying or reloading already-accepted
blocks does not consult the clock, so full-chain validation stays deterministic
and clock-independent.

## Merkle trees

Each block commits to the exact ordered transaction list. When a level has an
odd number of hashes, Toychain duplicates the final hash at that level. A
membership proof carries one sibling per level, so its size and verification
work are `O(log n)`. Any transaction mutation changes its ID, leaf, ancestors,
root, block header, and block hash.

## Fork choice and reorganizations

Known blocks form a tree. The work of a block is `2 ** difficulty_bits`; a
branch's cumulative work is the sum from genesis. Toychain chooses the valid
tip with greatest cumulative work, even if it has fewer blocks. Equal-work
tips are resolved by the lexicographically lowest block hash.

On a tip change, Toychain finds the lowest common ancestor, identifies
disconnected and connected blocks, adopts the replayed state cached for the
new branch, removes newly confirmed pending transactions, and attempts to
return valid transactions from orphaned blocks to the mempool.

This is honest local fork-choice consensus over known blocks, not a hardened
distributed consensus protocol.

## Persistence and processes

Each data directory is isolated:

```text
node1/
  config.json
  node.pid
  node.lock        # held by a running node process
  node.writelock   # short-lived; serializes one-off state-changing CLI commands
  node.log
  wallet.json
  chain/
    blocks/<hash>.json
    index.json           # block metadata; the tip is derived, not stored here
    canonical_tip.txt    # human-readable mirror of the derived tip
  mempool/
    transactions.json
```

The authoritative canonical tip is **derived** from the validated block set by
fork choice; `canonical_tip.txt` is a human-readable mirror, not a trusted input
(so there is no read-during-write
race on a separate tip file). Loading trusts the node's own already-validated
store — it rebuilds the block tree and replays transactions for balances and
nonces but skips proof-of-work, Merkle, and signature re-checks — which keeps
every command cheap. `validate-chain` is the full verifier: it re-checks the
entire chain from genesis and confirms the trusted state matches a clean replay.

A running node owns its data directory through an exclusive `node.lock`. While
it runs it does real work: it drains its own mempool by mining pending
transactions to its wallet (logging `mining disabled` if the directory has no
wallet) and writes a periodic heartbeat to `node.log`. Read-only commands
(`balance`, `show-chain`, `validate-chain`, `show-forks`, `inspect-*`,
`merkle-proof`, …) run safely alongside a live node and never write node state
to disk; state-changing commands (`send`, `mine`, `submit-tx`, `import-block`,
`mempool validate/clear`) are refused while the node owns the directory.
Independently of the daemon, each one-off state-changing command briefly holds
`node.writelock` so two concurrent CLI writers cannot interleave a
read-modify-write; a stale lock left by a crashed writer is reclaimed
automatically. `node start`, `node stop`, and `network run-local` exercise
subprocess lifecycle, signal/stop-file shutdown, and isolation. Nodes do not
gossip in the core project; blocks and transactions move between them with
explicit import/export commands.

## Security and scope

This is an educational toy, not money. Wallet files store the Ed25519 private
key unencrypted (base64 in `wallet.json`); on POSIX the file is chmod-ed to
`0600`, but on Windows that is a no-op, so never place a key of value in a
toychain wallet. `create-wallet` prints a reminder to that effect.

## Bytecode

`debug-disassemble` uses Python's `dis` module. It shows Python VM instructions
and can offer implementation/performance intuition. It does not show native
machine code, prove cryptographic security, or establish consensus correctness.

## Test

```powershell
pytest
```

The suite protects canonical encoding, malformed parsing, signature tampering,
Merkle proofs, PoW, deterministic genesis, replayed balances/nonces, mempool
conflicts, most-work forks, reorg repair, persistence, and process lifecycle.
It also covers acceptance-time drift, clock-independent replay, parent-bound
coinbase uniqueness, integer-only parsing, the `validate-chain --explain` trace,
read-only node isolation, a running node mining its mempool, the derived
canonical tip, and equivalence of the trusted fast-load with full validation.

## Design decisions

The non-obvious decisions and their trade-offs are recorded as ADRs in
[`docs/adr/`](docs/adr/README.md): canonical serialization and domain
separation, the account model, most-work fork choice, the derived tip and
trusted fast-load trust boundary, and acceptance-only timestamp drift.

## License

MIT — see [LICENSE](LICENSE).

