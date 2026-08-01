"""Safe canonical YAML generation and history-aware ID allocation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

import yaml

try:
    from scripts.validate_metadata import ValidationReport, validate_repository
except ModuleNotFoundError:  # Running ``python scripts/discover_papers.py``.
    from validate_metadata import ValidationReport, validate_repository

from .deduplicate import DatabaseIndex
from .models import Candidate
from .normalize import normalize_doi


RECORD_PATH_RE = re.compile(r"(?:^|/)database/records/([0-9]{5})\.yaml$")
FIELD_ORDER = (
    "document_type",
    "publication_stage",
    "title",
    "authors",
    "doi",
    "url",
    "publication_year",
    "journal",
    "related_papers",
    "pip_litdb_status",
    "pip_litdb_notes",
)


class QuotedStringDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


class QuotedString(str):
    pass


def _quoted_string(dumper: yaml.Dumper, value: QuotedString) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')


QuotedStringDumper.add_representer(QuotedString, _quoted_string)


def _git(root: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", *arguments],
        cwd=root,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def highest_historical_id(root: Path, base: str = "HEAD", reserved_ids: Iterable[str] = ()) -> int:
    result = _git(root, "log", "--format=", "--name-only", base, "--", "database/records")
    if result.returncode != 0:
        raise RuntimeError(f"cannot inspect record history: {result.stderr.strip()}")
    values = {int(value) for value in reserved_ids if str(value).isdigit()}
    for line in result.stdout.splitlines():
        match = RECORD_PATH_RE.search(line.replace("\\", "/"))
        if match:
            values.add(int(match.group(1)))
    for path in (root / "database" / "records").glob("[0-9][0-9][0-9][0-9][0-9].yaml"):
        values.add(int(path.stem))
    return max(values, default=0)


def candidate_record(candidate: Candidate) -> dict[str, Any]:
    if not candidate.eligible_for_record:
        raise ValueError(f"candidate {candidate.stable_identifier} lacks schema-required metadata")
    record: dict[str, Any] = {
        "document_type": candidate.document_type,
        "publication_stage": candidate.publication_stage,
        "title": candidate.title,
        "authors": [{"name": author.name} for author in candidate.authors],
        "publication_year": candidate.record_year,
        "journal": candidate.journal,
        "pip_litdb_status": "needs_review",
    }
    if candidate.doi:
        record["doi"] = candidate.doi
    if candidate.url:
        record["url"] = candidate.url
    return {key: record[key] for key in FIELD_ORDER if key in record}


def _ordered_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in FIELD_ORDER if key in record}


def serialize_record(record: dict[str, Any]) -> str:
    def quote_values(value: Any) -> Any:
        if isinstance(value, str):
            return QuotedString(value)
        if isinstance(value, list):
            return [quote_values(item) for item in value]
        if isinstance(value, dict):
            return {key: quote_values(item) for key, item in value.items()}
        return value

    return yaml.dump(
        quote_values(record),
        Dumper=QuotedStringDumper,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
        default_flow_style=False,
    )


def allocate_records(
    candidates: Iterable[Candidate],
    *,
    start_after: int,
) -> list[tuple[str, Candidate, str]]:
    eligible = [item for item in candidates if item.disposition in {"new", "related_version"} and item.eligible_for_record]
    eligible.sort(key=lambda item: (item.doi or "", item.stable_identifier))
    allocated: list[tuple[str, Candidate, str]] = []
    for offset, candidate in enumerate(eligible, start=1):
        numeric = start_after + offset
        if numeric > 99_999:
            raise ValueError("five-digit PIP LitDB ID space is exhausted")
        record_id = f"{numeric:05d}"
        allocated.append((record_id, candidate, serialize_record(candidate_record(candidate))))
    return allocated


def _append_relationship(record: dict[str, Any], target_id: str, relationship_type: str) -> None:
    relationships = record.setdefault("related_papers", [])
    value = {"pip_litdb_id": target_id, "relationship_type": relationship_type}
    if value not in relationships:
        relationships.append(value)
        relationships.sort(key=lambda item: (item["pip_litdb_id"], item["relationship_type"]))


def _relationship_pair(source_stage: str | None, target_stage: str | None) -> tuple[str, str]:
    if source_stage == "preprint" and target_stage == "publication":
        return "is_preprint_of", "has_preprint"
    if source_stage == "publication" and target_stage == "preprint":
        return "has_preprint", "is_preprint_of"
    return "is_version_of", "is_version_of"


def _prepare_relationship_changes(
    root: Path,
    allocated: list[tuple[str, Candidate, str]],
) -> tuple[list[tuple[str, Candidate, str]], dict[Path, str]]:
    new_records = {record_id: candidate_record(candidate) for record_id, candidate, _ in allocated}
    new_by_doi = {
        candidate.doi: (record_id, candidate.publication_stage)
        for record_id, candidate, _ in allocated
        if candidate.doi
    }
    existing_by_doi: dict[str, tuple[str, str | None, Path, dict[str, Any]]] = {}
    for path in sorted((root / "database" / "records").glob("[0-9][0-9][0-9][0-9][0-9].yaml")):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            continue
        doi = normalize_doi(value.get("doi"))
        if doi:
            existing_by_doi[doi] = (path.stem, value.get("publication_stage"), path, value)
    updates: dict[Path, dict[str, Any]] = {}
    for record_id, candidate, _ in allocated:
        for relationship in candidate.relationships:
            related_doi = normalize_doi(relationship.get("doi"))
            if not related_doi or related_doi == candidate.doi:
                continue
            if related_doi in new_by_doi:
                target_id, target_stage = new_by_doi[related_doi]
                if target_id == record_id:
                    continue
                forward, inverse = _relationship_pair(candidate.publication_stage, target_stage)
                _append_relationship(new_records[record_id], target_id, forward)
                _append_relationship(new_records[target_id], record_id, inverse)
            elif related_doi in existing_by_doi:
                target_id, target_stage, path, existing = existing_by_doi[related_doi]
                forward, inverse = _relationship_pair(candidate.publication_stage, target_stage)
                _append_relationship(new_records[record_id], target_id, forward)
                target_record = updates.setdefault(path, dict(existing))
                _append_relationship(target_record, record_id, inverse)
    prepared = [
        (record_id, candidate, serialize_record(_ordered_record(new_records[record_id])))
        for record_id, candidate, _ in allocated
    ]
    serialized_updates = {
        path: serialize_record(_ordered_record(record)) for path, record in updates.items()
    }
    return prepared, serialized_updates


def _temporary_tree_validation(
    root: Path,
    allocated: list[tuple[str, Candidate, str]],
    updates: dict[Path, str],
) -> ValidationReport:
    with tempfile.TemporaryDirectory(prefix="pip-litdb-discovery-") as directory:
        temporary_root = Path(directory)
        shutil.copytree(root / "database", temporary_root / "database")
        for record_id, _, content in allocated:
            (temporary_root / "database" / "records" / f"{record_id}.yaml").write_text(
                content, encoding="utf-8", newline="\n"
            )
        for path, content in updates.items():
            destination = temporary_root / path.relative_to(root)
            destination.write_text(content, encoding="utf-8", newline="\n")
        return validate_repository(temporary_root)


def _ephemeral_commit(root: Path, base: str, paths: list[Path]) -> str:
    base_result = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    if base_result.returncode != 0:
        raise RuntimeError(f"cannot resolve validation base {base!r}: {base_result.stderr.strip()}")
    base_sha = base_result.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="pip-litdb-index-") as directory:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_INDEX_FILE": str(Path(directory) / "index"),
                "GIT_AUTHOR_NAME": "PIP LitDB Discovery",
                "GIT_AUTHOR_EMAIL": "noreply@pip-litdb.org",
                "GIT_COMMITTER_NAME": "PIP LitDB Discovery",
                "GIT_COMMITTER_EMAIL": "noreply@pip-litdb.org",
            }
        )
        read_tree = _git(root, "read-tree", base_sha, env=environment)
        if read_tree.returncode != 0:
            raise RuntimeError(read_tree.stderr.strip())
        for path in paths:
            relative = path.relative_to(root).as_posix()
            blob = _git(root, "hash-object", "-w", str(path), env=environment)
            if blob.returncode != 0:
                raise RuntimeError(blob.stderr.strip())
            update = _git(
                root,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob.stdout.strip(),
                relative,
                env=environment,
            )
            if update.returncode != 0:
                raise RuntimeError(update.stderr.strip())
        tree = _git(root, "write-tree", env=environment)
        if tree.returncode != 0:
            raise RuntimeError(tree.stderr.strip())
        commit = _git(
            root,
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            base_sha,
            "-m",
            "Validate generated discovery records",
            env=environment,
        )
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.strip())
        return commit.stdout.strip()


def write_records(
    root: Path,
    candidates: Iterable[Candidate],
    *,
    base: str = "HEAD",
    reserved_ids: Iterable[str] = (),
) -> dict[str, str]:
    dirty = _git(root, "status", "--porcelain", "--", "database/records")
    if dirty.returncode != 0:
        raise RuntimeError(f"cannot inspect database worktree state: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise RuntimeError("refusing to generate records while database/records has existing changes")
    start_after = highest_historical_id(root, base, reserved_ids)
    allocated = allocate_records(candidates, start_after=start_after)
    if not allocated:
        return {}
    allocated, updates = _prepare_relationship_changes(root, allocated)
    temporary_report = _temporary_tree_validation(root, allocated, updates)
    if not temporary_report.passed:
        details = "; ".join(f"{item.code}: {item.message}" for item in temporary_report.errors[:10])
        raise ValueError(f"generated records failed temporary-tree validation: {details}")
    created: list[Path] = []
    backups = {path: path.read_text(encoding="utf-8") for path in updates}
    try:
        for record_id, _, content in allocated:
            path = root / "database" / "records" / f"{record_id}.yaml"
            if path.exists():
                raise FileExistsError(f"refusing to overwrite {path}")
            path.write_text(content, encoding="utf-8", newline="\n")
            created.append(path)
        for path, content in updates.items():
            path.write_text(content, encoding="utf-8", newline="\n")
        commit = _ephemeral_commit(root, base, created + list(updates))
        committed_report = validate_repository(root, base=base, head=commit, comparison="direct")
        if not committed_report.passed:
            details = "; ".join(f"{item.code}: {item.message}" for item in committed_report.errors[:10])
            raise ValueError(f"generated records failed committed base/head validation: {details}")
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        for path, content in backups.items():
            path.write_text(content, encoding="utf-8", newline="")
        raise
    return {record_id: candidate.stable_identifier for record_id, candidate, _ in allocated}


def changed_record_paths(root: Path) -> list[str]:
    result = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        "database/records",
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot inspect generated record paths: {result.stderr.strip()}")
    paths: list[str] = []
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        path = entry[3:].replace("\\", "/")
        if not re.fullmatch(r"database/records/[0-9]{5}\.yaml", path):
            raise RuntimeError(f"unexpected database worktree path {path!r}")
        paths.append(path)
    return sorted(set(paths))
