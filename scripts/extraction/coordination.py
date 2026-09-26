"""Frozen private workspaces and guarded handoffs for up to three paper owners."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .discovery import discover_sources
from .locking import OWNER_WORKSPACE_MARKER, repository_lock
from .metadata import load_record_status
from .paths import (
    atomic_write_bytes, atomic_write_json, ensure_within, reject_reparse_chain,
    sha256_file, validate_record_id, validate_run_id,
)
from .promotion import (
    _read_json_object, _tree_snapshot, _validate_tree, _verify_current_inputs,
    cleanup_approved_staging,
)


class CoordinationError(RuntimeError):
    """A workspace or handoff cannot safely use the frozen batch inputs."""


_RUNTIME_TREES = ("scripts", "tests", "database", "UI", ".github")
_RUNTIME_FILES = (
    "AGENTS.md", "README.md", "EXTRACTION_PROTOCOL.md", "EXTRACTION_PLAN.MD",
    "DOWNLOAD.md", "requirements.txt", "extraction_viewer.html", "LICENSE",
    ".gitignore", "audit_sources.py", "audit_html_summary.py",
)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _safe(path: Path, boundary: Path, *, exists: bool = True) -> Path:
    reject_reparse_chain(path, boundary)
    return ensure_within(path, boundary, require_exists=exists)


def _files(root: Path, directory: Path) -> dict[str, Any]:
    if not os.path.lexists(directory):
        return {}
    _validate_tree(directory, root, "workspace input")
    return {
        path.relative_to(root).as_posix(): _file(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _runtime(root: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for name in _RUNTIME_TREES:
        files.update(_files(root, root / name))
    # Another record's ordinary finalization must not invalidate this owner.
    files = {name: value for name, value in files.items()
             if not name.startswith("database/records/")}
    for name in _RUNTIME_FILES:
        if os.path.lexists(root / name):
            files[name] = _file(_safe(root / name, root))
    return files


def _record_files(root: Path, record_id: str) -> dict[str, Any]:
    files = _files(root, root / "papers (private)" / record_id)
    files.pop(f"papers (private)/{record_id}/extraction_overrides.yaml", None)
    metadata = f"database/records/{record_id}.yaml"
    files[metadata] = _file(_safe(root / metadata, root))
    return files


def _override(root: Path, record_id: str) -> dict[str, Any] | None:
    path = root / "papers (private)" / record_id / "extraction_overrides.yaml"
    return _file(_safe(path, root)) if os.path.lexists(path) else None


def _copy_files(source: Path, destination: Path, files: dict[str, Any]) -> None:
    for name, expected in files.items():
        original = _safe(source / name, source)
        target = _safe(destination / name, destination, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(original.read_bytes())
        if _file(target) != expected:
            raise CoordinationError(f"input changed during workspace copy: {name}")


def _locations(root: Path, batch_id: str) -> tuple[Path, Path]:
    validate_run_id(batch_id)
    private = _safe(root / "papers (private)", root)
    parent = _safe(private / "parallel", private, exists=False)
    return parent / batch_id, parent / "active.json"


def _load_batch(root: Path, batch_id: str) -> tuple[Path, dict[str, Any]]:
    batch, active_path = _locations(root, batch_id)
    _safe(batch, root)
    active = _read_json_object(_safe(active_path, root), "active batch")
    manifest_path = _safe(batch / "batch.json", batch)
    if active.get("batch_id") != batch_id or active.get("manifest_sha256") != sha256_file(manifest_path):
        raise CoordinationError("batch is not the current unchanged active assignment")
    manifest = _read_json_object(manifest_path, "batch manifest")
    if manifest.get("repository_root") != str(root):
        raise CoordinationError("batch belongs to another repository")
    return batch, manifest


def prepare_batch(repository_root: Path, batch_id: str, record_ids: list[str]) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    ids = [validate_record_id(value) for value in record_ids]
    if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids):
        raise CoordinationError("assign one to three distinct records, with one owner each")
    if os.path.lexists(root / OWNER_WORKSPACE_MARKER):
        raise CoordinationError("only the primary repository can prepare owner workspaces")
    with repository_lock(root):
        batch, active_path = _locations(root, batch_id)
        if os.path.lexists(active_path):
            raise CoordinationError("an active batch already owns the extraction slots")
        for record_id in ids:
            if load_record_status(root / "database/records" / f"{record_id}.yaml") != "ready_for_extraction":
                raise CoordinationError(f"record {record_id} is not ready for extraction")
            if os.path.lexists(root / "papers (private)" / record_id / "extraction"):
                raise CoordinationError(f"record {record_id} already has live output")
            discover_sources(root, record_id)
        runtime = _runtime(root)
        metadata = _files(root, root / "database/records")
        records = {record_id: _record_files(root, record_id) for record_id in ids}
        overrides = {record_id: _override(root, record_id) for record_id in ids}
        batch.parent.mkdir(exist_ok=True)
        batch.mkdir()
        owners = {}
        for record_id in ids:
            workspace = batch / record_id
            workspace.mkdir()
            protected = {**runtime, **metadata, **records[record_id]}
            _copy_files(root, workspace, protected)
            override_name = f"papers (private)/{record_id}/extraction_overrides.yaml"
            if overrides[record_id] is not None:
                _copy_files(root, workspace, {override_name: overrides[record_id]})
            marker = {"schema_version": "1.0", "batch_id": batch_id,
                      "record_id": record_id, "primary_repository": str(root)}
            atomic_write_json(workspace / OWNER_WORKSPACE_MARKER, marker)
            protected[OWNER_WORKSPACE_MARKER] = _file(workspace / OWNER_WORKSPACE_MARKER)
            owners[record_id] = {"protected_files": protected}
        if _runtime(root) != runtime or _files(root, root / "database/records") != metadata:
            raise CoordinationError("shared inputs changed while preparing the batch")
        for record_id in ids:
            if _record_files(root, record_id) != records[record_id] or _override(root, record_id) != overrides[record_id]:
                raise CoordinationError("record inputs changed while preparing the batch")
        manifest = {"schema_version": "1.0", "created_utc": _stamp(),
                    "batch_id": batch_id, "repository_root": str(root), "records": ids,
                    "owner_model": "gpt-6-astra", "owner_effort": "high",
                    "review_mode": "five_sequential_owner_self_reviews_and_separate_adjudication",
                    "runtime_files": runtime, "record_files": records,
                    "original_overrides": overrides, "owners": owners}
        atomic_write_json(batch / "batch.json", manifest)
        atomic_write_json(active_path, {"batch_id": batch_id,
                                       "manifest_sha256": sha256_file(batch / "batch.json")})
        return {"status": "prepared", "batch_id": batch_id,
                "workspaces": {record_id: str(batch / record_id) for record_id in ids},
                "runtime_files": len(runtime)}


def _check_owner(root: Path, batch: Path, manifest: dict[str, Any], record_id: str) -> Path:
    if record_id not in manifest["records"]:
        raise CoordinationError("record is not assigned to this batch")
    workspace = _safe(batch / record_id, batch)
    if _runtime(root) != manifest["runtime_files"] or _runtime(workspace) != manifest["runtime_files"]:
        raise CoordinationError("shared code, schema, tests or protocol changed; stop owners and rebase the batch")
    for name, expected in manifest["owners"][record_id]["protected_files"].items():
        path = _safe(workspace / name, workspace)
        if _file(path) != expected:
            raise CoordinationError(f"owner changed a protected input: {name}")
    expected_records = manifest["record_files"][record_id]
    if _record_files(root, record_id) != expected_records or _record_files(workspace, record_id) != expected_records:
        raise CoordinationError("record sources, history or metadata changed after assignment")
    if _override(root, record_id) != manifest["original_overrides"][record_id]:
        raise CoordinationError("primary private override changed during owner work")
    return workspace


def check_owner(repository_root: Path, batch_id: str, record_id: str) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    with repository_lock(root):
        batch, manifest = _load_batch(root, batch_id)
        workspace = _check_owner(root, batch, manifest, validate_record_id(record_id))
        return {"status": "inputs_unchanged", "workspace": str(workspace), "record_id": record_id}


def import_owner_runs(repository_root: Path, batch_id: str, record_id: str,
                      run_id: str, reproducibility_run_id: str) -> dict[str, Any]:
    """Copy exact frozen runs into primary staging; this never approves them."""
    root = repository_root.resolve(strict=True)
    validate_record_id(record_id)
    runs = [validate_run_id(run_id), validate_run_id(reproducibility_run_id)]
    if runs[0] == runs[1]:
        raise CoordinationError("handoff requires a distinct clean reproduction")
    with repository_lock(root):
        batch, manifest = _load_batch(root, batch_id)
        workspace = _check_owner(root, batch, manifest, record_id)
        receipt_path = batch / f"import-{record_id}.json"
        if receipt_path.exists():
            raise CoordinationError("owner handoff has already been imported")
        owner_override = _override(workspace, record_id)
        override_path = workspace / "papers (private)" / record_id / "extraction_overrides.yaml"
        override_bytes = override_path.read_bytes() if owner_override is not None else None
        if owner_override is None and manifest["original_overrides"][record_id] is not None:
            raise CoordinationError("removing an existing override requires primary reconciliation")
        snapshots = {}
        canonical = []
        for name in runs:
            source_run = _safe(workspace / "papers (private)/staging" / record_id / name, workspace)
            _validate_tree(source_run, workspace, "owner candidate")
            diagnostic = source_run / "extraction_diagnostic"
            candidate_manifest = _read_json_object(diagnostic / "manifest.json", "owner manifest")
            if candidate_manifest.get("record_id") != record_id or candidate_manifest.get("run_id") != name:
                raise CoordinationError("owner manifest does not identify the assigned run")
            _verify_current_inputs(workspace, workspace / "papers (private)" / record_id,
                                   record_id, diagnostic, candidate_manifest,
                                   _read_json_object(diagnostic / "sources.json", "owner sources"))
            snapshots[name] = _tree_snapshot(source_run)
            canonical.append(_tree_snapshot(source_run / "extraction"))
        if canonical[0] != canonical[1]:
            raise CoordinationError("clean reproduction differs from the reviewed canonical extraction")
        destination_parent = _safe(root / "papers (private)/staging" / record_id, root, exists=False)
        destination_parent.mkdir(parents=True, exist_ok=True)
        for name in runs:
            source_run = workspace / "papers (private)/staging" / record_id / name
            destination = _safe(destination_parent / name, root, exists=False)
            if destination.exists():
                _validate_tree(destination, root, "existing handoff destination")
                if _tree_snapshot(destination) != snapshots[name]:
                    raise CoordinationError("handoff would overwrite an existing staged run")
            else:
                shutil.copytree(source_run, destination)
            _validate_tree(destination, root, "imported candidate")
            if _tree_snapshot(destination) != snapshots[name] or _tree_snapshot(source_run) != snapshots[name]:
                raise CoordinationError("candidate changed during handoff")
        _check_owner(root, batch, manifest, record_id)
        if _override(workspace, record_id) != owner_override:
            raise CoordinationError("owner override changed during handoff")
        if override_bytes is not None:
            atomic_write_bytes(root / "papers (private)" / record_id / override_path.name, override_bytes)
        receipt = {"status": "staged_for_primary_review", "imported_utc": _stamp(),
                   "record_id": record_id, "batch_id": batch_id, "run_id": run_id,
                   "reproducibility_run_id": reproducibility_run_id,
                   "workspace": str(workspace), "run_snapshots": snapshots}
        atomic_write_json(receipt_path, receipt)
        return {key: value for key, value in receipt.items() if key != "run_snapshots"}


def close_batch(repository_root: Path, batch_id: str, *, abort_reason: str | None = None) -> dict[str, Any]:
    """Release slots after approval, or after the primary has stopped all owners."""
    root = repository_root.resolve(strict=True)
    with repository_lock(root):
        batch, manifest = _load_batch(root, batch_id)
        if abort_reason is None:
            for record_id in manifest["records"]:
                imported = _read_json_object(batch / f"import-{record_id}.json", "owner import")
                approval = _read_json_object(root / "papers (private)" / record_id / "extraction_diagnostic/approval.json", "live approval")
                if approval.get("run_id") != imported.get("run_id"):
                    raise CoordinationError("live approval does not match the imported owner run")
                cleanup_approved_staging(root, record_id, remove=False)
            status = "completed"
        else:
            if not abort_reason.strip():
                raise CoordinationError("aborting a batch requires a recorded reason and stopped owners")
            status = "aborted"
        result = {"status": status, "batch_id": batch_id, "records": manifest["records"],
                  "closed_utc": _stamp(), "reason": abort_reason}
        atomic_write_json(batch / f"{status}.json", result)
        _safe(batch.parent / "active.json", root).unlink()
        return result
