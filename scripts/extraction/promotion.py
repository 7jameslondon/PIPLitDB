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
from .locking import OWNER_WORKSPACE_MARKER
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

EXPLICIT_USER_APPROVAL = "explicit_user"
STANDING_POLICY_APPROVAL = "standing_policy"
_APPROVAL_MODES = frozenset({EXPLICIT_USER_APPROVAL, STANDING_POLICY_APPROVAL})
_STANDING_POLICY_APPROVER = "primary_agent_under_standing_policy"
_STANDING_POLICY_NAME = "automatic_after_protocol_finalization"
_STANDING_POLICY_VERSION = "1.0"

# This warning is produced because automated PDF/HTML alignment has not been
# implemented.  It may be accepted automatically only after the protocol's
# source audit, five-role review, adjudication, and reproducibility checks have
# all been recorded.  Every other finding still stops standing-policy approval.
_STANDING_POLICY_ACCEPTABLE_FINDINGS = frozenset(
    {"diagnostic_warning_automated_pdf_html_alignment_not_implemented"}
)


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
    approval_mode: str
    accepted_finding_codes: tuple[str, ...]
    finding_count: int
    replaced_run_id: str | None = None
    archived_previous_root: Path | None = None

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
            "approval_mode": self.approval_mode,
            "accepted_finding_codes": list(self.accepted_finding_codes),
            "finding_count": self.finding_count,
            "replaced_run_id": self.replaced_run_id,
            "archived_previous_root": (
                str(self.archived_previous_root)
                if self.archived_previous_root is not None
                else None
            ),
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


def _verified_override_snapshot_hash(
    record_root: Path,
    diagnostic_root: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
) -> str | None:
    """Verify the immutable reviewed override used by a staged or live run.

    Current extraction policy keeps reviewed inputs in the run diagnostics. A
    legacy record-root override may still exist; when it does, it must match
    the diagnostic snapshot exactly. A diagnostic-only override is valid only
    when its hash is explicitly bound into the manifest.
    """

    current_override = record_root / "extraction_overrides.yaml"
    diagnostic_override = diagnostic_root / "overrides.yaml"
    current_exists = os.path.lexists(current_override)
    diagnostic_exists = os.path.lexists(diagnostic_override)

    if current_exists:
        reject_reparse_chain(current_override, record_root)
        ensure_within(current_override, record_root)
        if is_reparse_point(current_override) or not current_override.is_file():
            raise PromotionError(f"private extraction override is unsafe: {current_override}")
    if diagnostic_exists:
        reject_reparse_chain(diagnostic_override, diagnostic_root)
        ensure_within(diagnostic_override, diagnostic_root)
        if is_reparse_point(diagnostic_override) or not diagnostic_override.is_file():
            raise PromotionError(f"{phase} extraction override is unsafe: {diagnostic_override}")

    if current_exists and not diagnostic_exists:
        raise PromotionError(
            f"the {phase} override does not preserve the current private override"
        )

    diagnostic_hash: str | None = None
    if diagnostic_exists:
        diagnostic_hash = sha256_file(diagnostic_override)
        if current_exists and sha256_file(current_override) != diagnostic_hash:
            raise PromotionError(
                f"the private extraction override changed after {phase}"
            )

    declared_hash = manifest.get("override_snapshot_sha256")
    declared_bytes = manifest.get("override_snapshot_bytes")
    if diagnostic_exists:
        if declared_hash is not None and declared_hash != diagnostic_hash:
            raise PromotionError(f"the {phase} override hash differs from the manifest")
        if not current_exists and declared_hash != diagnostic_hash:
            raise PromotionError(
                f"the diagnostic-only {phase} override is not bound to the manifest"
            )
        if declared_bytes is not None and (
            not isinstance(declared_bytes, int)
            or isinstance(declared_bytes, bool)
            or declared_bytes != diagnostic_override.stat().st_size
        ):
            raise PromotionError(f"the {phase} override size differs from the manifest")
    elif declared_hash is not None or declared_bytes is not None:
        raise PromotionError(
            f"the manifest declares a {phase} override snapshot that is missing"
        )

    return diagnostic_hash


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

    override_hash = _verified_override_snapshot_hash(
        record_root,
        diagnostic_root,
        manifest,
        phase="staged",
    )

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

    _verified_override_snapshot_hash(
        record_root,
        diagnostic_root,
        manifest,
        phase="approval",
    )

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


