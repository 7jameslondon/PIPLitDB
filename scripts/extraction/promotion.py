"""Safely promote one reviewed staged extraction into its record directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Iterable, Mapping
import uuid

from .discovery import discover_sources, source_fingerprint
from .metadata import load_record_metadata, load_record_status
from .paths import (
    UnsafePathError,
    atomic_write_json,
    ensure_within,
    is_reparse_point,
    reject_reparse_chain,
    sha256_file,
    validate_record_id,
    validate_run_id,
)
from .reporting import _pipeline_code_sha256
from .validation import ValidationReport, validate_candidate


RenameFunction = Callable[[Path, Path], None]


class PromotionError(RuntimeError):
    """A controlled refusal to promote a staged candidate."""


@dataclass(frozen=True)
class PromotionResult:
    record_id: str
    run_id: str
    candidate_root: Path
    extraction_root: Path
    diagnostic_root: Path
    source_fingerprint: str
    accepted_finding_codes: tuple[str, ...]
    finding_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": "approved",
            "record_id": self.record_id,
            "run_id": self.run_id,
            "candidate_root": str(self.candidate_root),
            "extraction": str(self.extraction_root),
            "diagnostic": str(self.diagnostic_root),
            "source_fingerprint": self.source_fingerprint,
            "accepted_finding_codes": list(self.accepted_finding_codes),
            "finding_count": self.finding_count,
        }


@dataclass(frozen=True)
class StagingCleanupResult:
    record_id: str
    approved_run_id: str
    staging_root: Path
    removed: bool
    removed_run_ids: tuple[str, ...]
    checked_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        if self.checked_only:
            status = "eligible" if self.removed_run_ids else "already_absent"
        else:
            status = "removed" if self.removed else "already_absent"
        return {
            "schema_version": "1.0",
            "status": status,
            "record_id": self.record_id,
            "approved_run_id": self.approved_run_id,
            "staging_root": str(self.staging_root),
            "removed": self.removed,
            "removed_run_ids": list(self.removed_run_ids),
            "checked_only": self.checked_only,
        }


# These findings mean the candidate could not be verified as the exact,
# self-contained artifact described by its own manifest.  They are never
# review exceptions: accepting them would defeat promotion's integrity gate.
_INTEGRITY_FINDING_CODES = frozenset(
    {
        "diagnostic_directory_missing",
        "diagnostic_not_utf8",
        "diagnostic_unreadable",
        "empty_link",
        "empty_output_file",
        "escaping_local_link",
        "extraction_directory_missing",
        "invalid_diagnostic_json",
        "invalid_diagnostic_jsonl",
        "invalid_diagnostic_jsonl_row",
        "invalid_manifest_path",
        "manifest_files_invalid",
        "manifest_file_entry_invalid",
        "duplicate_manifest_file",
        "manifest_file_unlisted",
        "output_size_mismatch",
        "invalid_table_json",
        "invalid_table_schema",
        "invalid_table_source_kind",
        "manifest_file_missing",
        "manifest_output_escapes",
        "missing_local_link",
        "missing_table_source_image",
        "non_file_local_link",
        "orphan_output_file",
        "output_hash_mismatch",
        "output_hash_unreadable",
        "record_missing",
        "record_not_utf8",
        "record_unreadable",
        "invalid_record_json",
        "invalid_record_schema",
        "record_schema_unavailable",
        "invalid_record_document_declaration",
        "record_document_declaration_missing",
        "multiple_record_documents",
        "invalid_record_json_coverage_pointer",
        "record_json_reverse_coverage_missing",
        "stale_markdown_coverage",
        "unsafe_record_asset_path",
        "missing_record_asset",
        "duplicate_record_asset_id",
        "duplicate_record_asset_path",
        "duplicate_record_content_id",
        "missing_figure_asset_reference",
        "unknown_figure_asset_reference",
        "unknown_supplement_asset_reference",
        "unregistered_table_asset",
        "table_asset_relationship_mismatch",
        "figure_asset_identity_mismatch",
        "figure_asset_kind_mismatch",
        "record_asset_manifest_mismatch",
        "redundant_table_sidecar",
        "record_dual_text_mismatch",
        "record_identity_mismatch",
        "record_metadata_mismatch",
        "diagnostic_field_in_record_json",
        "unsafe_record_html",
        "required_diagnostic_missing",
        "supplement_copy_escapes",
        "supplement_copy_hash_mismatch",
        "supplement_copy_not_identifiable",
        "supplement_hash_unreadable",
        "supplement_hash_unverifiable",
        "table_schema_unavailable",
        "title_mismatch",
        "unexpected_html_table_source_image",
        "unexpected_table_csv",
        "unlinked_table_source_image",
        "unsafe_external_scheme",
        "unsafe_local_link",
        "unsafe_table_source_image",
    }
)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except FileNotFoundError as exc:
        raise PromotionError(f"required {label} is missing: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise PromotionError(f"could not read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"{label} must contain a JSON object: {path}")
    return value


def _validate_directory_root(root: Path, boundary: Path, label: str) -> None:
    ensure_within(root, boundary)
    reject_reparse_chain(root, boundary)
    if not root.is_dir() or is_reparse_point(root):
        raise PromotionError(f"{label} is not a safe directory: {root}")


def _validate_tree(root: Path, boundary: Path, label: str) -> None:
    """Validate a directory without ever descending through a reparse point."""

    _validate_directory_root(root, boundary, label)

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise PromotionError(f"could not inspect {label}: {directory}: {exc}") from exc
        for child in children:
            if is_reparse_point(child):
                raise PromotionError(f"{label} contains a reparse point: {child}")
            ensure_within(child, root)
            if child.is_dir():
                pending.append(child)
            elif not child.is_file():
                raise PromotionError(f"{label} contains a non-regular entry: {child}")


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    rows: list[tuple[str, str, int, str]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = child.relative_to(root).as_posix()
            if child.is_dir():
                rows.append((relative, "directory", 0, ""))
                pending.append(child)
            else:
                rows.append((relative, "file", child.stat().st_size, sha256_file(child)))
    return tuple(sorted(rows))


def _normalized_source_rows(sources: Iterable[Any]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            (
                source.role,
                source.relative_path,
                source.size,
                source.sha256,
                source.detected_format,
            )
            for source in sources
        )
    )


def _recorded_source_rows(value: Any) -> tuple[tuple[Any, ...], ...]:
    if not isinstance(value, list):
        raise PromotionError("sources.json must contain a sources array")
    rows: list[tuple[Any, ...]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise PromotionError(f"sources.json source {index} must be an object")
        role = item.get("role")
        path = item.get("path")
        size = item.get("bytes")
        sha256 = item.get("sha256")
        detected_format = item.get("detected_format")
        if (
            not isinstance(role, str)
            or not isinstance(path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(sha256, str)
            or not isinstance(detected_format, str)
        ):
            raise PromotionError(f"sources.json source {index} is malformed")
        rows.append((role, path, size, sha256, detected_format))
    return tuple(sorted(rows))


def _verify_current_source_inputs(
    repository_root: Path,
    record_root: Path,
    record_id: str,
    diagnostic_root: Path,
    manifest: Mapping[str, Any],
    sources_document: Mapping[str, Any],
) -> str:
    """Verify current source bytes and the private override against a snapshot."""

    current_sources = discover_sources(repository_root, record_id)
    if _recorded_source_rows(sources_document.get("sources")) != _normalized_source_rows(
        current_sources
    ):
        raise PromotionError("the staged source inventory no longer matches current source files")

    current_override = record_root / "extraction_overrides.yaml"
    staged_override = diagnostic_root / "overrides.yaml"
    current_exists = os.path.lexists(current_override)
    staged_exists = os.path.lexists(staged_override)
    if current_exists:
        reject_reparse_chain(current_override, record_root)
        ensure_within(current_override, record_root)
        if is_reparse_point(current_override) or not current_override.is_file():
            raise PromotionError(f"private extraction override is unsafe: {current_override}")
    if staged_exists and (is_reparse_point(staged_override) or not staged_override.is_file()):
        raise PromotionError(f"staged extraction override is unsafe: {staged_override}")
    if current_exists != staged_exists:
        raise PromotionError("the staged override does not match the current private override")

    override_hash: str | None = None
    if current_exists:
        override_hash = sha256_file(current_override)
        if sha256_file(staged_override) != override_hash:
            raise PromotionError("the private extraction override changed after this run was built")

    fingerprint = source_fingerprint(current_sources, override_hash)
    if sources_document.get("record_id") != record_id:
        raise PromotionError("sources.json record_id does not match the requested record")
    if manifest.get("record_id") != record_id:
        raise PromotionError("manifest.json record_id does not match the requested record")
    for label, document in (
        ("sources.json", sources_document),
        ("manifest.json", manifest),
    ):
        if document.get("source_fingerprint") != fingerprint:
            raise PromotionError(f"{label} has a stale source fingerprint")
    return fingerprint


def _verify_current_inputs(
    repository_root: Path,
    record_root: Path,
    record_id: str,
    diagnostic_root: Path,
    manifest: Mapping[str, Any],
    sources_document: Mapping[str, Any],
) -> str:
    """Verify current sources and require the current extraction implementation."""

    fingerprint = _verify_current_source_inputs(
        repository_root,
        record_root,
        record_id,
        diagnostic_root,
        manifest,
        sources_document,
    )
    if manifest.get("pipeline_code_sha256") != _pipeline_code_sha256():
        raise PromotionError(
            "the staged candidate was built with stale extraction pipeline code"
        )
    return fingerprint


def _verify_approved_publication_inputs(
    repository_root: Path,
    record_root: Path,
    record_id: str,
    diagnostic_root: Path,
    manifest: Mapping[str, Any],
    sources_document: Mapping[str, Any],
) -> str:
    """Verify immutable publication inputs after the expected status change.

    Public metadata is fingerprinted when a candidate is built, but approval
    intentionally changes ``pip_litdb_status`` afterward. For cleanup, compare
    every publication source and the private override exactly, then validate
    the current public bibliographic metadata and approved status separately.
    """

    current_sources = discover_sources(repository_root, record_id)
    current_rows = tuple(
        row for row in _normalized_source_rows(current_sources) if row[0] != "public_metadata"
    )
    recorded_rows = tuple(
        row
        for row in _recorded_source_rows(sources_document.get("sources"))
        if row[0] != "public_metadata"
    )
    if recorded_rows != current_rows:
        raise PromotionError(
            "the approved publication source inventory no longer matches current files"
        )

    current_override = record_root / "extraction_overrides.yaml"
    approved_override = diagnostic_root / "overrides.yaml"
    current_exists = os.path.lexists(current_override)
    approved_exists = os.path.lexists(approved_override)
    if current_exists:
        reject_reparse_chain(current_override, record_root)
        ensure_within(current_override, record_root)
        if is_reparse_point(current_override) or not current_override.is_file():
            raise PromotionError(f"private extraction override is unsafe: {current_override}")
    if approved_exists and (
        is_reparse_point(approved_override) or not approved_override.is_file()
    ):
        raise PromotionError(f"approved extraction override is unsafe: {approved_override}")
    if current_exists != approved_exists:
        raise PromotionError("the approved override does not match the current private override")
    if current_exists and sha256_file(current_override) != sha256_file(approved_override):
        raise PromotionError("the private extraction override changed after approval")

    if sources_document.get("record_id") != record_id:
        raise PromotionError("sources.json record_id does not match the requested record")
    if manifest.get("record_id") != record_id:
        raise PromotionError("manifest.json record_id does not match the requested record")
    fingerprint = sources_document.get("source_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise PromotionError("sources.json has no valid source fingerprint")
    if manifest.get("source_fingerprint") != fingerprint:
        raise PromotionError("manifest.json and sources.json fingerprints differ")
    return fingerprint


def _validate_stored_result(
    diagnostic_root: Path, report: ValidationReport
) -> bytes:
    validation_path = diagnostic_root / "validation.json"
    try:
        original_bytes = validation_path.read_bytes()
    except OSError as exc:
        raise PromotionError(f"could not read validation.json: {exc}") from exc
    stored = _read_json_object(validation_path, "validation.json")
    expected_findings = [finding.as_dict() for finding in report.findings]
    expected_status = "passed" if report.passed else "failed"
    if (
        stored.get("status") != expected_status
        or stored.get("counts") != dict(report.counts)
        or stored.get("findings") != expected_findings
    ):
        raise PromotionError(
            "validation.json is stale or differs from the current independent validation"
        )
    return original_bytes


def _hard_validation_findings(report: ValidationReport) -> list[str]:
    return sorted(
        {
            finding.code
            for finding in report.findings
            if finding.severity == "critical"
            or finding.code in _INTEGRITY_FINDING_CODES
            or "hash_" in finding.code
            or finding.code.startswith("manifest_")
        }
    )


def _check_acceptances(report: ValidationReport, accepted_findings: Iterable[str]) -> tuple[str, ...]:
    raw = tuple(accepted_findings)
    if any(not isinstance(code, str) or not code for code in raw):
        raise PromotionError("accepted finding codes must be non-empty strings")
    if len(raw) != len(set(raw)):
        raise PromotionError("each finding code may be accepted only once")
    requested = set(raw)
    actual = {finding.code for finding in report.findings}
    missing = sorted(actual - requested)
    unknown = sorted(requested - actual)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"not accepted: {', '.join(missing)}")
        if unknown:
            details.append(f"not present: {', '.join(unknown)}")
        raise PromotionError("finding acceptance must match validation exactly (" + "; ".join(details) + ")")
    return tuple(sorted(actual))


def _reports_match(left: ValidationReport, right: ValidationReport) -> bool:
    return (
        tuple(finding.as_dict() for finding in left.findings)
        == tuple(finding.as_dict() for finding in right.findings)
        and dict(left.counts) == dict(right.counts)
        and dict(left.expected_counts) == dict(right.expected_counts)
        and left.expected_title == right.expected_title
    )


def _remove_temporary_tree(path: Path, record_root: Path) -> None:
    if not os.path.lexists(path):
        return
    ensure_within(path, record_root)
    reject_reparse_chain(path, record_root)
    if is_reparse_point(path):
        raise PromotionError(f"refusing to remove unsafe temporary path: {path}")
    shutil.rmtree(path)


def cleanup_approved_staging(
    repository_root: str | Path,
    record_id: str,
    *,
    remove: bool = True,
) -> StagingCleanupResult:
    """Remove one record's staging history after independently verifying approval.

    This is deliberately separate from promotion. A failed promotion or public
    metadata update must never destroy the reviewed candidate needed to retry.
    """

    try:
        record_id = validate_record_id(record_id)
        repository_root = Path(repository_root).resolve(strict=True)
        private_root = ensure_within(repository_root / "papers (private)", repository_root)
        reject_reparse_chain(private_root, repository_root)
        record_root = ensure_within(private_root / record_id, private_root)
        reject_reparse_chain(record_root, private_root)
        staging_root = ensure_within(
            private_root / "staging", private_root, require_exists=False
        )
        reject_reparse_chain(staging_root, private_root)
        record_staging = ensure_within(
            staging_root / record_id, staging_root, require_exists=False
        )
        reject_reparse_chain(record_staging, staging_root)
    except (FileNotFoundError, OSError, UnsafePathError, ValueError) as exc:
        raise PromotionError(f"unsafe or unavailable cleanup path: {exc}") from exc

    metadata_path = repository_root / "database" / "records" / f"{record_id}.yaml"
    try:
        status = load_record_status(metadata_path)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise PromotionError(f"could not verify public record status: {exc}") from exc
    if status != "extracted_approved":
        raise PromotionError(
            "staging cleanup requires pip_litdb_status: extracted_approved"
        )

    live_extraction = record_root / "extraction"
    live_diagnostic = record_root / "extraction_diagnostic"
    try:
        _validate_tree(live_extraction, record_root, "approved live extraction")
        _validate_tree(live_diagnostic, record_root, "approved live diagnostics")
    except (FileNotFoundError, OSError, UnsafePathError, ValueError) as exc:
        raise PromotionError(f"approved live extraction is unavailable or unsafe: {exc}") from exc

    approval = _read_json_object(live_diagnostic / "approval.json", "approval.json")
    manifest_path = live_diagnostic / "manifest.json"
    manifest = _read_json_object(manifest_path, "manifest.json")
    sources_document = _read_json_object(
        live_diagnostic / "sources.json", "sources.json"
    )
    quality = _read_json_object(live_diagnostic / "quality.json", "quality.json")

    approved_run_id = approval.get("run_id")
    try:
        if not isinstance(approved_run_id, str):
            raise ValueError("approval.json run_id must be a string")
        approved_run_id = validate_run_id(approved_run_id)
    except ValueError as exc:
        raise PromotionError(f"approval.json has an invalid run_id: {exc}") from exc

    if approval.get("status") != "approved" or approval.get("record_id") != record_id:
        raise PromotionError("approval.json does not approve the requested record")
    if approval.get("approved_by") != "user":
        raise PromotionError("approval.json does not record explicit user approval")
    if manifest.get("run_id") != approved_run_id:
        raise PromotionError("approval.json and manifest.json identify different runs")
    if quality.get("record_id") != record_id or quality.get("status") != "approved":
        raise PromotionError("quality.json does not mark the requested record approved")
    if quality.get("approval") != "approval.json":
        raise PromotionError("quality.json does not link to approval.json")

    manifest_hash = sha256_file(manifest_path)
    if approval.get("candidate_manifest_sha256") != manifest_hash:
        raise PromotionError("approval.json does not match the live manifest")

    fingerprint = _verify_approved_publication_inputs(
        repository_root,
        record_root,
        record_id,
        live_diagnostic,
        manifest,
        sources_document,
    )
    if approval.get("source_fingerprint") != fingerprint:
        raise PromotionError("approval.json has a stale source fingerprint")

    metadata = load_record_metadata(metadata_path, record_id)
    report = validate_candidate(
        live_extraction,
        live_diagnostic,
        expected_title=metadata.title,
        expected_metadata=metadata.as_dict(),
    )
    hard_findings = _hard_validation_findings(report)
    if hard_findings:
        raise PromotionError(
            "approved live extraction failed integrity validation: "
            + ", ".join(hard_findings)
        )
    accepted_codes = approval.get("accepted_finding_codes")
    if not isinstance(accepted_codes, list):
        raise PromotionError("approval.json accepted_finding_codes must be an array")
    _check_acceptances(report, accepted_codes)
    stored_validation = _read_json_object(
        live_diagnostic / "validation.json", "validation.json"
    )
    stored_findings = stored_validation.get("findings")
    if not isinstance(stored_findings, list):
        raise PromotionError("validation.json findings must be an array")
    if approval.get("accepted_findings") != stored_findings:
        raise PromotionError("approval.json does not preserve the reviewed findings")
    stored_codes = []
    for finding in stored_findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("code"), str):
            raise PromotionError("validation.json contains a malformed finding")
        stored_codes.append(finding["code"])
    if sorted(set(stored_codes)) != sorted(accepted_codes):
        raise PromotionError("approval.json finding codes differ from validation.json")

    live_extraction_snapshot = _tree_snapshot(live_extraction)
    live_diagnostic_snapshot = _tree_snapshot(live_diagnostic)
    if not os.path.lexists(record_staging):
        return StagingCleanupResult(
            record_id=record_id,
            approved_run_id=approved_run_id,
            staging_root=record_staging,
            removed=False,
            removed_run_ids=(),
            checked_only=not remove,
        )

    _validate_directory_root(record_staging, staging_root, "approved record staging")
    staged_run = record_staging / approved_run_id
    _validate_tree(staged_run, record_staging, "approved staged run")
    staged_extraction = staged_run / "extraction"
    staged_diagnostic = staged_run / "extraction_diagnostic"
    _validate_tree(staged_extraction, staged_run, "approved staged extraction")
    _validate_tree(staged_diagnostic, staged_run, "approved staged diagnostics")
    if sha256_file(staged_diagnostic / "manifest.json") != manifest_hash:
        raise PromotionError("approved staged run does not match the live manifest")
    if _tree_snapshot(staged_extraction) != live_extraction_snapshot:
        raise PromotionError("approved staged extraction differs from the live extraction")
    staged_diagnostic_files = {
        path.relative_to(staged_diagnostic).as_posix(): path
        for path in staged_diagnostic.rglob("*")
        if path.is_file()
    }
    live_diagnostic_files = {
        path.relative_to(live_diagnostic).as_posix(): path
        for path in live_diagnostic.rglob("*")
        if path.is_file()
    }
    expected_live_files = set(staged_diagnostic_files) | {"approval.json"}
    if set(live_diagnostic_files) != expected_live_files:
        raise PromotionError(
            "approved staged and live diagnostics contain different file sets"
        )
    staged_quality = _read_json_object(
        staged_diagnostic / "quality.json", "staged quality.json"
    )
    expected_quality = dict(staged_quality)
    expected_quality.update(
        {
            "status": "approved",
            "validation": "accepted_with_findings" if report.findings else "passed",
            "approval": "approval.json",
            "accepted_finding_codes": list(accepted_codes),
            "accepted_finding_count": len(report.findings),
        }
    )
    if quality != expected_quality:
        raise PromotionError("approved live quality.json is not the promoted form")
    for relative_path, staged_path in staged_diagnostic_files.items():
        if relative_path == "quality.json":
            continue
        if sha256_file(staged_path) != sha256_file(live_diagnostic_files[relative_path]):
            raise PromotionError(
                "approved staged and live diagnostics differ at " + relative_path
            )

    removed_run_ids = tuple(
        sorted(child.name for child in record_staging.iterdir() if child.is_dir())
    )

    if not remove:
        return StagingCleanupResult(
            record_id=record_id,
            approved_run_id=approved_run_id,
            staging_root=record_staging,
            removed=False,
            removed_run_ids=removed_run_ids,
            checked_only=True,
        )

    # Close path-substitution windows immediately before the destructive step.
    _validate_directory_root(record_staging, staging_root, "approved record staging")
    reject_reparse_chain(record_staging, staging_root)
    shutil.rmtree(record_staging)
    if os.path.lexists(record_staging):
        raise PromotionError(f"approved staging directory still exists: {record_staging}")
    if (
        _tree_snapshot(live_extraction) != live_extraction_snapshot
        or _tree_snapshot(live_diagnostic) != live_diagnostic_snapshot
    ):
        raise PromotionError("live approved output changed during staging cleanup")

    return StagingCleanupResult(
        record_id=record_id,
        approved_run_id=approved_run_id,
        staging_root=record_staging,
        removed=True,
        removed_run_ids=removed_run_ids,
    )


def promote_extraction(
    repository_root: str | Path,
    record_id: str,
    *,
    run_id: str,
    accepted_findings: Iterable[str] = (),
    replace: bool = False,
    _rename: RenameFunction = os.replace,
) -> PromotionResult:
    """Promote one staged run after source, artifact, and approval checks.

    The staged run is copied, never moved.  Replacement is deliberately not
    implemented yet; a caller must archive existing live output separately.
    ``_rename`` exists only to permit deterministic failure testing.
    """

    if replace:
        raise PromotionError("replacement promotion is not implemented")
    try:
        record_id = validate_record_id(record_id)
        run_id = validate_run_id(run_id)
        repository_root = Path(repository_root).resolve(strict=True)
        private_root = ensure_within(repository_root / "papers (private)", repository_root)
        reject_reparse_chain(private_root, repository_root)
        record_root = ensure_within(private_root / record_id, private_root)
        reject_reparse_chain(record_root, private_root)
        staging_root = ensure_within(private_root / "staging", private_root)
        reject_reparse_chain(staging_root, private_root)
        candidate_root = ensure_within(staging_root / record_id / run_id, staging_root)
        reject_reparse_chain(candidate_root, staging_root)
    except (FileNotFoundError, OSError, UnsafePathError, ValueError) as exc:
        raise PromotionError(f"unsafe or unavailable promotion path: {exc}") from exc

    candidate_extraction = candidate_root / "extraction"
    candidate_diagnostic = candidate_root / "extraction_diagnostic"
    _validate_tree(candidate_extraction, candidate_root, "candidate extraction")
    _validate_tree(candidate_diagnostic, candidate_root, "candidate diagnostics")

    live_extraction = record_root / "extraction"
    live_diagnostic = record_root / "extraction_diagnostic"
    if os.path.lexists(live_extraction) or os.path.lexists(live_diagnostic):
        raise PromotionError(
            "live extraction or extraction_diagnostic already exists; replacement is refused"
        )

    manifest_path = candidate_diagnostic / "manifest.json"
    manifest = _read_json_object(manifest_path, "manifest.json")
    sources_document = _read_json_object(
        candidate_diagnostic / "sources.json", "sources.json"
    )
    if manifest.get("run_id") != run_id:
        raise PromotionError("manifest.json run_id does not match the requested staged run")

    fingerprint = _verify_current_inputs(
        repository_root,
        record_root,
        record_id,
        candidate_diagnostic,
        manifest,
        sources_document,
    )
    metadata = load_record_metadata(
        repository_root / "database" / "records" / f"{record_id}.yaml", record_id
    )
    report = validate_candidate(
        candidate_extraction,
        candidate_diagnostic,
        expected_title=metadata.title,
        expected_metadata=metadata.as_dict(),
    )
    hard_findings = _hard_validation_findings(report)
    if hard_findings:
        raise PromotionError(
            "candidate failed non-acceptable integrity validation: "
            + ", ".join(hard_findings)
        )
    validation_bytes = _validate_stored_result(candidate_diagnostic, report)
    accepted_codes = _check_acceptances(report, accepted_findings)

    extraction_snapshot = _tree_snapshot(candidate_extraction)
    diagnostic_snapshot = _tree_snapshot(candidate_diagnostic)
    manifest_sha256 = sha256_file(manifest_path)
    token = uuid.uuid4().hex
    temporary_extraction = record_root / f".extraction.promoting-{token}"
    temporary_diagnostic = record_root / f".extraction_diagnostic.promoting-{token}"

    try:
        shutil.copytree(candidate_extraction, temporary_extraction, copy_function=shutil.copy2)
        shutil.copytree(candidate_diagnostic, temporary_diagnostic, copy_function=shutil.copy2)
        _validate_tree(temporary_extraction, record_root, "temporary extraction")
        _validate_tree(temporary_diagnostic, record_root, "temporary diagnostics")
        if _tree_snapshot(temporary_extraction) != extraction_snapshot:
            raise PromotionError("temporary extraction copy differs from the staged candidate")
        if _tree_snapshot(temporary_diagnostic) != diagnostic_snapshot:
            raise PromotionError("temporary diagnostic copy differs from the staged candidate")

        approved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        accepted_rows = [
            finding.as_dict()
            for finding in report.findings
            if finding.code in set(accepted_codes)
        ]
        atomic_write_json(
            temporary_diagnostic / "approval.json",
            {
                "schema_version": "1.0",
                "status": "approved",
                "record_id": record_id,
                "run_id": run_id,
                "approved_by": "user",
                "approved_at": approved_at,
                "source_fingerprint": fingerprint,
                "candidate_manifest_sha256": manifest_sha256,
                "validation_status_before_approval": report.status,
                "accepted_finding_codes": list(accepted_codes),
                "accepted_finding_count": len(report.findings),
                "accepted_findings": accepted_rows,
            },
        )
        quality_path = temporary_diagnostic / "quality.json"
        quality = _read_json_object(quality_path, "quality.json")
        if quality.get("record_id") != record_id:
            raise PromotionError("quality.json record_id does not match the requested record")
        quality.update(
            {
                "status": "approved",
                "validation": "accepted_with_findings" if report.findings else "passed",
                "approval": "approval.json",
                "accepted_finding_codes": list(accepted_codes),
                "accepted_finding_count": len(report.findings),
            }
        )
        atomic_write_json(quality_path, quality)
        if (temporary_diagnostic / "validation.json").read_bytes() != validation_bytes:
            raise PromotionError("validation.json changed while preparing approval")

        copied_report = validate_candidate(
            temporary_extraction,
            temporary_diagnostic,
            expected_title=metadata.title,
            expected_metadata=metadata.as_dict(),
        )
        if not _reports_match(report, copied_report):
            raise PromotionError("the prepared live copy does not reproduce candidate validation")

        # Close time-of-check/time-of-use windows before the two recoverable
        # renames.  The candidate and all current inputs must still be exact.
        _validate_tree(candidate_extraction, candidate_root, "candidate extraction")
        _validate_tree(candidate_diagnostic, candidate_root, "candidate diagnostics")
        if (
            _tree_snapshot(candidate_extraction) != extraction_snapshot
            or _tree_snapshot(candidate_diagnostic) != diagnostic_snapshot
        ):
            raise PromotionError("the staged candidate changed during promotion")
        _verify_current_inputs(
            repository_root,
            record_root,
            record_id,
            candidate_diagnostic,
            manifest,
            sources_document,
        )
        if os.path.lexists(live_extraction) or os.path.lexists(live_diagnostic):
            raise PromotionError("a live destination appeared during promotion")

        try:
            _rename(temporary_extraction, live_extraction)
            _rename(temporary_diagnostic, live_diagnostic)
        except BaseException as exc:
            rollback_errors: list[str] = []
            # If a rename completed before raising, its source is absent.  Move
            # only those known trees back; never disturb an independently
            # created destination that left our temporary source in place.
            if not os.path.lexists(temporary_diagnostic) and os.path.lexists(live_diagnostic):
                try:
                    _rename(live_diagnostic, temporary_diagnostic)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"diagnostic rollback failed: {rollback_exc}")
            if not os.path.lexists(temporary_extraction) and os.path.lexists(live_extraction):
                try:
                    _rename(live_extraction, temporary_extraction)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"extraction rollback failed: {rollback_exc}")
            suffix = ""
            if rollback_errors:
                suffix = "; " + "; ".join(rollback_errors)
            raise PromotionError(f"atomic promotion rename failed: {exc}{suffix}") from exc
    finally:
        _remove_temporary_tree(temporary_extraction, record_root)
        _remove_temporary_tree(temporary_diagnostic, record_root)

    return PromotionResult(
        record_id=record_id,
        run_id=run_id,
        candidate_root=candidate_root,
        extraction_root=live_extraction,
        diagnostic_root=live_diagnostic,
        source_fingerprint=fingerprint,
        accepted_finding_codes=accepted_codes,
        finding_count=len(report.findings),
    )


__all__ = [
    "PromotionError",
    "PromotionResult",
    "StagingCleanupResult",
    "cleanup_approved_staging",
    "promote_extraction",
]
