"""Strict node config.json loading and persistence."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import PERSISTENCE_SCHEMA_VERSION
from .errors import CodecError, NodeRuntimeError, PersistenceError, ValidationError
from .json_validation import (
    persistence_schema_version,
    reject_unknown_keys,
    strict_int,
    strict_str,
    validate_json_schema,
)
from .persistence import read_json, write_json

_CONFIG_KEYS = frozenset({"schema_version", "data_dir", "port"})
MIN_PORT = 0
MAX_PORT = 65535


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def validate_port_value(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValidationError("Port must be an integer")
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValidationError(f"Port must be between {MIN_PORT} and {MAX_PORT}")
    return port


def valid_port_arg(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if isinstance(port, bool):
        raise argparse.ArgumentTypeError("port must be an integer")
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"port must be between {MIN_PORT} and {MAX_PORT}")
    return port


@dataclass(frozen=True, slots=True)
class NodeConfig:
    schema_version: int
    data_dir: str
    port: int

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise ValidationError("schema_version must be an integer")
        if self.schema_version != PERSISTENCE_SCHEMA_VERSION:
            raise ValidationError(f"Unsupported schema_version: {self.schema_version}")
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValidationError("data_dir must be a non-empty string")
        validate_port_value(self.port)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "data_dir": self.data_dir,
            "port": self.port,
        }


def load_node_config(
    path: Path,
    *,
    expected_data_dir: Path | None = None,
) -> NodeConfig:
    data = read_json(path)
    if not isinstance(data, dict):
        raise PersistenceError("Node config must contain a JSON object")
    normalized = dict(data)
    if "schema_version" not in normalized:
        normalized["schema_version"] = PERSISTENCE_SCHEMA_VERSION
    try:
        validate_json_schema(normalized, "node-config")
        reject_unknown_keys(normalized, _CONFIG_KEYS, "node config")
        persistence_schema_version(normalized["schema_version"])
        data_dir = strict_str(normalized["data_dir"], "data_dir")
        if not data_dir.strip():
            raise CodecError("data_dir must not be empty")
        port = validate_port_value(strict_int(normalized["port"], "port"))
    except (ValidationError, CodecError) as exc:
        raise PersistenceError(f"Malformed node config: {exc}") from exc
    if expected_data_dir is not None:
        if _normalized_path(data_dir) != _normalized_path(expected_data_dir):
            raise NodeRuntimeError("Node config data_dir does not match this data directory")
    return NodeConfig(
        schema_version=normalized["schema_version"],
        data_dir=data_dir,
        port=port,
    )


def save_node_config(path: Path, config: NodeConfig) -> None:
    payload = config.to_dict()
    validate_json_schema(payload, "node-config")
    reject_unknown_keys(payload, _CONFIG_KEYS, "node config")
    write_json(path, payload)