def _standing_policy_acceptances(report: ValidationReport) -> tuple[str, ...]:
    actual = {finding.code for finding in report.findings}
    unexpected = sorted(actual - _STANDING_POLICY_ACCEPTABLE_FINDINGS)
    if unexpected:
        raise PromotionError(
            "standing-policy approval cannot accept new or unresolved findings: "
            + ", ".join(unexpected)
        )
    return tuple(sorted(actual))


def _validate_approval_authority(approval: Mapping[str, Any]) -> str:
    """Return a supported approval mode without rewriting legacy approvals."""

    approved_by = approval.get("approved_by")
    mode = approval.get("approval_mode")
    if approved_by == "user" and mode in (None, EXPLICIT_USER_APPROVAL):
        return EXPLICIT_USER_APPROVAL
    if (
        approved_by == _STANDING_POLICY_APPROVER
        and mode == STANDING_POLICY_APPROVAL
        and approval.get("approval_policy") == _STANDING_POLICY_NAME
        and approval.get("approval_policy_version") == _STANDING_POLICY_VERSION
    ):
        return STANDING_POLICY_APPROVAL
    raise PromotionError("approval.json has unsupported or inconsistent approval authority")


def _review_evidence(
    diagnostic_root: Path, accepted_finding_codes: Iterable[str]
) -> dict[str, Any]:
    reviews_root = diagnostic_root / "reviews"
    try:
        _validate_tree(reviews_root, diagnostic_root, "standing-policy reviews")
    except (FileNotFoundError, OSError, UnsafePathError, ValueError) as exc:
        raise PromotionError(
            f"standing-policy approval requires a safe reviews directory: {exc}"
        ) from exc
    relative_files = sorted(
        path.relative_to(reviews_root).as_posix()
        for path in reviews_root.rglob("*")
        if path.is_file()
    )
    if not relative_files:
        raise PromotionError("standing-policy approval requires recorded review files")

    searchable = [path.casefold().replace("-", "_") for path in relative_files]
    role_checks = {
        "text_and_reading_order": lambda value: "text" in value and "reading" in value,
        "scientific_notation_equations_tables": lambda value: (
            "scientific" in value or "notation" in value or "equation" in value
        ),
        "figures_schemes_supplements": lambda value: (
            "figure" in value or "supplement" in value or "scheme" in value
        ),
        "ai_readiness_consistency": lambda value: (
            ("ai" in value and "readiness" in value) or "machine_readiness" in value
        ),
        "adversarial_completeness": lambda value: (
            "adversarial" in value or "completeness" in value
        ),
    }
    missing_roles = [
        role
        for role, matches in role_checks.items()
        if not any(matches(value) for value in searchable)
    ]
    adjudication_files = [
        relative_files[index]
        for index, value in enumerate(searchable)
        if "adjudicat" in value
    ]
    if missing_roles:
        raise PromotionError(
            "standing-policy approval requires all five review roles; missing: "
            + ", ".join(missing_roles)
        )
    if not adjudication_files:
        raise PromotionError("standing-policy approval requires a review adjudication file")
    adjudication_text = "\n".join(
        (reviews_root / relative_path).read_text(
            encoding="utf-8", errors="strict"
        )
        for relative_path in adjudication_files
    )
    undocumented = sorted(
        code for code in accepted_finding_codes if code not in adjudication_text
    )
    if undocumented:
        raise PromotionError(
            "standing-policy findings are not documented in review adjudication: "
            + ", ".join(undocumented)
        )
    return {
        "review_files": relative_files,
        "review_roles": sorted(role_checks),
        "adjudication_files": adjudication_files,
    }


