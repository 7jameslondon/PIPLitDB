"""Load and apply the local, content-preserving table JSON Schema.

The schema is part of the extraction program.  References are deliberately
restricted to fragments within that file so validation never resolves a URL,
reads a source record, or depends on network access.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


TABLE_SCHEMA_VERSION = "1.0"
TABLE_ASSET_PATH_BASE = "extraction_root"
TABLE_SCHEMA_ID = "https://pip-litdb.org/schema/table-1.0.schema.json"
TABLE_SCHEMA_PATH = Path(__file__).with_name("schemas") / "table-1.0.schema.json"
_MAX_SCHEMA_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TableSchemaViolation:
    """One deterministic, location-aware table-schema violation."""

    instance_path: str
    schema_path: str
    validator: str
    message: str


class TableSchemaError(ValueError):
    """Raised when the local schema cannot be used or a table is invalid."""

    def __init__(
        self,
        message: str,
        *,
        violations: tuple[TableSchemaViolation, ...] = (),
    ) -> None:
        super().__init__(message)
        self.violations = violations


def _json_pointer(parts: Iterator[Any]) -> str:
    escaped = [
        str(part).replace("~", "~0").replace("/", "~1")
        for part in parts
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


@lru_cache(maxsize=1)
def _load_table_schema() -> dict[str, Any]:
    """Load and check the fixed schema once for the process-local validator."""

    try:
        size = TABLE_SCHEMA_PATH.stat().st_size
        if size > _MAX_SCHEMA_BYTES:
            raise TableSchemaError(
                f"Local table schema exceeds {_MAX_SCHEMA_BYTES} bytes."
            )
        raw = TABLE_SCHEMA_PATH.read_text(encoding="utf-8", errors="strict")
    except TableSchemaError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise TableSchemaError(
            f"Local table schema is unavailable or unreadable: {exc}."
        ) from exc

    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TableSchemaError(
            f"Local table schema is not valid JSON: {exc}."
        ) from exc
    if not isinstance(schema, dict):
        raise TableSchemaError("Local table schema root must be a JSON object.")
    if schema.get("$id") != TABLE_SCHEMA_ID:
        raise TableSchemaError(
            f"Local table schema must declare $id {TABLE_SCHEMA_ID!r}."
        )

    for keyword, reference in _iter_references(schema):
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise TableSchemaError(
                f"Local table schema {keyword} values must be internal fragments; "
                f"found {reference!r}."
            )

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise TableSchemaError(
            f"Local table schema is invalid: {exc.message}."
        ) from exc
    return schema


def load_table_schema() -> dict[str, Any]:
    """Return an isolated copy of the checked local table schema."""

    return deepcopy(_load_table_schema())


@lru_cache(maxsize=1)
def _table_validator() -> Draft202012Validator:
    return Draft202012Validator(_load_table_schema())


def validate_table_payload(
    payload: Any,
    *,
    source_path: str | None = None,
) -> None:
    """Raise :class:`TableSchemaError` if ``payload`` violates schema 1.0."""

    errors = list(_table_validator().iter_errors(payload))
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
        TableSchemaViolation(
            instance_path=instance_path,
            schema_path=schema_path,
            validator=validator,
            message=message,
        )
        for instance_path, schema_path, validator, message in sorted(violations)
    )
    if not ordered:
        return

    subject = source_path or "Table JSON"
    count = len(ordered)
    suffix = "violation" if count == 1 else "violations"
    raise TableSchemaError(
        f"{subject} does not conform to table schema {TABLE_SCHEMA_VERSION} "
        f"({count} {suffix}).",
        violations=ordered,
    )
