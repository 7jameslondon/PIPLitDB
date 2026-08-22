"""Load and validate the versioned, content-only record JSON schema."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


RECORD_SCHEMA_VERSION = "1.1"
SUPPORTED_RECORD_SCHEMA_VERSIONS = ("1.0", RECORD_SCHEMA_VERSION)
_SCHEMA_ROOT = Path(__file__).with_name("schemas")
_SCHEMA_FILES = {
    version: _SCHEMA_ROOT / f"record-{version}.schema.json"
    for version in SUPPORTED_RECORD_SCHEMA_VERSIONS
}
_SCHEMA_IDS = {
    version: f"https://pip-litdb.org/schema/record-{version}.schema.json"
    for version in SUPPORTED_RECORD_SCHEMA_VERSIONS
}
RECORD_SCHEMA_ID = _SCHEMA_IDS[RECORD_SCHEMA_VERSION]
RECORD_SCHEMA_PATH = _SCHEMA_FILES[RECORD_SCHEMA_VERSION]
_MAX_SCHEMA_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RecordSchemaViolation:
    """One deterministic, location-aware record-schema violation."""

    instance_path: str
    schema_path: str
    validator: str
    message: str


class RecordSchemaError(ValueError):
    """Raised when the local schema is unusable or a record is invalid."""

    def __init__(
        self,
        message: str,
        *,
        violations: tuple[RecordSchemaViolation, ...] = (),
    ) -> None:
        super().__init__(message)
        self.violations = violations


def _json_pointer(parts: Iterator[Any]) -> str:
    escaped = [
        str(part).replace("~", "~0").replace("/", "~1") for part in parts
    ]
    return "$" + "".join(f"/{part}" for part in escaped)


def _iter_references(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                yield key, child
            yield from _iter_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_references(child)


def _supported_version(version: str) -> str:
    if version not in _SCHEMA_FILES:
        supported = ", ".join(SUPPORTED_RECORD_SCHEMA_VERSIONS)
        raise RecordSchemaError(
            f"Unsupported record schema version {version!r}; supported versions: {supported}."
        )
    return version


@lru_cache(maxsize=None)
def _load_record_schema(version: str) -> dict[str, Any]:
    version = _supported_version(version)
    schema_path = _SCHEMA_FILES[version]
    schema_id = _SCHEMA_IDS[version]
    try:
        size = schema_path.stat().st_size
        if size > _MAX_SCHEMA_BYTES:
            raise RecordSchemaError(
                f"Local record schema {version} exceeds {_MAX_SCHEMA_BYTES} bytes."
            )
        raw = schema_path.read_text(encoding="utf-8", errors="strict")
    except RecordSchemaError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordSchemaError(
            f"Local record schema is unavailable or unreadable: {exc}."
        ) from exc

    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecordSchemaError(f"Local record schema is not valid JSON: {exc}.") from exc
    if not isinstance(schema, dict):
        raise RecordSchemaError("Local record schema root must be a JSON object.")
    if schema.get("$id") != schema_id:
        raise RecordSchemaError(
            f"Local record schema {version} must declare $id {schema_id!r}."
        )
    properties = schema.get("properties")
    version_schema = (
        properties.get("schema_version") if isinstance(properties, dict) else None
    )
    declared_version = version_schema.get("const") if isinstance(version_schema, dict) else None
    if declared_version != version:
        raise RecordSchemaError(
            f"Local record schema {version} must require schema_version {version!r}."
        )
    for keyword, reference in _iter_references(schema):
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise RecordSchemaError(
                f"Local record schema {keyword} values must be internal fragments; "
                f"found {reference!r}."
            )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise RecordSchemaError(f"Local record schema is invalid: {exc.message}.") from exc
    return schema


def load_record_schema(version: str = RECORD_SCHEMA_VERSION) -> dict[str, Any]:
    """Return an isolated copy of one checked, locally bundled schema."""

    return deepcopy(_load_record_schema(version))


@lru_cache(maxsize=None)
def _record_validator(version: str) -> Draft202012Validator:
    return Draft202012Validator(_load_record_schema(version))


def record_schema_version(payload: Any) -> str:
    """Select a bundled schema without allowing payload-controlled file access."""

    if isinstance(payload, Mapping):
        raw_version = payload.get("schema_version")
        if isinstance(raw_version, str):
            return _supported_version(raw_version)
    # Let the latest schema produce ordinary type/required violations for a
    # missing or non-string discriminator.
    return RECORD_SCHEMA_VERSION


def validate_record_schema(payload: Any, *, source_path: str | None = None) -> None:
    """Raise :class:`RecordSchemaError` for structural schema violations."""

    try:
        version = record_schema_version(payload)
    except RecordSchemaError as exc:
        subject = source_path or "Record JSON"
        violation = RecordSchemaViolation(
            instance_path="$/schema_version",
            schema_path="$/properties/schema_version",
            validator="enum",
            message=str(exc),
        )
        raise RecordSchemaError(
            f"{subject} does not declare a supported record schema version.",
            violations=(violation,),
        ) from exc
    errors = list(_record_validator(version).iter_errors(payload))
    violations = {
        (
            _json_pointer(iter(error.absolute_path)),
            _json_pointer(iter(error.absolute_schema_path)),
            str(error.validator),
            error.message,
        )
        for error in errors
    }
    ordered = tuple(
        RecordSchemaViolation(
            instance_path=instance_path,
            schema_path=schema_path,
            validator=validator,
            message=message,
        )
        for instance_path, schema_path, validator, message in sorted(violations)
    )
    if not ordered:
        return
    subject = source_path or "Record JSON"
    count = len(ordered)
    suffix = "violation" if count == 1 else "violations"
    raise RecordSchemaError(
        f"{subject} does not conform to record schema {version} "
        f"({count} {suffix}).",
        violations=ordered,
    )


__all__ = [
    "RECORD_SCHEMA_ID",
    "RECORD_SCHEMA_PATH",
    "RECORD_SCHEMA_VERSION",
    "SUPPORTED_RECORD_SCHEMA_VERSIONS",
    "RecordSchemaError",
    "RecordSchemaViolation",
    "load_record_schema",
    "record_schema_version",
    "validate_record_schema",
]
