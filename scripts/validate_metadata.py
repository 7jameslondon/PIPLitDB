#!/usr/bin/env python3
"""Validate the PIP LitDB metadata database and summarize record changes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


RECORD_NAME_RE = re.compile(r"^(?P<id>[0-9]{5})\.yaml$")
VOCABULARY_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
IDENTITY_FIELDS = frozenset({"title", "authors", "doi"})
ALLOWED_RECORD_DIRECTORY_FILES = frozenset({".gitkeep"})
MAX_YAML_NESTING_DEPTH = 100
MAX_SCHEMA_NESTING_DEPTH = 100
YAML_VALUE_ERRORS = (AttributeError, KeyError, TypeError, ValueError, OverflowError)
VOCABULARY_SPECS = {
    "document-types.yaml": frozenset({"label", "description"}),
    "publication-stages.yaml": frozenset({"label", "description"}),
    "record-statuses.yaml": frozenset({"label", "description"}),
    "relationship-types.yaml": frozenset({"label", "description", "inverse"}),
}
PUBLIC_URL_RE = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)
PATH_ENVIRONMENT_VARIABLE_NAME_RE = (
    r"(?:ALLUSERSPROFILE|APPDATA|CD|COMMONPROGRAMFILES(?:\(X86\))?|HOME|"
    r"HOMEDRIVE|HOMEPATH|LOCALAPPDATA|OLDPWD|ONEDRIVE(?:COMMERCIAL|CONSUMER)?|"
    r"PATH|PROGRAMDATA|"
    r"PROGRAMFILES(?:\(X86\))?|PROGRAMW6432|SYSTEMDRIVE|SYSTEMROOT|TEMP|TMP|"
    r"TMPDIR|PSHOME|PSSCRIPTROOT|PUBLIC|PWD|USERPROFILE|WINDIR|"
    r"XDG_(?:CACHE|CONFIG|DATA|STATE)_HOME|XDG_RUNTIME_DIR)"
)
EXPLICIT_ENVIRONMENT_REFERENCE_RE = re.compile(
    r"(?:%[A-Za-z_][A-Za-z0-9_()]*(?::[^%\r\n]*)?%|"
    r"\$env:[A-Za-z_][A-Za-z0-9_()]*|"
    r"\$\{env:[^}=\r\n]+\})",
    re.IGNORECASE,
)
BARE_PATH_ENVIRONMENT_REFERENCE_RE = re.compile(
    r"(?<!\$)\$"
    + PATH_ENVIRONMENT_VARIABLE_NAME_RE
    + r"(?![A-Za-z0-9_$%])",
    re.IGNORECASE,
)
BRACED_PATH_ENVIRONMENT_REFERENCE_RE = re.compile(
    r"\$\{"
    + PATH_ENVIRONMENT_VARIABLE_NAME_RE
    + r"\}",
    re.IGNORECASE,
)
PRIVATE_REFERENCE_PATTERNS = (
    re.compile(r"papers\s*\(private\)", re.IGNORECASE),
    re.compile(r"\bfile:(?:[\\/]+|[A-Za-z]:[\\/])", re.IGNORECASE),
    EXPLICIT_ENVIRONMENT_REFERENCE_RE,
    BARE_PATH_ENVIRONMENT_REFERENCE_RE,
    BRACED_PATH_ENVIRONMENT_REFERENCE_RE,
    re.compile(r"(?<![A-Za-z0-9_%])%[A-Za-z_][A-Za-z0-9_()]*%[\\/][^\s]*"),
    re.compile(
        r"(?<![A-Za-z0-9_$])\$(?:env:[A-Za-z_][A-Za-z0-9_]*|\{env:[^}=\r\n]+\})[\\/][^\s]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_$])\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})[\\/]"
        r"(?![^\s]*\$)[^\s]*"
    ),
    re.compile(r"\b[A-Za-z]:(?![\\/]{2})(?:[\\/]|(?=[^\\/\s]))[^\s]*"),
    re.compile(r"(?<![\\/])\\\\[^\\/\s]+[\\/][^\\/\s]+"),
    re.compile(r"(?<![:/])//[^/\s]+/[^/\s]+"),
    re.compile(r"(?<![A-Za-z0-9_])~(?:[A-Za-z0-9_.+-]+)?[\\/][^\s]+"),
    re.compile(r"(?<![A-Za-z0-9_])\.\.?[\\/][^\\/\s]+(?:[\\/][^\\/\s]+)*"),
    re.compile(r"(?<![A-Za-z0-9_:/%])/(?!/)[^/\s]+(?:/[^/\s]+)*"),
)


class StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys and merge keys."""


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


class InvalidJsonConstantError(ValueError):
    """Raised for non-standard JSON constants such as NaN and Infinity."""


def _construct_unique_mapping(
    loader: StrictSafeLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, but found {node.id}",
            node.start_mark,
        )

    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None

    def sort_key(self) -> tuple[str, int, int, str, str]:
        severity_order = 0 if self.severity == "error" else 1
        return (
            self.path or "",
            self.line or 0,
            severity_order,
            self.code,
            self.message,
        )


@dataclass(frozen=True)
class RecordChange:
    kind: str
    old_path: str | None = None
    new_path: str | None = None
    old_id: str | None = None
    new_id: str | None = None
    changed_fields: tuple[str, ...] = ()


@dataclass
class ValidationReport:
    root: Path
    record_count: int = 0
    findings: list[Finding] = field(default_factory=list)
    changes: list[RecordChange] = field(default_factory=list)
    compared_base: str | None = None
    compared_head: str | None = None

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        path: str | None = None,
        line: int | None = None,
    ) -> None:
        self.findings.append(Finding(severity, code, message, path, line))


@dataclass
class ParsedYaml:
    value: Any
    lines: dict[tuple[Any, ...], int]


