"""Models for the toychain package."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .constants import COINBASE_SENDER, FORMAT_VERSION
from .errors import CodecError
from .json_validation import (
    format_version,
    hex_bytes,
    hex_bytes_allowed,
    reject_unknown_keys,
    strict_bool,
    strict_int,
    strict_str,
    validate_json_schema,
)

_TRANSACTION_KEYS = frozenset(
    {"version", "sender", "recipient", "amount", "nonce", "public_key", "signature"}
)
_BLOCK_HEADER_KEYS = frozenset(
    {
        "version",
        "previous_hash",
        "merkle_root",
        "timestamp",
        "difficulty_bits",
        "nonce",
    }
)
_BLOCK_KEYS = frozenset({"header", "transactions", "hash"})
_MERKLE_STEP_KEYS = frozenset({"sibling", "sibling_on_left"})
_MERKLE_PROOF_KEYS = frozenset(
    {"root", "tx_id", "transaction_index", "transaction_count", "steps"}
)


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
        if not isinstance(data, dict):
            raise CodecError("Malformed transaction object")
        validate_json_schema(data, "transaction")
        reject_unknown_keys(data, _TRANSACTION_KEYS, "transaction")
        try:
            return cls(
                version=format_version(data["version"]),
                sender=strict_str(data["sender"], "sender"),
                recipient=strict_str(data["recipient"], "recipient"),
                amount=strict_int(data["amount"], "amount"),
                nonce=strict_int(data["nonce"], "nonce"),
                public_key=hex_bytes_allowed(
                    data["public_key"],
                    "public_key",
                    allowed_lengths=(0, 8, 32),
                ),
                signature=hex_bytes_allowed(
                    data["signature"],
                    "signature",
                    allowed_lengths=(0, 64),
                ),
            )
        except KeyError as exc:
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
        if not isinstance(data, dict):
            raise CodecError("Malformed block header object")
        validate_json_schema(data, "block-header")
        reject_unknown_keys(data, _BLOCK_HEADER_KEYS, "block header")
        try:
            return cls(
                version=format_version(data["version"]),
                previous_hash=hex_bytes(data["previous_hash"], "previous_hash", exact_bytes=32),
                merkle_root=hex_bytes(data["merkle_root"], "merkle_root", exact_bytes=32),
                timestamp=strict_int(data["timestamp"], "timestamp"),
                difficulty_bits=strict_int(data["difficulty_bits"], "difficulty_bits"),
                nonce=strict_int(data["nonce"], "nonce"),
            )
        except KeyError as exc:
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
        if not isinstance(data, dict):
            raise CodecError("Malformed block object")
        validate_json_schema(data, "block")
        reject_unknown_keys(data, _BLOCK_KEYS, "block")
        try:
            raw_transactions = data["transactions"]
            if not isinstance(raw_transactions, list):
                raise CodecError("transactions must be a list")
            block = cls(
                header=BlockHeader.from_dict(data["header"]),
                transactions=tuple(Transaction.from_dict(tx) for tx in raw_transactions),
            )
            stored_hash = data.get("hash")
            if stored_hash is not None:
                if not isinstance(stored_hash, str):
                    raise CodecError("hash must be a string")
                if stored_hash != block.hash:
                    raise CodecError("Stored block hash does not match its canonical header")
            return block
        except KeyError as exc:
            raise CodecError("Malformed block object") from exc


@dataclass(frozen=True, slots=True)
class MerkleProofStep:
    sibling: bytes
    sibling_on_left: bool

    def to_dict(self) -> dict[str, Any]:
        return {"sibling": self.sibling.hex(), "sibling_on_left": self.sibling_on_left}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MerkleProofStep":
        if not isinstance(data, dict):
            raise CodecError("Malformed Merkle proof step")
        reject_unknown_keys(data, _MERKLE_STEP_KEYS, "Merkle proof step")
        try:
            return cls(
                sibling=hex_bytes(data["sibling"], "sibling", exact_bytes=32),
                sibling_on_left=strict_bool(data["sibling_on_left"], "sibling_on_left"),
            )
        except KeyError as exc:
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
        if not isinstance(data, dict):
            raise CodecError("Malformed Merkle proof object")
        validate_json_schema(data, "merkle-proof")
        reject_unknown_keys(data, _MERKLE_PROOF_KEYS, "Merkle proof")
        try:
            raw_steps = data["steps"]
            if not isinstance(raw_steps, list):
                raise CodecError("steps must be a list")
            return cls(
                root=hex_bytes(data["root"], "root", exact_bytes=32),
                tx_id=hex_bytes(data["tx_id"], "tx_id", exact_bytes=32),
                transaction_index=strict_int(data["transaction_index"], "transaction_index"),
                transaction_count=strict_int(data["transaction_count"], "transaction_count"),
                steps=tuple(MerkleProofStep.from_dict(step) for step in raw_steps),
            )
        except KeyError as exc:
            raise CodecError("Malformed Merkle proof object") from exc
