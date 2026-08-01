"""Shared, serializable models for discovery adapters and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import re
import unicodedata
from typing import Any


def _stable_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", normalized)).strip()


def _json_value(value: Any) -> Any:
    if isinstance(value, Author):
        return value.to_dict()
    if isinstance(value, DateValue):
        return value.to_dict()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class Author:
    name: str
    orcid: str | None = None

    def to_dict(self) -> dict[str, str]:
        value = {"name": self.name}
        if self.orcid:
            value["orcid"] = self.orcid
        return value


@dataclass(frozen=True)
class DateValue:
    value: date
    kind: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value.isoformat(), "kind": self.kind, "source": self.source}


@dataclass(frozen=True)
class MatchEvidence:
    query: str
    field: str
    matched_text: str
    score: int


@dataclass
class Candidate:
    title: str
    authors: list[Author] = field(default_factory=list)
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    journal: str | None = None
    work_type: str | None = None
    document_type: str | None = None
    publication_stage: str | None = None
    dates: list[DateValue] = field(default_factory=list)
    discovered_by: dict[str, str] = field(default_factory=dict)
    enriched_by: dict[str, str] = field(default_factory=dict)
    evidence: list[MatchEvidence] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
    provenance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    score: int = 0
    disposition: str | None = None
    matched_record_ids: list[str] = field(default_factory=list)

    def add_provenance(self, field_name: str, source: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        entry = {"source": source, "value": _json_value(value)}
        bucket = self.provenance.setdefault(field_name, [])
        if entry not in bucket:
            bucket.append(entry)

    @property
    def canonical_date(self) -> date | None:
        precedence = {
            "online": 0,
            "issued": 1,
            "print": 2,
            "first_posted": 0,
            "publication": 3,
        }
        eligible = [
            value
            for value in self.dates
            if value.kind not in {"created", "updated", "indexed", "latest_version"}
        ]
        if not eligible:
            return None
        eligible.sort(key=lambda item: (precedence.get(item.kind, 9), item.value, item.source))
        return eligible[0].value

    @property
    def stable_identifier(self) -> str:
        if self.doi:
            return f"doi:{self.doi.casefold()}"
        if self.url:
            return f"url:{self.url}"
        normalized_title = _stable_title(self.title)
        if normalized_title:
            return f"title:{normalized_title}"
        for source, identifier in sorted(self.discovered_by.items()):
            if identifier:
                return f"{source}:{identifier}"
        return "title:"

    @property
    def record_year(self) -> int | None:
        if not self.dates:
            return None
        if self.publication_stage == "preprint":
            preference = ("first_posted", "publication", "online", "issued", "print")
        else:
            preference = ("print", "issued", "publication", "online")
        for kind in preference:
            values = sorted(item.value for item in self.dates if item.kind == kind)
            if values:
                return values[0].year
        return self.canonical_date.year if self.canonical_date else None

    @property
    def eligible_for_record(self) -> bool:
        return bool(
            self.score
            and self.title
            and self.authors
            and self.canonical_date
            and self.journal
            and self.document_type
            and self.publication_stage
        )

    def to_dict(self, *, include_abstract: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "title": self.title,
            "authors": [author.to_dict() for author in self.authors],
            "doi": self.doi,
            "url": self.url,
            "journal": self.journal,
            "work_type": self.work_type,
            "document_type": self.document_type,
            "publication_stage": self.publication_stage,
            "dates": [item.to_dict() for item in self.dates],
            "canonical_date": self.canonical_date.isoformat() if self.canonical_date else None,
            "record_year": self.record_year,
            "discovered_by": dict(sorted(self.discovered_by.items())),
            "enriched_by": dict(sorted(self.enriched_by.items())),
            "evidence": [asdict(item) for item in self.evidence],
            "relationships": self.relationships,
            "provenance": self.provenance,
            "warnings": self.warnings,
            "score": self.score,
            "disposition": self.disposition,
            "matched_record_ids": self.matched_record_ids,
            "stable_identifier": self.stable_identifier,
            "eligible_for_record": self.eligible_for_record,
        }
        if include_abstract:
            value["abstract"] = self.abstract
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Candidate":
        candidate = cls(
            title=str(value.get("title") or ""),
            authors=[Author(str(item["name"]), item.get("orcid")) for item in value.get("authors", [])],
            doi=value.get("doi"),
            url=value.get("url"),
            abstract=value.get("abstract"),
            journal=value.get("journal"),
            work_type=value.get("work_type"),
            document_type=value.get("document_type"),
            publication_stage=value.get("publication_stage"),
            discovered_by=dict(value.get("discovered_by", {})),
            enriched_by=dict(value.get("enriched_by", {})),
            relationships=list(value.get("relationships", [])),
            provenance=dict(value.get("provenance", {})),
            warnings=list(value.get("warnings", [])),
            score=int(value.get("score") or 0),
            disposition=value.get("disposition"),
            matched_record_ids=list(value.get("matched_record_ids", [])),
        )
        for item in value.get("dates", []):
            candidate.dates.append(
                DateValue(date.fromisoformat(item["value"]), item["kind"], item["source"])
            )
        for item in value.get("evidence", []):
            candidate.evidence.append(MatchEvidence(**item))
        return candidate


@dataclass
class SourceDiagnostics:
    source: str
    complete: bool = True
    request_count: int = 0
    cache_hits: int = 0
    retries: int = 0
    result_count: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class SourceResult:
    source: str
    candidates: list[Candidate]
    diagnostics: SourceDiagnostics


@dataclass
class DiscoveryReport:
    status: str
    date_from: date
    date_until: date
    resolved_today: date
    candidates: list[Candidate]
    source_diagnostics: list[SourceDiagnostics]
    config_digest: str
    generated_records: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": 1,
            "status": self.status,
            "date_from": self.date_from.isoformat(),
            "date_until": self.date_until.isoformat(),
            "resolved_today": self.resolved_today.isoformat(),
            "config_digest": self.config_digest,
            "source_diagnostics": [asdict(item) for item in self.source_diagnostics],
            "counts": {
                "total": len(self.candidates),
                "new": sum(item.disposition == "new" for item in self.candidates),
                "existing": sum(item.disposition == "existing" for item in self.candidates),
                "possible_duplicate": sum(
                    item.disposition == "possible_duplicate" for item in self.candidates
                ),
                "related_version": sum(
                    item.disposition == "related_version" for item in self.candidates
                ),
                "excluded": sum(item.disposition == "excluded" for item in self.candidates),
                "needs_metadata": sum(
                    item.disposition == "needs_metadata" for item in self.candidates
                ),
            },
            "generated_records": self.generated_records,
            "warnings": self.warnings,
            "candidates": [item.to_dict() for item in self.candidates],
        }