class MetadataValidator:
    def __init__(self, root: Path, report: ValidationReport) -> None:
        self.root = root
        self.report = report
        self.schema: dict[str, Any] | None = None
        self.schema_validator: Any | None = None
        self.vocabularies: dict[str, dict[str, Any]] = {}
        self.vocabulary_lines: dict[str, dict[tuple[Any, ...], int]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.record_paths: dict[str, str] = {}
        self.record_lines: dict[str, dict[tuple[Any, ...], int]] = {}

    def validate(self) -> None:
        self._load_schema()
        self._load_vocabularies()
        self._load_records()
        self._validate_record_schema_and_values()
        self._validate_database_invariants()

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _unsafe_path_component(self, path: Path) -> Path | None:
        """Return a symlink/junction component or a path that escapes the repository."""
        try:
            relative_parts = path.relative_to(self.root).parts
        except ValueError:
            return path

        current = self.root
        for part in relative_parts:
            current /= part
            is_junction = getattr(current, "is_junction", lambda: False)
            if current.is_symlink() or is_junction():
                return current

        try:
            path.resolve(strict=False).relative_to(self.root)
        except (OSError, RuntimeError, ValueError):
            return path
        return None

    def _reject_unsafe_path(self, path: Path, kind: str, label: str) -> bool:
        unsafe_component = self._unsafe_path_component(path)
        if unsafe_component is None:
            return False
        self.report.add(
            "error",
            f"{kind}.symlink",
            f"{label} must not use symbolic links, junctions, or paths outside the repository.",
            self._relative(unsafe_component),
        )
        return True

    def _line_for(
        self, lines: dict[tuple[Any, ...], int], path: Sequence[Any]
    ) -> int | None:
        candidate = tuple(path)
        while candidate:
            if candidate in lines:
                return lines[candidate]
            candidate = candidate[:-1]
        return lines.get((), 1)

    def _load_schema(self) -> None:
        path = self.root / "database" / "schema" / "paper.schema.json"
        relative = self._relative(path)
        if self._reject_unsafe_path(path, "schema", "Paper schema path"):
            return
        if not path.is_file():
            self.report.add("error", "schema.missing", "Paper schema is missing.", relative)
            return
        try:
            text = path.read_text(encoding="utf-8")
            schema = json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(schema, dict):
                self.report.add(
                    "error",
                    "schema.root",
                    "Paper schema must be a JSON object.",
                    relative,
                )
                return
            if _json_nesting_exceeds(schema, MAX_SCHEMA_NESTING_DEPTH):
                self.report.add(
                    "error",
                    "schema.nesting_depth",
                    "Schema nesting exceeds the supported depth of "
                    f"{MAX_SCHEMA_NESTING_DEPTH}.",
                    relative,
                )
                return
            self.schema = schema
            external_references = list(_find_external_schema_references(schema))
            if external_references:
                references = ", ".join(repr(value) for value in external_references)
                self.report.add(
                    "error",
                    "schema.reference",
                    "Schema references must be local fragments; external or ambient "
                    f"references are not allowed: {references}.",
                    relative,
                )
                return
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            self.schema_validator = validator_class(
                schema,
                format_checker=FormatChecker(),
                registry=Registry(),
            )
        except UnicodeDecodeError as exc:
            self.report.add(
                "error", "schema.encoding", f"Schema must be UTF-8: {exc}.", relative
            )
        except json.JSONDecodeError as exc:
            self.report.add(
                "error",
                "schema.json",
                f"Invalid JSON: {exc.msg}.",
                relative,
                exc.lineno,
            )
        except DuplicateJsonKeyError as exc:
            self.report.add("error", "schema.duplicate_key", str(exc), relative)
        except InvalidJsonConstantError as exc:
            self.report.add("error", "schema.json", str(exc), relative)
        except ValueError as exc:
            self.report.add("error", "schema.json", f"Invalid JSON value: {exc}.", relative)
        except RecursionError:
            self.report.add(
                "error",
                "schema.nesting_depth",
                "Schema nesting exceeds the supported depth of "
                f"{MAX_SCHEMA_NESTING_DEPTH}.",
                relative,
            )
        except SchemaError as exc:
            self.report.add(
                "error",
                "schema.invalid",
                f"Invalid JSON Schema: {exc.message}.",
                relative,
            )

    def _load_vocabularies(self) -> None:
        directory = self.root / "database" / "vocabularies"
        if self._reject_unsafe_path(
            directory, "vocabulary", "Vocabulary directory path"
        ):
            return
        if not directory.is_dir():
            self.report.add(
                "error",
                "vocabulary.directory_missing",
                "Vocabulary directory is missing.",
                self._relative(directory),
            )
            return

        for filename, required_fields in VOCABULARY_SPECS.items():
            path = directory / filename
            relative = self._relative(path)
            parsed = self._read_yaml(path, "vocabulary")
            if parsed is None:
                continue
            self.vocabulary_lines[filename] = parsed.lines
            if not isinstance(parsed.value, dict) or not parsed.value:
                self.report.add(
                    "error",
                    "vocabulary.root",
                    "Vocabulary must be a non-empty mapping.",
                    relative,
                    self._line_for(parsed.lines, ()),
                )
                continue

            vocabulary = parsed.value
            self.vocabularies[filename] = vocabulary
            seen_labels: dict[str, str] = {}
            for key, entry in vocabulary.items():
                key_path = (key,)
                key_line = self._line_for(parsed.lines, key_path)
                if not isinstance(key, str) or not VOCABULARY_KEY_RE.fullmatch(key):
                    self.report.add(
                        "error",
                        "vocabulary.key",
                        f"Vocabulary key {key!r} must use lower_snake_case.",
                        relative,
                        key_line,
                    )
                if not isinstance(entry, dict):
                    self.report.add(
                        "error",
                        "vocabulary.entry",
                        f"Entry {key!r} must be a mapping.",
                        relative,
                        key_line,
                    )
                    continue

                actual_fields = set(entry)
                missing = required_fields - actual_fields
                extra = actual_fields - required_fields
                if missing:
                    self.report.add(
                        "error",
                        "vocabulary.required",
                        f"Entry {key!r} is missing: {', '.join(sorted(missing))}.",
                        relative,
                        key_line,
                    )
                if extra:
                    self.report.add(
                        "error",
                        "vocabulary.additional_property",
                        f"Entry {key!r} has unsupported fields: {', '.join(sorted(map(str, extra)))}.",
                        relative,
                        key_line,
                    )

                for field_name in required_fields:
                    if field_name not in entry:
                        continue
                    value = entry[field_name]
                    line = self._line_for(parsed.lines, (key, field_name))
                    if not isinstance(value, str) or not value.strip():
                        self.report.add(
                            "error",
                            "vocabulary.value",
                            f"{key}.{field_name} must be a non-blank string.",
                            relative,
                            line,
                        )
                    elif value != value.strip():
                        self.report.add(
                            "error",
                            "vocabulary.whitespace",
                            f"{key}.{field_name} has leading or trailing whitespace.",
                            relative,
                            line,
                        )

                label = entry.get("label")
                if isinstance(label, str) and label.strip():
                    normalized_label = _normalize_text(label)
                    if normalized_label in seen_labels:
                        self.report.add(
                            "error",
                            "vocabulary.duplicate_label",
                            f"Label {label!r} is also used by {seen_labels[normalized_label]!r}.",
                            relative,
                            self._line_for(parsed.lines, (key, "label")),
                        )
                    else:
                        seen_labels[normalized_label] = str(key)

        self._validate_relationship_vocabulary()

    def _validate_relationship_vocabulary(self) -> None:
        filename = "relationship-types.yaml"
        relationships = self.vocabularies.get(filename, {})
        lines = self.vocabulary_lines.get(filename, {})
        relative = f"database/vocabularies/{filename}"
        for relationship_type, entry in relationships.items():
            if not isinstance(entry, dict):
                continue
            inverse = entry.get("inverse")
            if not isinstance(inverse, str) or not inverse.strip():
                continue
            if inverse not in relationships:
                self.report.add(
                    "error",
                    "relationship.inverse_missing",
                    f"{relationship_type!r} names undefined inverse {inverse!r}.",
                    relative,
                    self._line_for(lines, (relationship_type, "inverse")),
                )
                continue
            inverse_entry = relationships[inverse]
            inverse_of_inverse = (
                inverse_entry.get("inverse") if isinstance(inverse_entry, dict) else None
            )
            if inverse_of_inverse != relationship_type:
                self.report.add(
                    "error",
                    "relationship.inverse_not_reciprocal",
                    f"The inverse of {relationship_type!r} is {inverse!r}, but the inverse "
                    f"of {inverse!r} is {inverse_of_inverse!r}.",
                    relative,
                    self._line_for(lines, (relationship_type, "inverse")),
                )

    def _load_records(self) -> None:
        directory = self.root / "database" / "records"
        relative_directory = self._relative(directory)
        if self._reject_unsafe_path(directory, "record", "Record directory path"):
            return
        if not directory.is_dir():
            self.report.add(
                "error",
                "record.directory_missing",
                "Record directory is missing.",
                relative_directory,
            )
            return

        seen_ids: dict[str, str] = {}
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            relative = self._relative(path)
            if path.is_symlink():
                self.report.add(
                    "error",
                    "record.symlink",
                    "Record entries must not be symbolic links.",
                    relative,
                )
                continue
            if path.name in ALLOWED_RECORD_DIRECTORY_FILES and path.is_file():
                continue
            if not path.is_file():
                self.report.add(
                    "error",
                    "record.unexpected_entry",
                    "Only record YAML files are allowed in the record directory.",
                    relative,
                )
                continue
            match = RECORD_NAME_RE.fullmatch(path.name)
            if not match:
                self.report.add(
                    "error",
                    "record.filename",
                    "Record filename must be exactly five digits followed by .yaml.",
                    relative,
                )
                continue

            record_id = match.group("id")
            if int(record_id) < 1:
                self.report.add(
                    "error",
                    "record.id_range",
                    "Record IDs begin at 00001; 00000 is not valid.",
                    relative,
                )
            if record_id in seen_ids:
                self.report.add(
                    "error",
                    "record.duplicate_id",
                    f"Record ID {record_id} is also defined by {seen_ids[record_id]}.",
                    relative,
                )
                continue
            seen_ids[record_id] = relative

            parsed = self._read_yaml(path, "record")
            if parsed is None:
                continue
            self.record_lines[record_id] = parsed.lines
            if not isinstance(parsed.value, dict):
                self.report.add(
                    "error",
                    "record.root",
                    "Record YAML must contain exactly one mapping object.",
                    relative,
                    self._line_for(parsed.lines, ()),
                )
                continue
            self.records[record_id] = parsed.value
            self.record_paths[record_id] = relative

        self.report.record_count = len(self.records)
        if seen_ids and "00001" not in seen_ids:
            self.report.add(
                "error",
                "record.first_id",
                "A non-empty database must begin with record 00001.",
                relative_directory,
            )

    def _read_yaml(self, path: Path, kind: str) -> ParsedYaml | None:
        relative = self._relative(path)
        if self._reject_unsafe_path(path, kind, f"Required {kind} file path"):
            return None
        if not path.is_file():
            self.report.add(
                "error", f"{kind}.missing", f"Required {kind} file is missing.", relative
            )
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            self.report.add(
                "error", f"{kind}.encoding", f"File must be UTF-8: {exc}.", relative
            )
            return None

        try:
            nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        except RecursionError:
            self.report.add(
                "error",
                f"{kind}.nesting_depth",
                f"YAML nesting exceeds the supported depth of {MAX_YAML_NESTING_DEPTH}.",
                relative,
            )
            return None
        except yaml.MarkedYAMLError as exc:
            line = exc.problem_mark.line + 1 if exc.problem_mark else None
            problem = exc.problem or str(exc).splitlines()[0]
            self.report.add(
                "error", f"{kind}.yaml", f"Invalid YAML: {problem}.", relative, line
            )
            return None
        except yaml.YAMLError as exc:
            self.report.add(
                "error", f"{kind}.yaml", f"Invalid YAML: {exc}.", relative
            )
            return None

        if len(nodes) != 1:
            self.report.add(
                "error",
                f"{kind}.document_count",
                f"Expected exactly one YAML document, found {len(nodes)}.",
                relative,
            )
            return None
        node = nodes[0]
        node_issue = _yaml_node_graph_issue(node)
        if node_issue == "cycle":
            self.report.add(
                "error",
                f"{kind}.recursive_alias",
                "Recursive YAML aliases are not supported.",
                relative,
                node.start_mark.line + 1 if node is not None else None,
            )
            return None
        if node_issue == "alias":
            self.report.add(
                "error",
                f"{kind}.alias",
                "YAML aliases are not supported.",
                relative,
                node.start_mark.line + 1 if node is not None else None,
            )
            return None
        if node_issue == "depth":
            self.report.add(
                "error",
                f"{kind}.nesting_depth",
                f"YAML nesting exceeds the supported depth of {MAX_YAML_NESTING_DEPTH}.",
                relative,
                node.start_mark.line + 1 if node is not None else None,
            )
            return None

        try:
            documents = list(yaml.load_all(text, Loader=StrictSafeLoader))
        except RecursionError:
            self.report.add(
                "error",
                f"{kind}.nesting_depth",
                f"YAML nesting exceeds the supported depth of {MAX_YAML_NESTING_DEPTH}.",
                relative,
            )
            return None
        except yaml.MarkedYAMLError as exc:
            line = exc.problem_mark.line + 1 if exc.problem_mark else None
            problem = exc.problem or str(exc).splitlines()[0]
            self.report.add(
                "error", f"{kind}.yaml", f"Invalid YAML: {problem}.", relative, line
            )
            return None
        except yaml.YAMLError as exc:
            self.report.add(
                "error", f"{kind}.yaml", f"Invalid YAML: {exc}.", relative
            )
            return None
        except YAML_VALUE_ERRORS as exc:
            self.report.add(
                "error", f"{kind}.yaml", f"Invalid YAML value: {exc}.", relative
            )
            return None

        if len(documents) != 1:
            self.report.add(
                "error",
                f"{kind}.document_count",
                f"Expected exactly one YAML document, found {len(documents)}.",
                relative,
            )
            return None
        return ParsedYaml(documents[0], _build_line_map(node))

    def _validate_record_schema_and_values(self) -> None:
        document_types = self.vocabularies.get("document-types.yaml", {})
        publication_stages = self.vocabularies.get("publication-stages.yaml", {})
        record_statuses = self.vocabularies.get("record-statuses.yaml", {})
        relationship_types = self.vocabularies.get("relationship-types.yaml", {})

        for record_id, record in sorted(self.records.items()):
            relative = self.record_paths[record_id]
            lines = self.record_lines.get(record_id, {})
            non_string_keys = list(_find_non_string_keys(record))
            for parent_path, key in non_string_keys:
                self.report.add(
                    "error",
                    "record.non_string_key",
                    f"{_format_value_path(parent_path)} contains non-string key {key!r}; YAML records must be JSON-compatible.",
                    relative,
                    self._line_for(lines, parent_path),
                )
            if self.schema_validator is not None and not non_string_keys:
                try:
                    errors = sorted(
                        self.schema_validator.iter_errors(record),
                        key=lambda error: (
                            [str(component) for component in error.absolute_path],
                            error.message,
                        ),
                    )
                except Unresolvable as exc:
                    self.report.add(
                        "error",
                        "schema.reference",
                        f"Schema reference could not be resolved: {exc}.",
                        "database/schema/paper.schema.json",
                    )
                    self.schema_validator = None
                except RecursionError:
                    self.report.add(
                        "error",
                        "schema.reference_cycle",
                        "Schema validation exceeded the supported recursion depth; "
                        "check for cyclic local references.",
                        "database/schema/paper.schema.json",
                    )
                    self.schema_validator = None
                else:
                    for error in errors:
                        value_path = tuple(error.absolute_path)
                        self.report.add(
                            "error",
                            f"schema.{error.validator}",
                            f"{_format_value_path(value_path)}: {error.message}.",
                            relative,
                            self._line_for(lines, value_path),
                        )

            self._check_vocab_value(
                record, "document_type", document_types, relative, lines
            )
            self._check_vocab_value(
                record, "publication_stage", publication_stages, relative, lines
            )
            if "pip_litdb_status" in record:
                self._check_vocab_value(
                    record, "pip_litdb_status", record_statuses, relative, lines
                )

            single_line_paths: list[tuple[Any, ...]] = [
                ("document_type",),
                ("publication_stage",),
                ("title",),
                ("doi",),
                ("url",),
                ("journal",),
                ("pip_litdb_status",),
            ]
            authors = record.get("authors")
            if isinstance(authors, list):
                single_line_paths.extend(("authors", index, "name") for index in range(len(authors)))
            relationships = record.get("related_papers")
            if isinstance(relationships, list):
                for index in range(len(relationships)):
                    single_line_paths.extend(
                        (
                            ("related_papers", index, "pip_litdb_id"),
                            ("related_papers", index, "relationship_type"),
                        )
                    )

            for value_path in single_line_paths:
                value = _get_nested(record, value_path)
                if not isinstance(value, str):
                    continue
                if not value.strip():
                    self.report.add(
                        "error",
                        "record.blank_string",
                        f"{_format_value_path(value_path)} must not be blank.",
                        relative,
                        self._line_for(lines, value_path),
                    )
                elif value != value.strip():
                    self.report.add(
                        "error",
                        "record.whitespace",
                        f"{_format_value_path(value_path)} has leading or trailing whitespace.",
                        relative,
                        self._line_for(lines, value_path),
                    )
                if "\n" in value or "\r" in value:
                    self.report.add(
                        "error",
                        "record.multiline_value",
                        f"{_format_value_path(value_path)} must be a single-line value.",
                        relative,
                        self._line_for(lines, value_path),
                    )

            if isinstance(authors, list):
                seen_authors: dict[str, int] = {}
                for index, author in enumerate(authors):
                    if not isinstance(author, dict) or not isinstance(author.get("name"), str):
                        continue
                    normalized = _normalize_text(author["name"])
                    if normalized in seen_authors:
                        self.report.add(
                            "error",
                            "record.duplicate_author",
                            f"Author {author['name']!r} duplicates authors[{seen_authors[normalized]}].",
                            relative,
                            self._line_for(lines, ("authors", index, "name")),
                        )
                    else:
                        seen_authors[normalized] = index

            doi = record.get("doi")
            url = record.get("url")
            parsed_url = self._validate_url(url, relative, lines) if isinstance(url, str) else None
            if isinstance(doi, str) and parsed_url is not None:
                host = (parsed_url.hostname or "").casefold().rstrip(".")
                if host in {"doi.org", "dx.doi.org"}:
                    url_doi = unquote(parsed_url.path.lstrip("/"))
                    if _normalize_doi(url_doi) != _normalize_doi(doi):
                        self.report.add(
                            "error",
                            "record.doi_url_mismatch",
                            f"The doi.org URL resolves {url_doi!r}, not the record DOI {doi!r}.",
                            relative,
                            self._line_for(lines, ("url",)),
                        )

            if isinstance(relationships, list):
                seen_relationships: set[tuple[str, str]] = set()
                for index, relationship in enumerate(relationships):
                    if not isinstance(relationship, dict):
                        continue
                    target_id = relationship.get("pip_litdb_id")
                    relationship_type = relationship.get("relationship_type")
                    if isinstance(relationship_type, str) and relationship_type not in relationship_types:
                        self.report.add(
                            "error",
                            "record.unknown_relationship_type",
                            f"Relationship type {relationship_type!r} is not in relationship-types.yaml.",
                            relative,
                            self._line_for(lines, ("related_papers", index, "relationship_type")),
                        )
                    if isinstance(target_id, str) and isinstance(relationship_type, str):
                        pair = (target_id, relationship_type)
                        if pair in seen_relationships:
                            self.report.add(
                                "error",
                                "record.duplicate_relationship",
                                f"Duplicate relationship {relationship_type!r} to {target_id}.",
                                relative,
                                self._line_for(lines, ("related_papers", index)),
                            )
                        seen_relationships.add(pair)
                        if target_id == record_id:
                            self.report.add(
                                "error",
                                "record.self_relationship",
                                "A record cannot relate to itself.",
                                relative,
                                self._line_for(lines, ("related_papers", index, "pip_litdb_id")),
                            )

            notes = record.get("pip_litdb_notes")
            if isinstance(notes, str):
                if not notes.strip():
                    self.report.add(
                        "error",
                        "record.blank_string",
                        "$.pip_litdb_notes must not be blank.",
                        relative,
                        self._line_for(lines, ("pip_litdb_notes",)),
                    )
                searchable_notes = PUBLIC_URL_RE.sub("", notes)
                for pattern in PRIVATE_REFERENCE_PATTERNS:
                    if pattern.search(searchable_notes):
                        self.report.add(
                            "error",
                            "record.private_reference",
                            "Public metadata notes must not contain private or local filesystem references.",
                            relative,
                            self._line_for(lines, ("pip_litdb_notes",)),
                        )
                        break

    def _check_vocab_value(
        self,
        record: dict[str, Any],
        field_name: str,
        vocabulary: dict[str, Any],
        relative: str,
        lines: dict[tuple[Any, ...], int],
    ) -> None:
        value = record.get(field_name)
        if isinstance(value, str) and value not in vocabulary:
            self.report.add(
                "error",
                "record.unknown_vocabulary_value",
                f"{field_name} value {value!r} is not defined in its vocabulary.",
                relative,
                self._line_for(lines, (field_name,)),
            )

    def _validate_url(
        self,
        url: str,
        relative: str,
        lines: dict[tuple[Any, ...], int],
    ) -> Any | None:
        try:
            parsed = urlsplit(url)
            # Accessing port catches malformed values that urlsplit otherwise accepts.
            _ = parsed.port
        except ValueError as exc:
            self.report.add(
                "error",
                "record.url_format",
                f"URL is malformed: {exc}.",
                relative,
                self._line_for(lines, ("url",)),
            )
            return None
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            self.report.add(
                "error",
                "record.url_format",
                "URL must be an absolute http:// or https:// URL with a host.",
                relative,
                self._line_for(lines, ("url",)),
            )
            return None
        if parsed.username is not None or parsed.password is not None:
            self.report.add(
                "error",
                "record.url_credentials",
                "Public metadata URLs must not contain embedded credentials.",
                relative,
                self._line_for(lines, ("url",)),
            )
        return parsed

    def _validate_database_invariants(self) -> None:
        dois: dict[str, list[str]] = defaultdict(list)
        urls: dict[str, list[str]] = defaultdict(list)
        title_years: dict[tuple[str, int], list[str]] = defaultdict(list)

        for record_id, record in self.records.items():
            doi = record.get("doi")
            if isinstance(doi, str) and doi.strip():
                dois[_normalize_doi(doi)].append(record_id)
            url = record.get("url")
            if isinstance(url, str) and url.strip():
                urls[_normalize_url(url)].append(record_id)
            title = record.get("title")
            publication_year = record.get("publication_year")
            if (
                isinstance(title, str)
                and title.strip()
                and isinstance(publication_year, int)
                and not isinstance(publication_year, bool)
            ):
                title_years[(_normalize_title(title), publication_year)].append(record_id)

        for normalized_doi, record_ids in sorted(dois.items()):
            if len(record_ids) > 1:
                joined = ", ".join(record_ids)
                for record_id in record_ids:
                    self.report.add(
                        "error",
                        "database.duplicate_doi",
                        f"DOI {normalized_doi!r} is shared by records {joined}.",
                        self.record_paths.get(record_id),
                        self._line_for(self.record_lines.get(record_id, {}), ("doi",)),
                    )

        for normalized_url, record_ids in sorted(urls.items()):
            if len(record_ids) > 1:
                joined = ", ".join(record_ids)
                for record_id in record_ids:
                    self.report.add(
                        "warning",
                        "database.duplicate_url",
                        f"URL {normalized_url!r} is shared by records {joined}; confirm they are distinct papers.",
                        self.record_paths.get(record_id),
                        self._line_for(self.record_lines.get(record_id, {}), ("url",)),
                    )

        for (_normalized_title, year), record_ids in sorted(title_years.items()):
            if len(record_ids) > 1:
                joined = ", ".join(record_ids)
                for record_id in record_ids:
                    self.report.add(
                        "warning",
                        "database.possible_duplicate",
                        f"Normalized title and publication year {year!r} match records {joined}; confirm they are distinct papers.",
                        self.record_paths.get(record_id),
                        self._line_for(self.record_lines.get(record_id, {}), ("title",)),
                    )

        relationships = self.vocabularies.get("relationship-types.yaml", {})
        for record_id, record in sorted(self.records.items()):
            entries = record.get("related_papers")
            if not isinstance(entries, list):
                continue
            for index, relationship in enumerate(entries):
                if not isinstance(relationship, dict):
                    continue
                target_id = relationship.get("pip_litdb_id")
                relationship_type = relationship.get("relationship_type")
                if not isinstance(target_id, str) or not isinstance(relationship_type, str):
                    continue
                if target_id not in self.records:
                    self.report.add(
                        "error",
                        "relationship.target_missing",
                        f"Relationship points to missing record {target_id}.",
                        self.record_paths[record_id],
                        self._line_for(
                            self.record_lines.get(record_id, {}),
                            ("related_papers", index, "pip_litdb_id"),
                        ),
                    )
                    continue
                relationship_entry = relationships.get(relationship_type)
                inverse = (
                    relationship_entry.get("inverse")
                    if isinstance(relationship_entry, dict)
                    else None
                )
                if not isinstance(inverse, str) or target_id == record_id:
                    continue
                target_relationships = self.records[target_id].get("related_papers", [])
                if not isinstance(target_relationships, list):
                    target_relationships = []
                matches = [
                    candidate
                    for candidate in target_relationships
                    if isinstance(candidate, dict)
                    and candidate.get("pip_litdb_id") == record_id
                    and candidate.get("relationship_type") == inverse
                ]
                if len(matches) != 1:
                    self.report.add(
                        "error",
                        "relationship.inverse_count",
                        f"{relationship_type!r} from {record_id} to {target_id} requires exactly one "
                        f"{inverse!r} relationship from {target_id} to {record_id}; found {len(matches)}.",
                        self.record_paths[record_id],
                        self._line_for(
                            self.record_lines.get(record_id, {}),
                            ("related_papers", index),
                        ),
                    )


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"Duplicate JSON object key {key!r}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise InvalidJsonConstantError(
        f"Non-standard JSON numeric constant {value!r} is not allowed."
    )


