"""Guarded standing-policy finalization for one extraction record."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

from .paths import atomic_write_bytes, validate_record_id
from .locking import OWNER_WORKSPACE_MARKER, repository_lock
from .promotion import (
    PromotionError,
    PromotionResult,
    STANDING_POLICY_APPROVAL,
    cleanup_approved_staging,
    promote_extraction,
)


_APPROVED_STATUS = "extracted_approved"
_READY_STATUS = "ready_for_extraction"
_QUEUE_APPROVED_TEXT = "Extraction completed and approved."


@dataclass(frozen=True)
class FinalizationResult:
    promotion: PromotionResult
    metadata_changed: bool
    queue_changed: bool
    staging_removed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": "approved",
            "approval_mode": STANDING_POLICY_APPROVAL,
            "record_id": self.promotion.record_id,
            "run_id": self.promotion.run_id,
            "metadata_changed": self.metadata_changed,
            "queue_changed": self.queue_changed,
            "staging_removed": self.staging_removed,
            "promotion": self.promotion.as_dict(),
        }


def _planned_metadata_update(path: Path, *, replace: bool) -> tuple[bytes, bool]:
    try:
        original = path.read_bytes()
        text = original.decode("utf-8", errors="strict")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise PromotionError(f"could not prepare public status update: {exc}") from exc
    lines = text.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if line.startswith("pip_litdb_status:")
    ]
    if len(matches) != 1:
        raise PromotionError("public metadata must contain exactly one top-level status")
    line_index = matches[0]
    line = lines[line_index]
    content = line.rstrip("\r\n")
    newline = line[len(content) :]
    value_and_comment = content.partition(":")[2]
    raw_value, marker, comment = value_and_comment.partition("#")
    current = raw_value.strip().strip("'\"")
    allowed = {_APPROVED_STATUS} if replace else {_READY_STATUS}
    if current == _APPROVED_STATUS:
        if replace:
            return original, False
        raise PromotionError("new finalization found an already-approved public record")
    if current not in allowed:
        raise PromotionError(
            f"standing-policy finalization cannot change unexpected status: {current}"
        )
    suffix = f" #{comment}" if marker else ""
    lines[line_index] = f"pip_litdb_status: {_APPROVED_STATUS}{suffix}{newline}"
    updated = "".join(lines).encode("utf-8")
    return updated, updated != original


def _planned_queue_update(path: Path, record_id: str) -> tuple[bytes, bool]:
    try:
        original = path.read_bytes()
        text = original.decode("utf-8", errors="strict")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise PromotionError(f"could not prepare extraction queue update: {exc}") from exc
    pattern = re.compile(
        rf"^(\d+\. \*\*`{re.escape(record_id)}`\*\* — )(.*)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise PromotionError("extraction queue must contain the record exactly once")
    if matches[0].group(2).strip() == _QUEUE_APPROVED_TEXT:
        return original, False
    updated = pattern.sub(
        lambda match: f"{match.group(1)}{_QUEUE_APPROVED_TEXT}", text, count=1
    ).encode("utf-8")
    return updated, updated != original


def _finalize_extraction_locked(
    repository_root: str | Path,
    record_id: str,
    *,
    run_id: str,
    reproducibility_run_id: str,
    replace: bool = False,
    remove_staging: bool = True,
) -> FinalizationResult:
    """Approve, publish, mark, and optionally clean one finalized candidate."""

    try:
        record_id = validate_record_id(record_id)
        repository_root = Path(repository_root).resolve(strict=True)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise PromotionError(f"unsafe or unavailable finalization path: {exc}") from exc

    metadata_path = repository_root / "database" / "records" / f"{record_id}.yaml"
    queue_path = repository_root / "tmp" / "EXTRACTION_QUEUE.md"
    metadata_bytes, metadata_changed = _planned_metadata_update(
        metadata_path, replace=replace
    )
    queue_bytes, queue_changed = _planned_queue_update(queue_path, record_id)

    promotion = promote_extraction(
        repository_root,
        record_id,
        run_id=run_id,
        approval_mode=STANDING_POLICY_APPROVAL,
        reproducibility_run_id=reproducibility_run_id,
        replace=replace,
    )

    if metadata_changed:
        atomic_write_bytes(metadata_path, metadata_bytes)
    if queue_changed:
        atomic_write_bytes(queue_path, queue_bytes)

    # The cleanup check independently revalidates the live artifact, approval
    # authority, current public status, sources, and exact staged/live match.
    cleanup_approved_staging(repository_root, record_id, remove=False)
    cleanup = cleanup_approved_staging(
        repository_root, record_id, remove=remove_staging
    )
    return FinalizationResult(
        promotion=promotion,
        metadata_changed=metadata_changed,
        queue_changed=queue_changed,
        staging_removed=cleanup.removed,
    )


def finalize_extraction(
    repository_root: str | Path,
    record_id: str,
    *,
    run_id: str,
    reproducibility_run_id: str,
    replace: bool = False,
    remove_staging: bool = True,
) -> FinalizationResult:
    """Finalize with exclusive access to shared metadata and the queue."""
    root = Path(repository_root).resolve(strict=True)
    if os.path.lexists(root / OWNER_WORKSPACE_MARKER):
        raise PromotionError("isolated owners must hand off to the primary repository")
    with repository_lock(root):
        return _finalize_extraction_locked(
            root, record_id, run_id=run_id,
            reproducibility_run_id=reproducibility_run_id,
            replace=replace, remove_staging=remove_staging,
        )


__all__ = ["FinalizationResult", "finalize_extraction"]
