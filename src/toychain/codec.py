"""Codec-related functionality."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .constants import (
    BLOCK_HEADER_MAGIC,
    BLOCK_MAGIC,
    FORMAT_VERSION,
    TX_SIGNED_MAGIC,
    TX_UNSIGNED_MAGIC,
)
from .errors import CodecError
from .models import Block, BlockHeader, Transaction

MAX_FIELD_BYTES = 1_000_000
MAX_TRANSACTIONS = 100_000


def _u8(value: int, name: str) -> bytes:
    if not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise CodecError(f"{name} must fit in an unsigned byte")
    return struct.pack(">B", value)


def _u32(value: int, name: str) -> bytes:
    if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise CodecError(f"{name} must fit in an unsigned 32-bit integer")
    return struct.pack(">I", value)


def _u64(value: int, name: str) -> bytes:
    if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise CodecError(f"{name} must fit in an unsigned 64-bit integer")
    return struct.pack(">Q", value)


def _bytes(value: bytes, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise CodecError(f"{name} must be bytes")
    if len(value) > MAX_FIELD_BYTES:
        raise CodecError(f"{name} is too large")
    return _u32(len(value), f"{name} length") + value


def _text(value: str, name: str) -> bytes:
    if not isinstance(value, str):
        raise CodecError(f"{name} must be text")
    return _bytes(value.encode("utf-8"), name)


def _transaction_fields(tx: Transaction) -> bytes:
    return b"".join(
        (
            _text(tx.sender, "sender"),
            _text(tx.recipient, "recipient"),
            _u64(tx.amount, "amount"),
            _u64(tx.nonce, "nonce"),
            _bytes(tx.public_key, "public_key"),
        )
    )


def encode_unsigned_transaction(tx: Transaction) -> bytes:
    return TX_UNSIGNED_MAGIC + _u8(tx.version, "version") + _transaction_fields(tx)


def encode_transaction(tx: Transaction) -> bytes:
    return (
        TX_SIGNED_MAGIC
        + _u8(tx.version, "version")
        + _transaction_fields(tx)
        + _bytes(tx.signature, "signature")
    )


def encode_block_header(header: BlockHeader) -> bytes:
    if len(header.previous_hash) != 32:
        raise CodecError("previous_hash must be exactly 32 bytes")
    if len(header.merkle_root) != 32:
        raise CodecError("merkle_root must be exactly 32 bytes")
    return b"".join(
        (
            BLOCK_HEADER_MAGIC,
            _u8(header.version, "version"),
            header.previous_hash,
            header.merkle_root,
            _u64(header.timestamp, "timestamp"),
            _u8(header.difficulty_bits, "difficulty_bits"),
            _u64(header.nonce, "nonce"),
        )
    )


def encode_block(block: Block) -> bytes:
    if len(block.transactions) > MAX_TRANSACTIONS:
        raise CodecError("Transaction count is too large")
    encoded_transactions = [encode_transaction(tx) for tx in block.transactions]
    return b"".join(
        (
            BLOCK_MAGIC,
            _u8(FORMAT_VERSION, "block record version"),
            _bytes(encode_block_header(block.header), "header"),
            _u32(len(encoded_transactions), "transaction count"),
            *(_bytes(tx, "transaction") for tx in encoded_transactions),
        )
    )


@dataclass(slots=True)
class _Reader:
    data: bytes
    offset: int = 0

    def read(self, size: int, name: str) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise CodecError(f"Truncated payload while reading {name}")
        value = self.data[self.offset : self.offset + size]
        self.offset += size
        return value

    def u8(self, name: str) -> int:
        return struct.unpack(">B", self.read(1, name))[0]

    def u32(self, name: str) -> int:
        return struct.unpack(">I", self.read(4, name))[0]

    def u64(self, name: str) -> int:
        return struct.unpack(">Q", self.read(8, name))[0]

    def bytes(self, name: str) -> bytes:
        size = self.u32(f"{name} length")
        if size > MAX_FIELD_BYTES:
            raise CodecError(f"{name} is too large")
        return self.read(size, name)

    def text(self, name: str) -> str:
        try:
            return self.bytes(name).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodecError(f"{name} is not valid UTF-8") from exc

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise CodecError(f"Payload has {len(self.data) - self.offset} extra byte(s)")


def _expect(reader: _Reader, expected: bytes, name: str) -> None:
    actual = reader.read(len(expected), name)
    if actual != expected:
        raise CodecError(f"Invalid {name}")


def _version(reader: _Reader, name: str) -> int:
    version = reader.u8(name)
    if version != FORMAT_VERSION:
        raise CodecError(f"Unsupported {name}: {version}")
    return version


def _read_transaction_fields(reader: _Reader) -> dict[str, object]:
    return {
        "sender": reader.text("sender"),
        "recipient": reader.text("recipient"),
        "amount": reader.u64("amount"),
        "nonce": reader.u64("nonce"),
        "public_key": reader.bytes("public_key"),
    }


def parse_unsigned_transaction(data: bytes) -> Transaction:
    reader = _Reader(data)
    _expect(reader, TX_UNSIGNED_MAGIC, "unsigned transaction magic")
    version = _version(reader, "transaction version")
    fields = _read_transaction_fields(reader)
    reader.finish()
    return Transaction(version=version, signature=b"", **fields)  # type: ignore[arg-type]


def parse_transaction(data: bytes) -> Transaction:
    reader = _Reader(data)
    _expect(reader, TX_SIGNED_MAGIC, "signed transaction magic")
    version = _version(reader, "transaction version")
    fields = _read_transaction_fields(reader)
    signature = reader.bytes("signature")
    reader.finish()
    return Transaction(version=version, signature=signature, **fields)  # type: ignore[arg-type]


def parse_block_header(data: bytes) -> BlockHeader:
    reader = _Reader(data)
    _expect(reader, BLOCK_HEADER_MAGIC, "block header magic")
    version = _version(reader, "block header version")
    header = BlockHeader(
        version=version,
        previous_hash=reader.read(32, "previous_hash"),
        merkle_root=reader.read(32, "merkle_root"),
        timestamp=reader.u64("timestamp"),
        difficulty_bits=reader.u8("difficulty_bits"),
        nonce=reader.u64("nonce"),
    )
    reader.finish()
    return header


def parse_block(data: bytes) -> Block:
    reader = _Reader(data)
    _expect(reader, BLOCK_MAGIC, "block magic")
    _version(reader, "block record version")
    header = parse_block_header(reader.bytes("header"))
    count = reader.u32("transaction count")
    if count > MAX_TRANSACTIONS:
        raise CodecError("Transaction count is too large")
    transactions = tuple(parse_transaction(reader.bytes("transaction")) for _ in range(count))
    reader.finish()
    return Block(header=header, transactions=transactions)
