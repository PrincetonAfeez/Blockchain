"""Models for the toychain package."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .constants import COINBASE_SENDER, FORMAT_VERSION
from .errors import CodecError


def _hex_bytes(value: Any, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise CodecError(f"{field_name} must be a hexadecimal string")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise CodecError(f"{field_name} is not valid hexadecimal") from exc


def _strict_int(value: Any, field_name: str) -> int:
    # Reject floats (e.g. 5.9) and bools so amounts/nonces are never silently
    # truncated; consensus-critical state is integer-only.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"{field_name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class Transaction:
    sender: str
    recipient: str
    amount: int
    nonce: int
    public_key: bytes = b""
    signature: bytes = b""
    version: int = FORMAT_VERSION

    @property
    def is_coinbase(self) -> bool:
        return self.sender == COINBASE_SENDER

    def unsigned_bytes(self) -> bytes:
        from .codec import encode_unsigned_transaction

        return encode_unsigned_transaction(self)

    def signed_bytes(self) -> bytes:
        from .codec import encode_transaction

        return encode_transaction(self)

    def tx_id_bytes(self) -> bytes:
        from .transactions import transaction_id

        return transaction_id(self)

    @property
    def tx_id(self) -> str:
        return self.tx_id_bytes().hex()

    def with_signature(self, signature: bytes) -> "Transaction":
        return replace(self, signature=signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sender": self.sender,
            "recipient": self.recipient,
            "amount": self.amount,
            "nonce": self.nonce,
            "public_key": self.public_key.hex(),
            "signature": self.signature.hex(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        try:
            return cls(
                version=_strict_int(data["version"], "version"),
                sender=str(data["sender"]),
                recipient=str(data["recipient"]),
                amount=_strict_int(data["amount"], "amount"),
                nonce=_strict_int(data["nonce"], "nonce"),
                public_key=_hex_bytes(data["public_key"], "public_key"),
                signature=_hex_bytes(data["signature"], "signature"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodecError("Malformed transaction object") from exc


@dataclass(frozen=True, slots=True)
class BlockHeader:
    previous_hash: bytes
    merkle_root: bytes
    timestamp: int
    difficulty_bits: int
    nonce: int
    version: int = FORMAT_VERSION

    def canonical_bytes(self) -> bytes:
        from .codec import encode_block_header

        return encode_block_header(self)

    def hash_bytes(self) -> bytes:
        from .block import block_header_hash

        return block_header_hash(self)

    @property
    def hash(self) -> str:
        return self.hash_bytes().hex()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "previous_hash": self.previous_hash.hex(),
            "merkle_root": self.merkle_root.hex(),
            "timestamp": self.timestamp,
            "difficulty_bits": self.difficulty_bits,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlockHeader":
        try:
            return cls(
                version=_strict_int(data["version"], "version"),
                previous_hash=_hex_bytes(data["previous_hash"], "previous_hash"),
                merkle_root=_hex_bytes(data["merkle_root"], "merkle_root"),
                timestamp=_strict_int(data["timestamp"], "timestamp"),
                difficulty_bits=_strict_int(data["difficulty_bits"], "difficulty_bits"),
                nonce=_strict_int(data["nonce"], "nonce"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodecError("Malformed block header object") from exc


@dataclass(frozen=True, slots=True)
class Block:
    header: BlockHeader
    transactions: tuple[Transaction, ...] = field(default_factory=tuple)

    @property
    def hash(self) -> str:
        return self.header.hash

    def canonical_bytes(self) -> bytes:
        from .codec import encode_block

        return encode_block(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "header": self.header.to_dict(),
            "transactions": [tx.to_dict() for tx in self.transactions],
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        try:
            header = BlockHeader.from_dict(data["header"])
            raw_transactions = data["transactions"]
            if not isinstance(raw_transactions, list):
                raise CodecError("transactions must be a list")
            block = cls(
                header=header,
                transactions=tuple(Transaction.from_dict(tx) for tx in raw_transactions),
            )
            stored_hash = data.get("hash")
            if stored_hash is not None and stored_hash != block.hash:
                raise CodecError("Stored block hash does not match its canonical header")
            return block
        except (KeyError, TypeError) as exc:
            raise CodecError("Malformed block object") from exc


@dataclass(frozen=True, slots=True)
class MerkleProofStep:
    sibling: bytes
    sibling_on_left: bool

    def to_dict(self) -> dict[str, Any]:
        return {"sibling": self.sibling.hex(), "sibling_on_left": self.sibling_on_left}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MerkleProofStep":
        try:
            if not isinstance(data["sibling_on_left"], bool):
                raise CodecError("sibling_on_left must be a boolean")
            return cls(
                sibling=_hex_bytes(data["sibling"], "sibling"),
                sibling_on_left=data["sibling_on_left"],
            )
        except (KeyError, TypeError) as exc:
            raise CodecError("Malformed Merkle proof step") from exc


@dataclass(frozen=True, slots=True)
class MerkleProof:
    root: bytes
    tx_id: bytes
    transaction_index: int
    transaction_count: int
    steps: tuple[MerkleProofStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.hex(),
            "tx_id": self.tx_id.hex(),
            "transaction_index": self.transaction_index,
            "transaction_count": self.transaction_count,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MerkleProof":
        try:
            raw_steps = data["steps"]
            if not isinstance(raw_steps, list):
                raise CodecError("steps must be a list")
            return cls(
                root=_hex_bytes(data["root"], "root"),
                tx_id=_hex_bytes(data["tx_id"], "tx_id"),
                transaction_index=_strict_int(data["transaction_index"], "transaction_index"),
                transaction_count=_strict_int(data["transaction_count"], "transaction_count"),
                steps=tuple(MerkleProofStep.from_dict(step) for step in raw_steps),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodecError("Malformed Merkle proof object") from exc

