"""Small, strict reader for the public per-record metadata used in extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - CLI reports the dependency
    yaml = None


@dataclass(frozen=True)
class RecordMetadata:
    record_id: str
    title: str
    authors: tuple[str, ...]
    journal: str
    publication_year: int | str
    doi: str
    document_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "title": self.title,
            "authors": list(self.authors),
            "journal": self.journal,
            "publication_year": self.publication_year,
            "doi": self.doi,
            "document_type": self.document_type,
        }


def _load_record_mapping(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required for extraction")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"metadata must be a mapping: {path}")
    return value


def load_record_metadata(path: Path, record_id: str) -> RecordMetadata:
    value = _load_record_mapping(path)
    title = value.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"metadata title is missing: {path}")
    authors_value = value.get("authors")
    if not isinstance(authors_value, list) or not authors_value:
        raise ValueError(f"metadata authors are missing: {path}")
    authors: list[str] = []
    for author in authors_value:
        if isinstance(author, dict):
            name = author.get("canonical_name") or author.get("name")
        else:
            name = author
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"invalid author entry in {path}")
        authors.append(name.strip())
    year = value.get("publication_year", "")
    if not isinstance(year, (int, str)):
        raise ValueError(f"invalid publication_year in {path}")
    return RecordMetadata(
        record_id=record_id,
        title=title.strip(),
        authors=tuple(authors),
        journal=str(value.get("journal", "")).strip(),
        publication_year=year,
        doi=str(value.get("doi", "")).strip(),
        document_type=str(value.get("document_type", "")).strip(),
    )


def load_record_status(path: Path) -> str:
    """Read only the controlled extraction-workflow status from a record."""

    value = _load_record_mapping(path)
    status = value.get("pip_litdb_status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError(f"pip_litdb_status is missing: {path}")
    return status.strip()
