"""Deterministic cross-source candidate reconciliation."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from .models import Author, Candidate
from .normalize import normalize_title


PUBLICATION_PRIORITY = ("crossref", "pubmed", "openalex", "arxiv")
PREPRINT_PRIORITY = ("biorxiv", "arxiv", "crossref", "openalex", "pubmed")


def _source_rank(candidate: Candidate, source: str) -> int:
    priority = PREPRINT_PRIORITY if candidate.publication_stage == "preprint" else PUBLICATION_PRIORITY
    try:
        return priority.index(source)
    except ValueError:
        return len(priority)


def _candidate_rank(candidate: Candidate) -> tuple[int, int, str]:
    sources = set(candidate.discovered_by) | set(candidate.enriched_by)
    best = min((_source_rank(candidate, source) for source in sources), default=99)
    completeness = -sum(
        bool(value)
        for value in (
            candidate.title,
            candidate.authors,
            candidate.doi,
            candidate.url,
            candidate.journal,
            candidate.document_type,
            candidate.canonical_date,
        )
    )
    return best, completeness, candidate.stable_identifier


def representation_key(candidate: Candidate) -> tuple[str, str]:
    stage = candidate.publication_stage or "unknown"
    if candidate.doi:
        return f"doi:{candidate.doi.casefold()}", "exact_identifier"
    for source, identifier in sorted(candidate.discovered_by.items()):
        if identifier:
            return f"{source}:{identifier}", stage
    year = candidate.canonical_date.year if candidate.canonical_date else 0
    return f"title:{normalize_title(candidate.title)}:{year}", stage


def _merge_mapping(target: dict[str, str], incoming: dict[str, str]) -> None:
    for source, identifier in incoming.items():
        if source not in target or not target[source]:
            target[source] = identifier


def _conflict_value(value: Any) -> Any:
    if isinstance(value, list) and value and isinstance(value[0], Author):
        return [author.to_dict() for author in value]
    return value


def merge_candidates(candidates: Iterable[Candidate]) -> Candidate:
    ordered = sorted(candidates, key=_candidate_rank)
    if not ordered:
        raise ValueError("cannot merge an empty candidate group")
    primary = ordered[0]
    fields = (
        "title",
        "authors",
        "doi",
        "url",
        "abstract",
        "journal",
        "work_type",
        "document_type",
        "publication_stage",
    )
    for incoming in ordered[1:]:
        _merge_mapping(primary.discovered_by, incoming.discovered_by)
        _merge_mapping(primary.enriched_by, incoming.enriched_by)
        for field_name in fields:
            current = getattr(primary, field_name)
            offered = getattr(incoming, field_name)
            if not current and offered:
                setattr(primary, field_name, offered)
            elif current and offered and _conflict_value(current) != _conflict_value(offered):
                warning = (
                    f"conflicting {field_name}: retained value from preferred source; "
                    f"alternatives are preserved in provenance"
                )
                if warning not in primary.warnings:
                    primary.warnings.append(warning)
        for field_name, entries in incoming.provenance.items():
            for entry in entries:
                primary.add_provenance(field_name, str(entry.get("source")), entry.get("value"))
        for item in incoming.dates:
            if item not in primary.dates:
                primary.dates.append(item)
        for item in incoming.relationships:
            if item not in primary.relationships:
                primary.relationships.append(item)
        for warning in incoming.warnings:
            if warning not in primary.warnings:
                primary.warnings.append(warning)
    primary.dates.sort(key=lambda item: (item.value, item.kind, item.source))
    return primary


def reconcile_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    groups: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[representation_key(candidate)].append(candidate)
    reconciled = [merge_candidates(group) for _, group in sorted(groups.items())]
    reconciled.sort(key=lambda item: item.stable_identifier)
    for index, first in enumerate(reconciled):
        for second in reconciled[index + 1 :]:
            if first.publication_stage == second.publication_stage:
                continue
            similarity = SequenceMatcher(
                None, normalize_title(first.title), normalize_title(second.title)
            ).ratio()
            first_authors = {normalize_title(author.name) for author in first.authors}
            second_authors = {normalize_title(author.name) for author in second.authors}
            if similarity >= 0.94 and first_authors & second_authors:
                for candidate, related in ((first, second), (second, first)):
                    warning = (
                        "high-confidence preprint/publication relationship suggestion with "
                        f"{related.stable_identifier}; confirm before adding reciprocal links"
                    )
                    if warning not in candidate.warnings:
                        candidate.warnings.append(warning)
    return reconciled
