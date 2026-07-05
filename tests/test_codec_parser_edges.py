"""Tests for byte-parser decoding edge cases."""

from __future__ import annotations

import struct

import pytest

from toychain.codec import (
    MAX_FIELD_BYTES,
    parse_transaction,
    parse_unsigned_transaction,
)
from toychain.errors import CodecError


def _minimal_unsigned_payload(*, sender: bytes, recipient: bytes = b"tc1" + b"a" * 40) -> bytes:
    return (
        b"TXU\x01"
        + struct.pack(">I", len(sender))
        + sender
        + struct.pack(">I", len(recipient))
        + recipient
        + struct.pack(">Q", 1)
        + struct.pack(">Q", 0)
        + struct.pack(">I", 32)
        + b"\x01" * 32
    )


def _minimal_signed_payload(*, sender: bytes, recipient: bytes = b"tc1" + b"a" * 40) -> bytes:
    return (
        _minimal_unsigned_payload(sender=sender, recipient=recipient)
        .replace(b"TXU", b"TXS", 1)
        + struct.pack(">I", 64)
        + b"\x02" * 64
    )


def test_parse_unsigned_transaction_rejects_invalid_utf8_in_sender():
    payload = _minimal_unsigned_payload(sender=b"\xff\xfe\xfd")
    with pytest.raises(CodecError, match="not valid UTF-8"):
        parse_unsigned_transaction(payload)


def test_parse_unsigned_transaction_rejects_invalid_utf8_in_recipient():
    payload = _minimal_unsigned_payload(sender=b"alice", recipient=b"\xc0\x80")
    with pytest.raises(CodecError, match="not valid UTF-8"):
        parse_unsigned_transaction(payload)


def test_parse_transaction_rejects_declared_field_length_over_max():
    payload = b"TXS\x01" + struct.pack(">I", MAX_FIELD_BYTES + 1)
    with pytest.raises(CodecError, match="too large"):
        parse_transaction(payload)


def test_parse_unsigned_transaction_rejects_truncated_length_prefix():
    payload = b"TXU\x01" + struct.pack(">I", 32) + b"short"
    with pytest.raises(CodecError, match="Truncated"):
        parse_unsigned_transaction(payload)


def test_parse_unsigned_transaction_accepts_exactly_max_field_bytes():
    sender = b"a" * MAX_FIELD_BYTES
    payload = _minimal_unsigned_payload(sender=sender)
    parsed = parse_unsigned_transaction(payload)
    assert parsed.sender == sender.decode("utf-8")


def test_parse_signed_transaction_rejects_invalid_utf8_in_recipient():
    payload = _minimal_signed_payload(sender=b"alice", recipient=b"\xff\xfe")
    with pytest.raises(CodecError, match="not valid UTF-8"):
        parse_transaction(payload)
