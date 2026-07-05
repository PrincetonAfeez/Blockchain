"""Strict node config.json loading and persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import PERSISTENCE_SCHEMA_VERSION
from .errors import CodecError, NodeRuntimeError, PersistenceError
from .json_validation import (
    persistence_schema_version,
    reject_unknown_keys,
    strict_int,
    strict_str,
    validate_json_schema,
)
from .persistence import read_json, write_json

_CONFIG_KEYS = frozenset({"schema_version", "data_dir", "port"})


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


@dataclass(frozen=True, slots=True)
class NodeConfig:
    schema_version: int
    data_dir: str
    port: int

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
        port = strict_int(normalized["port"], "port")
    except CodecError as exc:
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
    write_json(path, config.to_dict())
