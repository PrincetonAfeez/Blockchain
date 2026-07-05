"""Mempool-related functionality."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from .chain import Blockchain, ReorgResult
from .errors import MempoolError, ValidationError
from .models import Transaction
from .transactions import ChainState, apply_normal_transaction


@dataclass(frozen=True, slots=True)
class MempoolRepairReport:
    accepted: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


class Mempool:
    def __init__(self, transactions: Iterable[Transaction] = ()) -> None:
        self._transactions: OrderedDict[str, Transaction] = OrderedDict(
            (tx.tx_id, tx) for tx in transactions
        )

    def __len__(self) -> int:
        return len(self._transactions)

    def __iter__(self):
        return iter(self._transactions.values())

    def transactions(self) -> tuple[Transaction, ...]:
        return tuple(self._transactions.values())

    def get(self, tx_id: str) -> Transaction | None:
        return self._transactions.get(tx_id)

    def projected_state(self, canonical_state: ChainState) -> ChainState:
        state = canonical_state.copy()
        for transaction in self._transactions.values():
            apply_normal_transaction(transaction, state)
        return state

    def submit(self, transaction: Transaction, canonical_state: ChainState) -> str:
        if transaction.is_coinbase:
            raise MempoolError("Coinbase transactions cannot enter the mempool")
        if transaction.tx_id in self._transactions:
            raise MempoolError("Transaction ID is already pending")
        if any(
            tx.sender == transaction.sender and tx.nonce == transaction.nonce
            for tx in self._transactions.values()
        ):
            raise MempoolError("A pending transaction already uses this sender nonce")
        try:
            state = self.projected_state(canonical_state)
            apply_normal_transaction(transaction, state)
        except ValidationError as exc:
            raise MempoolError(str(exc)) from exc
        self._transactions[transaction.tx_id] = transaction
        return transaction.tx_id

    def remove_confirmed(self, state: ChainState) -> None:
        for tx_id in list(self._transactions):
            if tx_id in state.confirmed_tx_ids:
                del self._transactions[tx_id]

    def clear(self) -> None:
        self._transactions.clear()

    def revalidate(
        self,
        canonical_state: ChainState,
        extra_transactions: Iterable[Transaction] = (),
    ) -> MempoolRepairReport:
        candidates = [*self._transactions.values(), *extra_transactions]
        self.clear()
        accepted: list[str] = []
        rejected: list[tuple[str, str]] = []
        seen: set[str] = set()
        for transaction in candidates:
            if transaction.tx_id in seen:
                continue
            seen.add(transaction.tx_id)
            if transaction.tx_id in canonical_state.confirmed_tx_ids:
                continue
            try:
                accepted.append(self.submit(transaction, canonical_state))
            except MempoolError as exc:
                rejected.append((transaction.tx_id, str(exc)))
        return MempoolRepairReport(tuple(accepted), tuple(rejected))

    def repair_after_tip_change(
        self,
        chain: Blockchain,
        reorg: ReorgResult,
    ) -> MempoolRepairReport:
        orphaned_transactions: list[Transaction] = []
        for block_hash in reversed(reorg.orphaned_hashes):
            orphaned_transactions.extend(
                tx for tx in chain.blocks[block_hash].transactions if not tx.is_coinbase
            )
        return self.revalidate(chain.state, orphaned_transactions)

