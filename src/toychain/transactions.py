"""Transactions-related functionality."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import (
    BLOCK_REWARD,
    COINBASE_EXTRANONCE_BYTES,
    COINBASE_SENDER,
    FORMAT_VERSION,
    TX_SIGNED_DOMAIN,
    TX_UNSIGNED_DOMAIN,
)
from .crypto import address_from_public_key, is_valid_address, sha256, sign, verify
from .errors import ValidationError
from .models import Transaction


@dataclass(slots=True)
class ChainState:
    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    confirmed_tx_ids: set[str] = field(default_factory=set)
    confirmed_sender_nonces: set[tuple[str, int]] = field(default_factory=set)

    def copy(self) -> "ChainState":
        return ChainState(
            balances=dict(self.balances),
            nonces=dict(self.nonces),
            confirmed_tx_ids=set(self.confirmed_tx_ids),
            confirmed_sender_nonces=set(self.confirmed_sender_nonces),
        )


def signing_payload(tx: Transaction) -> bytes:
    return TX_UNSIGNED_DOMAIN + tx.unsigned_bytes()


def transaction_id(tx: Transaction) -> bytes:
    return sha256(TX_SIGNED_DOMAIN + tx.signed_bytes())


def create_signed_transaction(
    *,
    private_key: bytes,
    public_key: bytes,
    sender: str,
    recipient: str,
    amount: int,
    nonce: int,
) -> Transaction:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValidationError("Transaction amount must be a positive integer")
    if not is_valid_address(recipient):
        raise ValidationError(
            f"Recipient is not a valid toychain address: {recipient!r}"
        )
    unsigned = Transaction(
        version=FORMAT_VERSION,
        sender=sender,
        recipient=recipient,
        amount=amount,
        nonce=nonce,
        public_key=public_key,
        signature=b"",
    )
    return unsigned.with_signature(sign(private_key, signing_payload(unsigned)))


def create_coinbase(
    recipient: str,
    height: int,
    amount: int = BLOCK_REWARD,
    *,
    extranonce: bytes = b"",
) -> Transaction:
    # The coinbase has no signing key, so (like Bitcoin's coinbase scriptSig)
    # its public-key slot carries arbitrary "extranonce" bytes. This makes the
    # coinbase tx_id unique per block, so competing same-height blocks from the
    # same miner do not collide. See make_block_candidate for the value used.
    return Transaction(
        sender=COINBASE_SENDER,
        recipient=recipient,
        amount=amount,
        nonce=height,
        public_key=extranonce,
        signature=b"",
    )


def validate_transaction_authenticity(tx: Transaction) -> None:
    if tx.is_coinbase:
        raise ValidationError("Coinbase transaction is not a normal signed transaction")
    if tx.version != FORMAT_VERSION:
        raise ValidationError(f"Unsupported transaction version: {tx.version}")
    if not tx.sender or not tx.recipient:
        raise ValidationError("Sender and recipient must be non-empty")
    if tx.amount <= 0:
        raise ValidationError("Transaction amount must be positive")
    if len(tx.public_key) != 32:
        raise ValidationError("Transaction public key must be exactly 32 bytes")
    if len(tx.signature) != 64:
        raise ValidationError("Transaction signature must be exactly 64 bytes")
    try:
        derived_address = address_from_public_key(tx.public_key)
    except Exception as exc:
        raise ValidationError("Transaction contains an invalid public key") from exc
    if derived_address != tx.sender:
        raise ValidationError("Public key does not derive the sender address")
    if not verify(tx.public_key, tx.signature, signing_payload(tx)):
        raise ValidationError("Transaction signature is invalid")


def validate_transaction_against_state(tx: Transaction, state: ChainState) -> None:
    validate_transaction_authenticity(tx)
    tx_id = tx.tx_id
    if tx_id in state.confirmed_tx_ids:
        raise ValidationError("Transaction ID is already confirmed")
    sender_nonce = (tx.sender, tx.nonce)
    if sender_nonce in state.confirmed_sender_nonces:
        raise ValidationError("Sender nonce is already confirmed")
    expected_nonce = state.nonces.get(tx.sender, 0)
    if tx.nonce != expected_nonce:
        raise ValidationError(
            f"Wrong nonce for {tx.sender}: expected {expected_nonce}, got {tx.nonce}"
        )
    available = state.balances.get(tx.sender, 0)
    if available < tx.amount:
        raise ValidationError(
            f"Insufficient balance for {tx.sender}: has {available}, needs {tx.amount}"
        )


def apply_normal_effects(tx: Transaction, state: ChainState) -> None:
    """Mutate state for a normal transaction WITHOUT validating it.

    Shared by the validated path and the trusted replay used when loading a
    node's own persisted (already-validated) chain. Keeping the mutation in one
    place guarantees both paths derive identical balances, nonces, and sets.
    """
    state.balances[tx.sender] = state.balances.get(tx.sender, 0) - tx.amount
    state.balances[tx.recipient] = state.balances.get(tx.recipient, 0) + tx.amount
    state.nonces[tx.sender] = tx.nonce + 1
    state.confirmed_tx_ids.add(tx.tx_id)
    state.confirmed_sender_nonces.add((tx.sender, tx.nonce))


def apply_coinbase_effects(tx: Transaction, state: ChainState) -> None:
    """Mutate state for a coinbase WITHOUT validating it (see apply_normal_effects)."""
    state.balances[tx.recipient] = state.balances.get(tx.recipient, 0) + tx.amount
    state.confirmed_tx_ids.add(tx.tx_id)


def apply_normal_transaction(tx: Transaction, state: ChainState) -> None:
    validate_transaction_against_state(tx, state)
    apply_normal_effects(tx, state)


def apply_coinbase(tx: Transaction, state: ChainState, *, expected_height: int) -> None:
    if not tx.is_coinbase:
        raise ValidationError("First block transaction must be coinbase")
    if tx.version != FORMAT_VERSION:
        raise ValidationError(f"Unsupported coinbase version: {tx.version}")
    if tx.signature:
        raise ValidationError("Coinbase must not contain a signature")
    if len(tx.public_key) not in (0, COINBASE_EXTRANONCE_BYTES):
        raise ValidationError(
            f"Coinbase extranonce must be empty or {COINBASE_EXTRANONCE_BYTES} bytes"
        )
    if not tx.recipient:
        raise ValidationError("Coinbase recipient must be non-empty")
    if tx.amount != BLOCK_REWARD:
        raise ValidationError(
            f"Coinbase amount must equal fixed block reward {BLOCK_REWARD}"
        )
    if tx.nonce != expected_height:
        raise ValidationError(
            f"Coinbase nonce must equal block height {expected_height}"
        )
    if tx.tx_id in state.confirmed_tx_ids:
        raise ValidationError("Coinbase transaction ID is already confirmed")
    apply_coinbase_effects(tx, state)

