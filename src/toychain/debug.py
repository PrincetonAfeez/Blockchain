"""Debug-related functionality."""

from __future__ import annotations

import dis
import io
from collections.abc import Callable

from .block import mine_block
from .chain import validate_block
from .consensus import select_best_chain
from .errors import ValidationError
from .merkle import build_merkle_root
from .transactions import validate_transaction_against_state

DISASSEMBLY_TARGETS: dict[str, Callable[..., object]] = {
    "validate-transaction": validate_transaction_against_state,
    "validate-block": validate_block,
    "mine-block": mine_block,
    "select-best-chain": select_best_chain,
    "build-merkle-root": build_merkle_root,
}


def disassemble_target(name: str) -> str:
    function = DISASSEMBLY_TARGETS.get(name)
    if function is None:
        choices = ", ".join(sorted(DISASSEMBLY_TARGETS))
        raise ValidationError(f"Unsupported disassembly target. Choose one of: {choices}")
    output = io.StringIO()
    output.write(f"Disassembly of {function.__module__}.{function.__name__}\n")
    dis.dis(function, file=output)
    return output.getvalue()
