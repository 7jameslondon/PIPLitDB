"""Tiered database duplicate and relationship-aware classification."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

import yaml

from .config import Exclusion
from .models import Candidate
from .normalize import normalize_doi, normalize_title, normalize_url


@dataclass(frozen=True)
class DatabasePaper:
    record_id: str
    title: str
    normalized_title: str
    first_author: str
    author_names: frozenset[str]
    year: int
    doi: str | None
    url: str | None
    publication_stage: str | None


@dataclass
class DatabaseIndex:
    papers: list[DatabasePaper]
    by_doi: dict[str, DatabasePaper]
    by_url: dict[str, DatabasePaper]
    by_title_year: dict[tuple[str, int], list[DatabasePaper]]


def load_database(records_directory: Path, *, hidden_ids: Iterable[str] = ()) -> DatabaseIndex:
    hidden = set(hidden_ids)
    papers: list[DatabasePaper] = []
    for path in sorted(records_directory.glob("[0-9][0-9][0-9][0-9][0-9].yaml")):
        record_id = path.stem
        if record_id in hidden:
            continue
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot index {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"record {path} must be a mapping")
        authors = value.get("authors") if isinstance(value.get("authors"), list) else []
        author_names = [
            normalize_title(author.get("name"))
            for author in authors
            if isinstance(author, dict) and isinstance(author.get("name"), str)
        ]
        paper = DatabasePaper(
            record_id=record_id,
            title=str(value.get("title") or ""),
            normalized_title=normalize_title(value.get("title")),
            first_author=author_names[0] if author_names else "",
            author_names=frozenset(author_names),
            year=int(value.get("publication_year") or 0),
            doi=normalize_doi(value.get("doi")),
            url=normalize_url(value.get("url")),
            publication_stage=value.get("publication_stage"),
        )
        papers.append(paper)
    by_doi = {paper.doi: paper for paper in papers if paper.doi}
    by_url = {paper.url: paper for paper in papers if paper.url}
    by_title_year: dict[tuple[str, int], list[DatabasePaper]] = {}
    for paper in papers:
        by_title_year.setdefault((paper.normalized_title, paper.year), []).append(paper)
    return DatabaseIndex(papers, by_doi, by_url, by_title_year)


def _candidate_identifiers(candidate: Candidate) -> set[str]:
    identifiers = {candidate.stable_identifier}
    if candidate.doi:
        identifiers.add(f"doi:{candidate.doi.casefold()}")
    if candidate.url:
        normalized = normalize_url(candidate.url)
        if normalized:
            identifiers.add(f"url:{normalized}")
    identifiers.update(
        f"{source.casefold()}:{identifier}"
        for source, identifier in candidate.discovered_by.items()
        if identifier
    )
    return identifiers


def _compatible_author(candidate: Candidate, paper: DatabasePaper) -> bool:
    candidate_authors = {normalize_title(author.name) for author in candidate.authors}
    if not candidate_authors or not paper.author_names:
        return False
    first = normalize_title(candidate.authors[0].name)
    return first == paper.first_author or bool(candidate_authors & paper.author_names)


def _explicit_related_existing(candidate: Candidate, index: DatabaseIndex) -> list[DatabasePaper]:
    related: list[DatabasePaper] = []
    for relationship in candidate.relationships:
        doi = normalize_doi(relationship.get("doi"))
        if doi and doi in index.by_doi:
            related.append(index.by_doi[doi])
    return related


def classify_candidate(
    candidate: Candidate,
    index: DatabaseIndex,
    exclusions: Iterable[Exclusion],
    *,
    threshold: int,
    fuzzy_threshold: float = 0.94,
) -> str:
    if _candidate_identifiers(candidate) & {item.identifier for item in exclusions}:
        candidate.disposition = "excluded"
        return candidate.disposition
    if candidate.doi and candidate.doi in index.by_doi:
        candidate.disposition = "existing"
        candidate.matched_record_ids = [index.by_doi[candidate.doi].record_id]
        return candidate.disposition
    normalized_url = normalize_url(candidate.url)
    if normalized_url and normalized_url in index.by_url:
        candidate.disposition = "existing"
        candidate.matched_record_ids = [index.by_url[normalized_url].record_id]
        return candidate.disposition
    if candidate.score < threshold:
        candidate.disposition = "below_threshold"
        return candidate.disposition
    if not candidate.eligible_for_record:
        candidate.disposition = "needs_metadata"
        return candidate.disposition
    related = _explicit_related_existing(candidate, index)
    if related:
        candidate.disposition = "related_version"
        candidate.matched_record_ids = [paper.record_id for paper in related]
        return candidate.disposition
    title = normalize_title(candidate.title)
    year = candidate.canonical_date.year if candidate.canonical_date else 0
    exact_title_matches = index.by_title_year.get((title, year), [])
    if exact_title_matches:
        candidate.disposition = "possible_duplicate"
        candidate.matched_record_ids = [paper.record_id for paper in exact_title_matches]
        return candidate.disposition
    fuzzy_matches: list[DatabasePaper] = []
    for paper in index.papers:
        if paper.year and year and abs(paper.year - year) > 1:
            continue
        similarity = SequenceMatcher(None, title, paper.normalized_title).ratio()
        if similarity >= fuzzy_threshold and _compatible_author(candidate, paper):
            fuzzy_matches.append(paper)
    if fuzzy_matches:
        candidate.disposition = "possible_duplicate"
        candidate.matched_record_ids = [paper.record_id for paper in fuzzy_matches]
        return candidate.disposition
    candidate.disposition = "new"
    return candidate.disposition


def classify_candidates(
    candidates: Iterable[Candidate],
    index: DatabaseIndex,
    exclusions: Iterable[Exclusion],
    *,
    threshold: int,
) -> list[Candidate]:
    results = list(candidates)
    exclusion_list = list(exclusions)
    for candidate in results:
        classify_candidate(candidate, index, exclusion_list, threshold=threshold)
    results.sort(key=lambda item: item.stable_identifier)
    return results