def _json_nesting_exceeds(value: Any, maximum_depth: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum_depth:
            return True
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return False


def _find_external_schema_references(value: Any) -> Iterable[str]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if (
                    key in {"$ref", "$dynamicRef", "$recursiveRef"}
                    and isinstance(child, str)
                    and not child.startswith("#")
                ):
                    yield child
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)


def _yaml_node_graph_issue(node: Node | None) -> str | None:
    if node is None:
        return None

    active: set[int] = set()
    seen: set[int] = set()
    stack: list[tuple[Node, int, bool]] = [(node, 0, False)]
    while stack:
        current, depth, exiting = stack.pop()
        node_id = id(current)
        if exiting:
            active.remove(node_id)
            continue
        if depth > MAX_YAML_NESTING_DEPTH:
            return "depth"
        if node_id in active:
            return "cycle"
        if node_id in seen:
            return "alias"

        active.add(node_id)
        seen.add(node_id)
        stack.append((current, depth, True))
        if isinstance(current, MappingNode):
            children = [
                child
                for key_node, value_node in current.value
                for child in (key_node, value_node)
            ]
        elif isinstance(current, SequenceNode):
            children = list(current.value)
        else:
            children = []
        stack.extend((child, depth + 1, False) for child in reversed(children))
    return None


def _build_line_map(node: Node | None, path: tuple[Any, ...] = ()) -> dict[tuple[Any, ...], int]:
    if node is None:
        return {}
    lines: dict[tuple[Any, ...], int] = {path: node.start_mark.line + 1}
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, ScalarNode) else str(key_node.value)
            child_path = path + (key,)
            lines[child_path] = key_node.start_mark.line + 1
            lines.update(_build_line_map(value_node, child_path))
    elif isinstance(node, SequenceNode):
        for index, value_node in enumerate(node.value):
            child_path = path + (index,)
            lines.update(_build_line_map(value_node, child_path))
    return lines


