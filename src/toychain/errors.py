"""Errors for the toychain package."""

class ToychainError(Exception):
    """Base class for clean user-facing failures."""


class CodecError(ToychainError):
    pass


class CryptoError(ToychainError):
    pass


class ValidationError(ToychainError):
    pass


class ConsensusError(ToychainError):
    pass


class MempoolError(ToychainError):
    pass


class NodeRuntimeError(ToychainError):
    pass


class PersistenceError(ToychainError):
    pass

