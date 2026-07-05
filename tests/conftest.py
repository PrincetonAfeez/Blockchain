"""Fixtures for the toychain package."""

from __future__ import annotations

import pytest

from toychain.crypto import KeyPair, generate_keypair


@pytest.fixture
def alice() -> KeyPair:
    return generate_keypair()


@pytest.fixture
def bob() -> KeyPair:
    return generate_keypair()