def _get_nested(value: Any, path: Sequence[Any]) -> Any:
    current = value
    try:
        for component in path:
            current = current[component]
    except (KeyError, IndexError, TypeError):
        return None
    return current


def _find_non_string_keys(
    value: Any, path: tuple[Any, ...] = ()
) -> Iterable[tuple[tuple[Any, ...], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                yield path, key
                continue
            yield from _find_non_string_keys(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _find_non_string_keys(child, path + (index,))


def _format_value_path(path: Sequence[Any]) -> str:
    if not path:
        return "$"
    result = "$"
    for component in path:
        if isinstance(component, int):
            result += f"[{component}]"
        else:
            result += f".{component}"
    return result


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _normalize_title(value: str) -> str:
    return "".join(character for character in _normalize_text(value) if character.isalnum())


def _normalize_doi(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _normalize_url(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        return value.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    port = f":{parsed_port}" if parsed_port is not None else ""
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.casefold()}://{host}{port}{path}?{parsed.query}".rstrip("?")


def _git_command(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", *arguments],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_file(root: Path, revision: str, path: str) -> Any | None:
    result = _git_command(root, "show", f"{revision}:{path}")
    if result.returncode != 0:
        return None
    try:
        text = result.stdout.decode("utf-8")
        nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        if len(nodes) != 1 or _yaml_node_graph_issue(nodes[0]) is not None:
            return None
        documents = list(yaml.load_all(text, Loader=StrictSafeLoader))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return None
    except YAML_VALUE_ERRORS:
        return None
    return documents[0] if len(documents) == 1 else None


def _record_id_from_path(path: str | None) -> str | None:
    if path is None:
        return None
    match = RECORD_NAME_RE.fullmatch(Path(path).name)
    return match.group("id") if match else None


def detect_record_changes(
    root: Path,
    base: str,
    head: str,
    report: ValidationReport,
    comparison: str = "merge-base",
) -> list[RecordChange]:
    if comparison not in {"merge-base", "direct"}:
        report.add(
            "error",
            "diff.comparison",
            f"Unsupported Git comparison mode {comparison!r}.",
        )
        return []

    for name, revision in (("base", base), ("head", head)):
        result = _git_command(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            report.add(
                "error",
                "diff.invalid_revision",
                f"Cannot resolve {name} revision {revision!r}: {message}.",
            )
            return []

    comparison_base = base
    if comparison == "merge-base":
        result = _git_command(root, "merge-base", base, head)
        if result.returncode != 0 or not result.stdout.strip():
            message = result.stderr.decode("utf-8", errors="replace").strip()
            detail = message or "the revisions do not share a merge base"
            report.add(
                "error",
                "diff.merge_base",
                f"Could not resolve the merge base for {base!r} and {head!r}: {detail}.",
            )
            return []
        comparison_base = result.stdout.decode("ascii").strip()

    result = _git_command(
        root,
        "diff",
        "--name-status",
        "-z",
        # A high threshold avoids misclassifying deletion + addition of two
        # structurally similar YAML records as an ID rename.
        "--find-renames=90%",
        "--find-copies=90%",
        "--find-copies-harder",
        f"{comparison_base}..{head}",
        "--",
        "database/records",
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        report.add(
            "error",
            "diff.failed",
            f"Could not compare metadata changes: {message}.",
        )
        return []

    tokens = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: list[RecordChange] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        code = status[:1]
        if code in {"R", "C"}:
            if index + 1 >= len(tokens):
                report.add("error", "diff.parse", "Git returned an incomplete rename/copy entry.")
                break
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
        else:
            if index >= len(tokens):
                report.add("error", "diff.parse", "Git returned an incomplete change entry.")
                break
            path = tokens[index]
            index += 1
            old_path = path if code in {"D", "M", "T"} else None
            new_path = path if code in {"A", "M", "T"} else None

        if old_path == "database/records/.gitkeep" or new_path == "database/records/.gitkeep":
            continue
        kind = {
            "A": "added",
            "D": "removed",
            "M": "modified",
            "T": "modified",
            "R": "renamed",
            "C": "copied",
        }.get(code, "modified")
        old_value = new_value = None
        if old_path and new_path:
            old_value = _git_file(root, comparison_base, old_path)
            new_value = _git_file(root, head, new_path)
        changed_fields: tuple[str, ...] = ()
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            changed_fields = tuple(
                sorted(
                    str(key)
                    for key in set(old_value) | set(new_value)
                    if old_value.get(key) != new_value.get(key)
                )
            )
        changes.append(
            RecordChange(
                kind=kind,
                old_path=old_path,
                new_path=new_path,
                old_id=_record_id_from_path(old_path),
                new_id=_record_id_from_path(new_path),
                changed_fields=changed_fields,
            )
        )

        if code in {"A", "C", "R"} and new_path is not None:
            history = _git_command(
                root,
                "log",
                "-n",
                "1",
                "--format=%H",
                base,
                "--",
                new_path,
            )
            if history.returncode != 0:
                message = history.stderr.decode("utf-8", errors="replace").strip()
                report.add(
                    "error",
                    "diff.history_failed",
                    f"Could not inspect record history for {new_path}: {message}.",
                    new_path,
                )
            elif history.stdout.strip():
                report.add(
                    "error",
                    "change.id_reused",
                    f"Record ID {_record_id_from_path(new_path) or new_path} existed "
                    "before the base revision and must not be reused.",
                    new_path,
                )
    return changes


def _add_change_warnings(report: ValidationReport) -> None:
    for change in report.changes:
        path = change.new_path or change.old_path
        if change.kind == "removed":
            report.add(
                "warning",
                "change.record_removed",
                f"Record {change.old_id or change.old_path} is removed; confirm the deletion is intentional and the ID will not be reused.",
                path,
            )
        elif change.kind == "renamed" and change.old_id != change.new_id:
            report.add(
                "warning",
                "change.id_changed",
                f"Record ID changes from {change.old_id or change.old_path} to {change.new_id or change.new_path}; filenames are permanent identifiers.",
                path,
            )
        if change.kind in {"modified", "renamed"}:
            identity_changes = sorted(IDENTITY_FIELDS.intersection(change.changed_fields))
            if identity_changes:
                report.add(
                    "warning",
                    "change.identity_modified",
                    f"Identity-bearing fields changed: {', '.join(identity_changes)}; verify this is still the same paper.",
                    path,
                )


def validate_repository(
    root: Path | str,
    base: str | None = None,
    head: str = "HEAD",
    comparison: str = "merge-base",
) -> ValidationReport:
    root_path = Path(root).resolve()
    report = ValidationReport(root=root_path, compared_base=base, compared_head=head if base else None)
    MetadataValidator(root_path, report).validate()
    if base:
        report.changes = detect_record_changes(
            root_path, base, head, report, comparison=comparison
        )
        _add_change_warnings(report)
    report.findings.sort(key=Finding.sort_key)
    return report


def _compress_ids(ids: Iterable[str]) -> str:
    valid_ids = sorted({value for value in ids if value and value.isdigit()}, key=int)
    if not valid_ids:
        return "None"
    ranges: list[str] = []
    start = previous = valid_ids[0]
    for current in valid_ids[1:]:
        if int(current) == int(previous) + 1:
            previous = current
            continue
        ranges.append(start if start == previous else f"{start}\u2013{previous}")
        start = previous = current
    ranges.append(start if start == previous else f"{start}\u2013{previous}")
    return ", ".join(ranges)


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown_summary(report: ValidationReport) -> str:
    icon = "\u2705" if report.passed else "\u274c"
    status = "Passed" if report.passed else "Failed"
    lines = [
        "## Metadata validation",
        "",
        f"{icon} **{status}** \u2014 {report.record_count} resulting records, "
        f"{len(report.errors)} errors, and {len(report.warnings)} warnings.",
    ]

    if report.compared_base:
        counts = {
            kind: sum(change.kind == kind for change in report.changes)
            for kind in ("added", "modified", "removed", "renamed", "copied")
        }
        lines.extend(
            [
                "",
                "### Pull request record changes",
                "",
                "| Added | Modified | Removed | Renamed | Copied |",
                "| ---: | ---: | ---: | ---: | ---: |",
                f"| {counts['added']} | {counts['modified']} | {counts['removed']} | {counts['renamed']} | {counts['copied']} |",
            ]
        )
        for label, kind, attribute in (
            ("Added IDs", "added", "new_id"),
            ("Modified IDs", "modified", "new_id"),
            ("Removed IDs", "removed", "old_id"),
        ):
            values = [getattr(change, attribute) for change in report.changes if change.kind == kind]
            if values:
                lines.append(f"- **{label}:** `{_compress_ids(value for value in values if value)}`")

        detailed = [
            change
            for change in report.changes
            if change.kind in {"modified", "renamed", "copied"}
        ]
        if detailed:
            lines.extend(
                [
                    "",
                    "| Change | Record | Fields changed |",
                    "| --- | --- | --- |",
                ]
            )
            for change in detailed[:100]:
                if change.kind == "renamed":
                    record = f"{change.old_id or change.old_path} \u2192 {change.new_id or change.new_path}"
                else:
                    record = change.new_id or change.old_id or change.new_path or change.old_path or "unknown"
                fields = ", ".join(change.changed_fields) or "None detected"
                lines.append(
                    f"| {_markdown_escape(change.kind.title())} | `{_markdown_escape(record)}` | "
                    f"{_markdown_escape(fields)} |"
                )
            if len(detailed) > 100:
                lines.append(f"\n_{len(detailed) - 100} additional detailed changes omitted._")

    if report.findings:
        lines.extend(["", "### Findings", ""])
        for finding in report.findings[:100]:
            icon = "\u274c" if finding.severity == "error" else "\u26a0\ufe0f"
            location = finding.path or "repository"
            if finding.line:
                location += f":{finding.line}"
            lines.append(
                f"- {icon} `{finding.code}` at `{_markdown_escape(location)}`: "
                f"{_markdown_escape(finding.message)}"
            )
        if len(report.findings) > 100:
            lines.append(f"\n_{len(report.findings) - 100} additional findings omitted._")

    lines.extend(
        [
            "",
            "### Coverage",
            "",
            "The check validates strict YAML/JSON parsing, filenames and IDs, JSON Schema, controlled "
            "vocabularies, DOI/URL integrity, duplicate records, author uniqueness, private-path leakage, "
            "relationship targets, and exact reciprocal inverse relationships across the resulting database.",
            "",
        ]
    )
    return "\n".join(lines)


def _annotation_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _configure_stdout() -> None:
    """Keep record-controlled findings printable on narrow Windows encodings."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, TypeError, ValueError):
        try:
            reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, TypeError, ValueError):
            pass


def print_report(report: ValidationReport) -> None:
    for finding in report.findings:
        location = finding.path or "repository"
        if finding.line:
            location += f":{finding.line}"
        print(f"{finding.severity.upper()} [{finding.code}] {location}: {finding.message}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            properties = []
            if finding.path:
                properties.append(f"file={_annotation_escape(finding.path)}")
            if finding.line:
                properties.append(f"line={finding.line}")
            properties.append(f"title={_annotation_escape(finding.code)}")
            print(
                f"::{finding.severity} {','.join(properties)}::"
                f"{_annotation_escape(finding.message)}"
            )

    if report.compared_base:
        counts = defaultdict(int)
        for change in report.changes:
            counts[change.kind] += 1
        print(
            "Record changes: "
            + ", ".join(
                f"{kind}={counts[kind]}"
                for kind in ("added", "modified", "removed", "renamed", "copied")
            )
        )
    print(
        f"Validated {report.record_count} records: {len(report.errors)} errors, "
        f"{len(report.warnings)} warnings."
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Validate all metadata and optionally summarize changes between Git revisions."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help="Repository root (defaults to the parent of this script's directory).",
    )
    parser.add_argument(
        "--base",
        help="Base Git revision used to classify record additions, removals, modifications, and renames.",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        help="Head Git revision used with --base (default: HEAD).",
    )
    parser.add_argument(
        "--comparison",
        choices=("merge-base", "direct"),
        default="merge-base",
        help=(
            "How to compare --base and --head: merge-base for pull-request branch "
            "changes (default), or direct for an exact before-to-after transition."
        ),
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Write a Markdown summary here (defaults to GITHUB_STEP_SUMMARY in GitHub Actions).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdout()
    args = parse_args(argv)
    report = validate_repository(args.root, args.base, args.head, args.comparison)
    print_report(report)
    summary_file = args.summary_file
    if summary_file is None and os.environ.get("GITHUB_STEP_SUMMARY"):
        summary_file = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if summary_file is not None:
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        with summary_file.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(render_markdown_summary(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
