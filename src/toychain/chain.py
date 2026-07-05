"""Chain-related functionality."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .block import GENESIS_BLOCK, block_work, meets_difficulty
from .constants import (
    FORMAT_VERSION,
    MAX_DIFFICULTY_BITS,
    MAX_TIMESTAMP_DRIFT_SECONDS,
    ZERO_HASH,
)
from .consensus import ChainScore, lowest_common_ancestor, select_best_chain
from .errors import ConsensusError, ValidationError
from .merkle import build_merkle_root
from .models import Block
from .transactions import (
    ChainState,
    apply_coinbase,
    apply_coinbase_effects,
    apply_normal_effects,
    apply_normal_transaction,
)


@dataclass(frozen=True, slots=True)
class BlockMetadata:
    parent_hash: str | None
    height: int
    cumulative_work: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_hash": self.parent_hash,
            "height": self.height,
            "cumulative_work": self.cumulative_work,
        }


@dataclass(frozen=True, slots=True)
class ReorgResult:
    old_tip: str
    new_tip: str
    common_ancestor: str
    orphaned_hashes: tuple[str, ...]
    connected_hashes: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.old_tip != self.new_tip

    @property
    def is_reorg(self) -> bool:
        return bool(self.orphaned_hashes)


@dataclass(frozen=True, slots=True)
class AddBlockResult:
    block_hash: str
    height: int
    cumulative_work: int
    became_canonical: bool
    reorg: ReorgResult | None
    already_known: bool = False


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    checked_blocks: int
    tip_hash: str
    message: str
    checked_canonical_blocks: int = 0
    checked_fork_blocks: int = 0
    invalid_block_hash: str | None = None
    steps: tuple[str, ...] = ()


def validate_block(
    block: Block,
    *,
    parent: Block,
    parent_state: ChainState,
    height: int,
    now: int | None = None,
) -> ChainState:
    header = block.header
    if header.version != FORMAT_VERSION:
        raise ValidationError(f"Unsupported block header version: {header.version}")
    if len(header.previous_hash) != 32 or len(header.merkle_root) != 32:
        raise ValidationError("Block header hashes must be exactly 32 bytes")
    if header.previous_hash.hex() != parent.hash:
        raise ValidationError("Block previous hash does not match its parent")
    if not 0 <= header.difficulty_bits <= MAX_DIFFICULTY_BITS:
        raise ValidationError(
            f"Block difficulty must be between 0 and {MAX_DIFFICULTY_BITS}"
        )
    if not meets_difficulty(header.hash_bytes(), header.difficulty_bits):
        raise ValidationError("Block proof of work does not meet its difficulty")
    if header.timestamp < parent.header.timestamp:
        raise ValidationError("Block timestamp is earlier than its parent timestamp")
    # The future-drift bound is a tip-acceptance policy, not a replay rule: it is
    # only applied when a caller supplies the current time (accepting a fresh
    # block). Replaying or loading already-accepted blocks passes now=None so that
    # validation stays deterministic and clock-independent.
    if now is not None and header.timestamp > now + MAX_TIMESTAMP_DRIFT_SECONDS:
        raise ValidationError("Block timestamp is too far in the future")
    if not block.transactions:
        raise ValidationError("Block must contain a coinbase transaction")

    coinbase_count = sum(tx.is_coinbase for tx in block.transactions)
    if coinbase_count != 1:
        raise ValidationError("Block must contain exactly one coinbase transaction")
    if not block.transactions[0].is_coinbase:
        raise ValidationError("Coinbase transaction must be first in the block")

    expected_root = build_merkle_root(
        tuple(tx.tx_id_bytes() for tx in block.transactions)
    )
    if expected_root != header.merkle_root:
        raise ValidationError("Block Merkle root does not match its transactions")

    state = parent_state.copy()
    apply_coinbase(block.transactions[0], state, expected_height=height)
    for transaction in block.transactions[1:]:
        if transaction.is_coinbase:
            raise ValidationError("Coinbase transaction may only appear first")
        apply_normal_transaction(transaction, state)
    return state


def validate_genesis(block: Block) -> ChainState:
    if block != GENESIS_BLOCK:
        raise ValidationError("Genesis block does not match the deterministic genesis")
    if block.header.previous_hash != ZERO_HASH:
        raise ValidationError("Genesis previous hash must be all zero bytes")
    if not meets_difficulty(block.header.hash_bytes(), block.header.difficulty_bits):
        raise ValidationError("Genesis proof of work is invalid")
    expected_root = build_merkle_root(
        tuple(tx.tx_id_bytes() for tx in block.transactions)
    )
    if expected_root != block.header.merkle_root:
        raise ValidationError("Genesis Merkle root is invalid")
    state = ChainState()
    if len(block.transactions) != 1:
        raise ValidationError("Genesis must contain exactly one coinbase")
    apply_coinbase(block.transactions[0], state, expected_height=0)
    return state


class Blockchain:
    def __init__(self) -> None:
        genesis_state = validate_genesis(GENESIS_BLOCK)
        self.blocks: dict[str, Block] = {GENESIS_BLOCK.hash: GENESIS_BLOCK}
        self.metadata: dict[str, BlockMetadata] = {
            GENESIS_BLOCK.hash: BlockMetadata(
                parent_hash=None,
                height=0,
                cumulative_work=block_work(GENESIS_BLOCK),
            )
        }
        self.children: dict[str, set[str]] = {GENESIS_BLOCK.hash: set()}
        self._states: dict[str, ChainState] = {GENESIS_BLOCK.hash: genesis_state}
        self.tip_hash = GENESIS_BLOCK.hash

    @property
    def genesis_hash(self) -> str:
        return GENESIS_BLOCK.hash

    @property
    def tip(self) -> Block:
        return self.blocks[self.tip_hash]

    @property
    def height(self) -> int:
        return self.metadata[self.tip_hash].height

    @property
    def state(self) -> ChainState:
        return self._states[self.tip_hash].copy()

    def state_at(self, block_hash: str) -> ChainState:
        try:
            return self._states[block_hash].copy()
        except KeyError as exc:
            raise ConsensusError(f"Unknown block: {block_hash}") from exc

    def _best_tip(self) -> str:
        # Every stored block is valid (invalid blocks are rejected before they
        # are ever stored), so all known tips are eligible candidates. The
        # validity filter still lives in select_best_chain for debug-consensus,
        # which can be fed hypothetical invalid tips.
        scores = {
            block_hash: ChainScore(
                cumulative_work=metadata.cumulative_work,
                block_hash=block_hash,
            )
            for block_hash, metadata in self.metadata.items()
        }
        return select_best_chain(scores)

    def _path_to_root(self, tip_hash: str) -> list[str]:
        path: list[str] = []
        cursor: str | None = tip_hash
        while cursor is not None:
            path.append(cursor)
            cursor = self.metadata[cursor].parent_hash
        return path

    def _describe_tip_change(self, old_tip: str, new_tip: str) -> ReorgResult:
        parents = {
            block_hash: metadata.parent_hash
            for block_hash, metadata in self.metadata.items()
        }
        ancestor = lowest_common_ancestor(old_tip, new_tip, parents)
        old_path = self._path_to_root(old_tip)
        new_path = self._path_to_root(new_tip)
        orphaned = tuple(old_path[: old_path.index(ancestor)])
        connected_reverse = new_path[: new_path.index(ancestor)]
        connected = tuple(reversed(connected_reverse))
        return ReorgResult(
            old_tip=old_tip,
            new_tip=new_tip,
            common_ancestor=ancestor,
            orphaned_hashes=orphaned,
            connected_hashes=connected,
        )

    def add_block(self, block: Block, *, now: int | None = None) -> AddBlockResult:
        block_hash = block.hash
        if block_hash in self.blocks:
            metadata = self.metadata[block_hash]
            return AddBlockResult(
                block_hash=block_hash,
                height=metadata.height,
                cumulative_work=metadata.cumulative_work,
                became_canonical=block_hash == self.tip_hash,
                reorg=None,
                already_known=True,
            )

        parent_hash = block.header.previous_hash.hex()
        if parent_hash not in self.blocks:
            raise ConsensusError(f"Block parent is unknown: {parent_hash}")
        parent = self.blocks[parent_hash]
        parent_metadata = self.metadata[parent_hash]
        height = parent_metadata.height + 1
        state = validate_block(
            block,
            parent=parent,
            parent_state=self._states[parent_hash],
            height=height,
            now=now,
        )
        metadata = BlockMetadata(
            parent_hash=parent_hash,
            height=height,
            cumulative_work=parent_metadata.cumulative_work + block_work(block),
        )

        self.blocks[block_hash] = block
        self.metadata[block_hash] = metadata
        self._states[block_hash] = state
        self.children.setdefault(parent_hash, set()).add(block_hash)
        self.children.setdefault(block_hash, set())

        old_tip = self.tip_hash
        new_tip = self._best_tip()
        reorg: ReorgResult | None = None
        if new_tip != old_tip:
            reorg = self._describe_tip_change(old_tip, new_tip)
            self.tip_hash = new_tip
        return AddBlockResult(
            block_hash=block_hash,
            height=height,
            cumulative_work=metadata.cumulative_work,
            became_canonical=new_tip == block_hash,
            reorg=reorg,
        )

    def canonical_hashes(self) -> tuple[str, ...]:
        return tuple(reversed(self._path_to_root(self.tip_hash)))

    def canonical_blocks(self) -> tuple[Block, ...]:
        return tuple(self.blocks[block_hash] for block_hash in self.canonical_hashes())

    def validate_canonical_chain(self, *, explain: bool = False) -> ValidationReport:
        return self.validate_all_blocks(explain=explain)

    def validate_all_blocks(self, *, explain: bool = False) -> ValidationReport:
        canonical_hashes = set(self.canonical_hashes())
        ordered_hashes = sorted(
            self.metadata,
            key=lambda block_hash: (self.metadata[block_hash].height, block_hash),
        )
        steps: list[str] = []
        checked = 0
        checked_canonical = 0
        checked_fork = 0
        recomputed_states: dict[str, ChainState] = {}
        recomputed_metadata: dict[str, BlockMetadata] = {}

        for block_hash in ordered_hashes:
            block = self.blocks[block_hash]
            stored_metadata = self.metadata[block_hash]
            on_canonical = block_hash in canonical_hashes
            branch = "canonical" if on_canonical else "fork"
            try:
                parent_hash = block.header.previous_hash.hex()
                if block_hash == self.genesis_hash:
                    state = validate_genesis(block)
                    expected_metadata = BlockMetadata(
                        parent_hash=None,
                        height=0,
                        cumulative_work=block_work(block),
                    )
                else:
                    if parent_hash not in recomputed_states:
                        raise ValidationError(
                            f"Block parent is unknown or out of order: {parent_hash}"
                        )
                    if stored_metadata.parent_hash != parent_hash:
                        raise ValidationError("Stored parent_hash does not match block header")
                    parent = self.blocks[parent_hash]
                    parent_metadata = recomputed_metadata[parent_hash]
                    height = parent_metadata.height + 1
                    state = validate_block(
                        block,
                        parent=parent,
                        parent_state=recomputed_states[parent_hash],
                        height=height,
                        now=None,
                    )
                    expected_metadata = BlockMetadata(
                        parent_hash=parent_hash,
                        height=height,
                        cumulative_work=parent_metadata.cumulative_work + block_work(block),
                    )

                if stored_metadata != expected_metadata:
                    raise ValidationError(
                        "Stored block metadata does not match recomputed metadata"
                    )

                recomputed_states[block_hash] = state
                recomputed_metadata[block_hash] = expected_metadata
                checked += 1
                if on_canonical:
                    checked_canonical += 1
                else:
                    checked_fork += 1
                steps.append(
                    f"height {expected_metadata.height} {block_hash[:12]} {branch} ok "
                    f"({len(block.transactions)} tx)"
                )
            except ValidationError as exc:
                steps.append(
                    f"height {stored_metadata.height} {block_hash[:12]} {branch} FAILED: {exc}"
                )
                return ValidationReport(
                    valid=False,
                    checked_blocks=checked,
                    checked_canonical_blocks=checked_canonical,
                    checked_fork_blocks=checked_fork,
                    tip_hash=self.tip_hash,
                    message=str(exc),
                    invalid_block_hash=block_hash,
                    steps=tuple(steps) if explain else (),
                )

        derived_tip = select_best_chain(
            {
                block_hash: ChainScore(
                    cumulative_work=metadata.cumulative_work,
                    block_hash=block_hash,
                )
                for block_hash, metadata in recomputed_metadata.items()
            }
        )
        if derived_tip != self.tip_hash:
            message = (
                f"Derived best tip {derived_tip} does not match loaded tip {self.tip_hash}"
            )
            steps.append(message)
            return ValidationReport(
                valid=False,
                checked_blocks=checked,
                checked_canonical_blocks=checked_canonical,
                checked_fork_blocks=checked_fork,
                tip_hash=self.tip_hash,
                message=message,
                invalid_block_hash=None,
                steps=tuple(steps) if explain else (),
            )

        canonical_state = recomputed_states[self.tip_hash]
        if canonical_state.balances != self._states[self.tip_hash].balances:
            message = "Replayed canonical balances differ from cached balances"
            steps.append(message)
            return ValidationReport(
                valid=False,
                checked_blocks=checked,
                checked_canonical_blocks=checked_canonical,
                checked_fork_blocks=checked_fork,
                tip_hash=self.tip_hash,
                message=message,
                invalid_block_hash=self.tip_hash,
                steps=tuple(steps) if explain else (),
            )
        if canonical_state.nonces != self._states[self.tip_hash].nonces:
            message = "Replayed canonical nonces differ from cached nonces"
            steps.append(message)
            return ValidationReport(
                valid=False,
                checked_blocks=checked,
                checked_canonical_blocks=checked_canonical,
                checked_fork_blocks=checked_fork,
                tip_hash=self.tip_hash,
                message=message,
                invalid_block_hash=self.tip_hash,
                steps=tuple(steps) if explain else (),
            )

        return ValidationReport(
            valid=True,
            checked_blocks=checked,
            checked_canonical_blocks=checked_canonical,
            checked_fork_blocks=checked_fork,
            tip_hash=self.tip_hash,
            message=(
                f"All stored blocks are valid "
                f"({checked_canonical} canonical, {checked_fork} fork)"
            ),
            steps=tuple(steps) if explain else (),
        )

    def fork_summary(self) -> list[dict[str, Any]]:
        canonical = set(self.canonical_hashes())
        return [
            {
                "hash": block_hash,
                "parent": metadata.parent_hash,
                "height": metadata.height,
                "cumulative_work": metadata.cumulative_work,
                "canonical": block_hash in canonical,
                "children": sorted(self.children.get(block_hash, set())),
            }
            for block_hash, metadata in sorted(
                self.metadata.items(), key=lambda item: (item[1].height, item[0])
            )
        ]

    def _attach_trusted(self, block: Block) -> None:
        """Attach a block trusting it was already validated when first stored.

        Recomputes height and work cheaply from the block header and replays its
        transactions for balances/nonces, but skips proof-of-work, Merkle root,
        signature, and balance checks. This is the read/reload fast path; full
        re-verification from genesis remains available via validate-chain.
        """
        parent_hash = block.header.previous_hash.hex()
        parent_metadata = self.metadata[parent_hash]
        state = self._states[parent_hash].copy()
        if block.transactions:
            apply_coinbase_effects(block.transactions[0], state)
            for transaction in block.transactions[1:]:
                apply_normal_effects(transaction, state)
        self.blocks[block.hash] = block
        self.metadata[block.hash] = BlockMetadata(
            parent_hash=parent_hash,
            height=parent_metadata.height + 1,
            cumulative_work=parent_metadata.cumulative_work + block_work(block),
        )
        self._states[block.hash] = state
        self.children.setdefault(parent_hash, set()).add(block.hash)
        self.children.setdefault(block.hash, set())
        self.tip_hash = self._best_tip()

    @classmethod
    def from_blocks(
        cls,
        blocks: list[Block],
        tip_hash: str | None = None,
        *,
        validate: bool = True,
    ) -> "Blockchain":
        chain = cls()
        pending = {
            block.hash: block
            for block in blocks
            if block.hash != chain.genesis_hash
        }
        while pending:
            progressed = False
            for block_hash, block in list(pending.items()):
                if block.header.previous_hash.hex() in chain.blocks:
                    if validate:
                        chain.add_block(block)
                    else:
                        chain._attach_trusted(block)
                    del pending[block_hash]
                    progressed = True
            if not progressed:
                missing = ", ".join(sorted(pending))
                raise ConsensusError(f"Could not connect stored block(s): {missing}")
        if tip_hash is not None and tip_hash != chain.tip_hash:
            raise ConsensusError(
                f"Stored canonical tip {tip_hash} disagrees with fork choice {chain.tip_hash}"
            )
        return chain