def _verify_reproducibility_run(
    staging_root: Path,
    record_id: str,
    candidate_run_id: str,
    reproducibility_run_id: str | None,
    candidate_extraction: Path,
    candidate_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if not reproducibility_run_id:
        raise PromotionError(
            "standing-policy approval requires a distinct reproducibility run"
        )
    try:
        reproducibility_run_id = validate_run_id(reproducibility_run_id)
    except ValueError as exc:
        raise PromotionError(f"invalid reproducibility run ID: {exc}") from exc
    if reproducibility_run_id == candidate_run_id:
        raise PromotionError("the reviewed and reproducibility runs must be distinct")

    run_root = ensure_within(
        staging_root / record_id / reproducibility_run_id, staging_root
    )
    reject_reparse_chain(run_root, staging_root)
    extraction_root = run_root / "extraction"
    diagnostic_root = run_root / "extraction_diagnostic"
    _validate_tree(extraction_root, run_root, "reproducibility extraction")
    _validate_tree(diagnostic_root, run_root, "reproducibility diagnostics")
    if _tree_snapshot(extraction_root) != _tree_snapshot(candidate_extraction):
        raise PromotionError(
            "reproducibility run does not exactly reproduce the canonical extraction"
        )

    manifest_path = diagnostic_root / "manifest.json"
    manifest = _read_json_object(manifest_path, "reproducibility manifest.json")
    if manifest.get("run_id") != reproducibility_run_id:
        raise PromotionError("reproducibility manifest identifies a different run")
    comparable_fields = (
        "record_id",
        "source_fingerprint",
        "pipeline_code_sha256",
        "override_snapshot_sha256",
        "override_snapshot_bytes",
        "ocr_performed",
        "files",
        "assets",
    )
    differing = [
        field
        for field in comparable_fields
        if manifest.get(field) != candidate_manifest.get(field)
    ]
    if differing:
        raise PromotionError(
            "reproducibility manifest differs from the reviewed run: "
            + ", ".join(differing)
        )
    return {
        "run_id": reproducibility_run_id,
        "manifest_sha256": sha256_file(manifest_path),
    }


def _existing_live_approval(
    record_root: Path, record_id: str
) -> tuple[str, tuple[tuple[str, str, int, str], ...], tuple[tuple[str, str, int, str], ...]]:
    live_extraction = record_root / "extraction"
    live_diagnostic = record_root / "extraction_diagnostic"
    if not os.path.lexists(live_extraction) or not os.path.lexists(live_diagnostic):
        raise PromotionError(
            "replacement requires both an existing extraction and extraction_diagnostic"
        )
    _validate_tree(live_extraction, record_root, "existing live extraction")
    _validate_tree(live_diagnostic, record_root, "existing live diagnostics")
    approval = _read_json_object(live_diagnostic / "approval.json", "existing approval.json")
    if approval.get("status") != "approved" or approval.get("record_id") != record_id:
        raise PromotionError("replacement target is not an approved extraction")
    _validate_approval_authority(approval)
    old_run_id = approval.get("run_id")
    try:
        if not isinstance(old_run_id, str):
            raise ValueError("run_id must be a string")
        old_run_id = validate_run_id(old_run_id)
    except ValueError as exc:
        raise PromotionError(f"existing approval has an invalid run ID: {exc}") from exc
    manifest_path = live_diagnostic / "manifest.json"
    if approval.get("candidate_manifest_sha256") != sha256_file(manifest_path):
        raise PromotionError("existing approval does not match its live manifest")
    return old_run_id, _tree_snapshot(live_extraction), _tree_snapshot(live_diagnostic)


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


def _remove_validated_approved_staging_tree(path: Path) -> None:
    """Remove validated staging while tolerating disappearing descendants.

    LibreOffice and similar helpers can delete transient cache entries between
    ``rmtree`` enumerating them and attempting to remove them.  Only that
    descendant ``FileNotFoundError`` race is harmless; root disappearance,
    permission failures, and every other removal error must still abort.
    """

    removal_root = path.resolve(strict=True)

    def handle_remove_error(
        function: Callable[..., object],
        failed_path: str | os.PathLike[str],
        exception: BaseException,
    ) -> None:
        del function
        if isinstance(exception, FileNotFoundError):
            try:
                resolved_failed_path = Path(failed_path).resolve(strict=False)
                resolved_failed_path.relative_to(removal_root)
            except (OSError, ValueError):
                pass
            else:
                if resolved_failed_path != removal_root:
                    return
        raise exception

    shutil.rmtree(path, onexc=handle_remove_error)


def _remove_redundant_working_override(
    record_root: Path, diagnostic_root: Path
) -> bool:
    """Remove an approved record's temporary override after exact verification."""

    working_override = ensure_within(
        record_root / "extraction_overrides.yaml", record_root, require_exists=False
    )
    if not os.path.lexists(working_override):
        return False
    reject_reparse_chain(working_override, record_root)
    if is_reparse_point(working_override) or not working_override.is_file():
        raise PromotionError(f"private extraction override is unsafe: {working_override}")

    diagnostic_override = ensure_within(
        diagnostic_root / "overrides.yaml", diagnostic_root
    )
    reject_reparse_chain(diagnostic_override, diagnostic_root)
    if is_reparse_point(diagnostic_override) or not diagnostic_override.is_file():
        raise PromotionError(
            f"approved diagnostic override is unsafe: {diagnostic_override}"
        )
    if sha256_file(working_override) != sha256_file(diagnostic_override):
        raise PromotionError(
            "the temporary private extraction override differs from the approved "
            "diagnostic snapshot"
        )
    try:
        working_override.unlink()
    except OSError as exc:
        raise PromotionError(
            f"could not remove approved temporary extraction override: {exc}"
        ) from exc
    if os.path.lexists(working_override):
        raise PromotionError(
            f"approved temporary extraction override still exists: {working_override}"
        )
    return True


def cleanup_approved_staging(
    repository_root: str | Path,
    record_id: str,
    *,
    remove: bool = True,
) -> StagingCleanupResult:
    """Remove approved staging and a matching temporary working override.

    This is deliberately separate from promotion. A failed promotion or public
    metadata update must never destroy the reviewed candidate needed to retry.
    """

    try:
        record_id = validate_record_id(record_id)
        repository_root = Path(repository_root).resolve(strict=True)
        if os.path.lexists(repository_root / OWNER_WORKSPACE_MARKER):
            raise PromotionError("isolated owners must hand off to the primary repository")
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
    _validate_approval_authority(approval)
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
    if remove:
        _remove_redundant_working_override(record_root, live_diagnostic)
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
    _remove_validated_approved_staging_tree(record_staging)
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
    approval_mode: str = EXPLICIT_USER_APPROVAL,
    reproducibility_run_id: str | None = None,
    replace: bool = False,
    _rename: RenameFunction = os.replace,
) -> PromotionResult:
    """Promote one staged run after source, artifact, and approval checks.

    Explicit user approval remains supported for historical/manual operations.
    Standing-policy approval adds mandatory review and reproducibility evidence
    and may accept only the single policy allow-listed alignment warning.
    ``_rename`` exists only to permit deterministic failure testing.
    """

    if approval_mode not in _APPROVAL_MODES:
        raise PromotionError(f"unsupported approval mode: {approval_mode}")
    requested_acceptances = tuple(accepted_findings)
    try:
        record_id = validate_record_id(record_id)
        run_id = validate_run_id(run_id)
        repository_root = Path(repository_root).resolve(strict=True)
        if os.path.lexists(repository_root / OWNER_WORKSPACE_MARKER):
            raise PromotionError("isolated owners must hand off to the primary repository")
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
    replaced_run_id: str | None = None
    existing_extraction_snapshot: tuple[tuple[str, str, int, str], ...] | None = None
    existing_diagnostic_snapshot: tuple[tuple[str, str, int, str], ...] | None = None
    if replace:
        try:
            current_status = load_record_status(
                repository_root / "database" / "records" / f"{record_id}.yaml"
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise PromotionError(f"could not verify replacement record status: {exc}") from exc
        if current_status != "extracted_approved":
            raise PromotionError(
                "replacement requires pip_litdb_status: extracted_approved"
            )
        (
            replaced_run_id,
            existing_extraction_snapshot,
            existing_diagnostic_snapshot,
        ) = _existing_live_approval(record_root, record_id)
        if replaced_run_id == run_id:
            raise PromotionError("replacement run must differ from the existing approved run")
    elif os.path.lexists(live_extraction) or os.path.lexists(live_diagnostic):
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
    review_evidence: dict[str, Any] | None = None
    reproducibility_evidence: dict[str, Any] | None = None
    if approval_mode == STANDING_POLICY_APPROVAL:
        if requested_acceptances:
            raise PromotionError(
                "standing-policy approval derives accepted findings from its fixed allow-list"
            )
        accepted_codes = _standing_policy_acceptances(report)
        review_evidence = _review_evidence(candidate_diagnostic, accepted_codes)
        reproducibility_evidence = _verify_reproducibility_run(
            staging_root,
            record_id,
            run_id,
            reproducibility_run_id,
            candidate_extraction,
            manifest,
        )
    else:
        if reproducibility_run_id is not None:
            raise PromotionError(
                "reproducibility_run_id is reserved for standing-policy approval"
            )
        accepted_codes = _check_acceptances(report, requested_acceptances)

    extraction_snapshot = _tree_snapshot(candidate_extraction)
    diagnostic_snapshot = _tree_snapshot(candidate_diagnostic)
    manifest_sha256 = sha256_file(manifest_path)
    token = uuid.uuid4().hex
    temporary_extraction = record_root / f".extraction.promoting-{token}"
    temporary_diagnostic = record_root / f".extraction_diagnostic.promoting-{token}"
    archived_previous_root = (
        record_root / "extraction_history" / f"{replaced_run_id}--{token[:12]}"
        if replaced_run_id is not None
        else None
    )

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
        if approval_mode == STANDING_POLICY_APPROVAL:
            approved_by = _STANDING_POLICY_APPROVER
            approval_policy = _STANDING_POLICY_NAME
            approval_policy_version = _STANDING_POLICY_VERSION
        else:
            approved_by = "user"
            approval_policy = "explicit_user_authorization"
            approval_policy_version = "1.0"
        finalization_evidence: dict[str, Any] | None = None
        if review_evidence is not None and reproducibility_evidence is not None:
            finalization_evidence = {
                **review_evidence,
                "reproducibility": reproducibility_evidence,
            }
        replacement: dict[str, Any] | None = None
        if replaced_run_id is not None and archived_previous_root is not None:
            replacement = {
                "replaced_run_id": replaced_run_id,
                "archived_previous_root": archived_previous_root.relative_to(
                    record_root
                ).as_posix(),
            }
        atomic_write_json(
            temporary_diagnostic / "approval.json",
            {
                "schema_version": "1.0",
                "status": "approved",
                "record_id": record_id,
                "run_id": run_id,
                "approved_by": approved_by,
                "approval_mode": approval_mode,
                "approval_policy": approval_policy,
                "approval_policy_version": approval_policy_version,
                "approved_at": approved_at,
                "source_fingerprint": fingerprint,
                "candidate_manifest_sha256": manifest_sha256,
                "validation_status_before_approval": report.status,
                "accepted_finding_codes": list(accepted_codes),
                "accepted_finding_count": len(report.findings),
                "accepted_findings": accepted_rows,
                **(
                    {"finalization_evidence": finalization_evidence}
                    if finalization_evidence is not None
                    else {}
                ),
                **({"replacement": replacement} if replacement is not None else {}),
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

        # Close time-of-check/time-of-use windows before the recoverable
        # renames. The candidate and all current inputs must still be exact.
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
        if replace:
            if (
                replaced_run_id is None
                or existing_extraction_snapshot is None
                or existing_diagnostic_snapshot is None
                or archived_previous_root is None
            ):
                raise PromotionError("replacement state was not initialized safely")
            current_run_id, current_extraction, current_diagnostic = (
                _existing_live_approval(record_root, record_id)
            )
            if (
                current_run_id != replaced_run_id
                or current_extraction != existing_extraction_snapshot
                or current_diagnostic != existing_diagnostic_snapshot
            ):
                raise PromotionError("the existing live extraction changed during replacement")
            history_root = archived_previous_root.parent
            history_root.mkdir(exist_ok=True)
            _validate_directory_root(history_root, record_root, "extraction history")
            if os.path.lexists(archived_previous_root):
                raise PromotionError("replacement archive destination already exists")
            archived_previous_root.mkdir()
            try:
                _rename(live_extraction, archived_previous_root / "extraction")
                _rename(
                    live_diagnostic,
                    archived_previous_root / "extraction_diagnostic",
                )
                _rename(temporary_extraction, live_extraction)
                _rename(temporary_diagnostic, live_diagnostic)
            except BaseException as exc:
                rollback_errors: list[str] = []
                if (
                    not os.path.lexists(temporary_diagnostic)
                    and os.path.lexists(live_diagnostic)
                ):
                    try:
                        _rename(live_diagnostic, temporary_diagnostic)
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            f"new diagnostic rollback failed: {rollback_exc}"
                        )
                if (
                    not os.path.lexists(temporary_extraction)
                    and os.path.lexists(live_extraction)
                ):
                    try:
                        _rename(live_extraction, temporary_extraction)
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            f"new extraction rollback failed: {rollback_exc}"
                        )
                archived_diagnostic = archived_previous_root / "extraction_diagnostic"
                if not os.path.lexists(live_diagnostic) and os.path.lexists(
                    archived_diagnostic
                ):
                    try:
                        _rename(archived_diagnostic, live_diagnostic)
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            f"old diagnostic rollback failed: {rollback_exc}"
                        )
                archived_extraction = archived_previous_root / "extraction"
                if not os.path.lexists(live_extraction) and os.path.lexists(
                    archived_extraction
                ):
                    try:
                        _rename(archived_extraction, live_extraction)
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            f"old extraction rollback failed: {rollback_exc}"
                        )
                if not any(archived_previous_root.iterdir()):
                    archived_previous_root.rmdir()
                suffix = "; " + "; ".join(rollback_errors) if rollback_errors else ""
                raise PromotionError(
                    f"atomic replacement rename failed: {exc}{suffix}"
                ) from exc
            if (
                _tree_snapshot(archived_previous_root / "extraction")
                != existing_extraction_snapshot
                or _tree_snapshot(
                    archived_previous_root / "extraction_diagnostic"
                )
                != existing_diagnostic_snapshot
            ):
                raise PromotionError("archived prior extraction changed during replacement")
        else:
            if os.path.lexists(live_extraction) or os.path.lexists(live_diagnostic):
                raise PromotionError("a live destination appeared during promotion")
            try:
                _rename(temporary_extraction, live_extraction)
                _rename(temporary_diagnostic, live_diagnostic)
            except BaseException as exc:
                rollback_errors = []
                # If a rename completed before raising, its source is absent.
                if (
                    not os.path.lexists(temporary_diagnostic)
                    and os.path.lexists(live_diagnostic)
                ):
                    try:
                        _rename(live_diagnostic, temporary_diagnostic)
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            f"diagnostic rollback failed: {rollback_exc}"
                        )
                if (
                    not os.path.lexists(temporary_extraction)
                    and os.path.lexists(live_extraction)
                ):
                    try:
                        _rename(live_extraction, temporary_extraction)
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            f"extraction rollback failed: {rollback_exc}"
                        )
                suffix = "; " + "; ".join(rollback_errors) if rollback_errors else ""
                raise PromotionError(
                    f"atomic promotion rename failed: {exc}{suffix}"
                ) from exc
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
        approval_mode=approval_mode,
        accepted_finding_codes=accepted_codes,
        finding_count=len(report.findings),
        replaced_run_id=replaced_run_id,
        archived_previous_root=archived_previous_root,
    )


__all__ = [
    "PromotionError",
    "PromotionResult",
    "StagingCleanupResult",
    "EXPLICIT_USER_APPROVAL",
    "STANDING_POLICY_APPROVAL",
    "cleanup_approved_staging",
    "promote_extraction",
]
