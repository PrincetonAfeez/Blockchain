"""Strict JSON validation helpers and JSON Schema checks."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .constants import FORMAT_VERSION, PERSISTENCE_SCHEMA_VERSION
from .errors import CodecError


def _schema_directory() -> Path:
    return Path(__file__).resolve().parent / "schema"


def reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise CodecError(f"Unknown {name} properties: {', '.join(sorted(extra))}")


def strict_str(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise CodecError(f"{field_name} must be a string")
    return value


def strict_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"{field_name} must be an integer")
    return value


def strict_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CodecError(f"{field_name} must be a boolean")
    return value


def format_version(value: Any, field_name: str = "version") -> int:
    version = strict_int(value, field_name)
    if version != FORMAT_VERSION:
        raise CodecError(f"Unsupported {field_name}: {version}")
    return version


def persistence_schema_version(value: Any, field_name: str = "schema_version") -> int:
    version = strict_int(value, field_name)
    if version != PERSISTENCE_SCHEMA_VERSION:
        raise CodecError(f"Unsupported {field_name}: {version}")
    return version


def hex_bytes(value: Any, field_name: str, *, exact_bytes: int) -> bytes:
    if not isinstance(value, str):
        raise CodecError(f"{field_name} must be a hexadecimal string")
    if value != value.lower():
        raise CodecError(f"{field_name} must use lowercase hexadecimal")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise CodecError(f"{field_name} is not valid hexadecimal") from exc
    if len(decoded) != exact_bytes:
        raise CodecError(f"{field_name} must be exactly {exact_bytes} bytes")
    return decoded


def hex_bytes_allowed(
    value: Any,
    field_name: str,
    *,
    allowed_lengths: tuple[int, ...],
) -> bytes:
    if not isinstance(value, str):
        raise CodecError(f"{field_name} must be a hexadecimal string")
    if value == "":
        if 0 in allowed_lengths:
            return b""
        raise CodecError(f"{field_name} must not be empty")
    if value != value.lower():
        raise CodecError(f"{field_name} must use lowercase hexadecimal")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise CodecError(f"{field_name} is not valid hexadecimal") from exc
    if len(decoded) not in allowed_lengths:
        lengths = ", ".join(str(length) for length in sorted(allowed_lengths))
        raise CodecError(f"{field_name} must decode to one of ({lengths}) bytes")
    return decoded


@lru_cache
def _schema_registry() -> Registry:
    resources: list[tuple[str, Resource[Any]]] = []
    for path in sorted(_schema_directory().glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((path.name, Resource.from_contents(contents)))
        schema_id = contents.get("$id")
        if isinstance(schema_id, str):
            resources.append((schema_id, Resource.from_contents(contents)))
    return Registry().with_resources(resources)


@lru_cache
def _load_schema(name: str) -> dict[str, Any]:
    path = _schema_directory() / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_json_schema(instance: dict[str, Any], schema_name: str) -> None:
    schema = _load_schema(schema_name)
    validator = Draft202012Validator(schema, registry=_schema_registry())
    try:
        validator.validate(instance)
    except jsonschema.ValidationError as exc:
        raise CodecError(f"JSON schema validation failed: {exc.message}") from exc
