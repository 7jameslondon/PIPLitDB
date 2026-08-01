"""Normalization and bounded relevance evidence extraction."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import date
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

from .models import Author, Candidate, DateValue, MatchEvidence


DOI_RE = re.compile(r"^10\.[0-9]{4,9}/\S+$", re.IGNORECASE)
DOI_PREFIX_RE = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
TITLE_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


def clean_text(value: Any, *, maximum: int = 10_000) -> str:
    if value is None:
        return ""
    text = html.unescape(TAG_RE.sub(" ", str(value)))
    text = SPACE_RE.sub(" ", text).strip()
    return text[:maximum]


def normalize_doi(value: Any) -> str | None:
    text = unquote(clean_text(value, maximum=2048))
    text = DOI_PREFIX_RE.sub("", text).strip().rstrip(".,;)")
    if not DOI_RE.fullmatch(text) or any(character.isspace() for character in text):
        return None
    return text.casefold()


def canonical_doi_url(doi: str | None) -> str | None:
    return f"https://doi.org/{doi}" if doi else None


def normalize_url(value: Any) -> str | None:
    text = clean_text(value, maximum=4096)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    netloc = parsed.hostname.casefold()
    if port:
        netloc += f":{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value, maximum=2000)).casefold()
    return SPACE_RE.sub(" ", TITLE_TOKEN_RE.sub(" ", text)).strip()


def normalize_author_name(value: Any) -> str:
    return SPACE_RE.sub(" ", unicodedata.normalize("NFKC", clean_text(value, maximum=300))).strip()


def authors_from_names(values: Iterable[Any]) -> list[Author]:
    authors: list[Author] = []
    for value in values:
        name = normalize_author_name(value)
        if name and name.casefold() not in {item.name.casefold() for item in authors}:
            authors.append(Author(name))
    return authors


def date_from_parts(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and value:
        try:
            year = int(value[0])
            month = int(value[1]) if len(value) > 1 else 1
            day = int(value[2]) if len(value) > 2 else 1
            return date(year, month, day)
        except (TypeError, ValueError):
            return None
    return None


def inverted_abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in value.items():
        if not isinstance(word, str) or not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int) and 0 <= index < 100_000:
                positions.append((index, word))
    positions.sort()
    return clean_text(" ".join(word for _, word in positions), maximum=20_000)


def map_work_type(work_type: str | None, *, preprint: bool = False) -> tuple[str | None, str]:
    normalized = clean_text(work_type, maximum=100).casefold().replace("-", "_").replace(" ", "_")
    if preprint or normalized in {"posted_content", "preprint"}:
        stage = "preprint"
    else:
        stage = "publication"
    mapping = {
        "article": "research_article",
        "journal_article": "research_article",
        "research_article": "research_article",
        "posted_content": "research_article",
        "preprint": "research_article",
        "review": "review",
        "review_article": "review",
        "correction": "correction",
        "erratum": "correction",
    }
    return mapping.get(normalized), stage


def add_date(candidate: Candidate, value: date | None, kind: str, source: str) -> None:
    if value is None:
        return
    item = DateValue(value, kind, source)
    if item not in candidate.dates:
        candidate.dates.append(item)


def score_candidate(candidate: Candidate, query_groups: Iterable[Any]) -> int:
    candidate.evidence.clear()
    fields = {"title": candidate.title, "abstract": candidate.abstract or ""}
    for group in query_groups:
        group_matched = False
        for phrase in group.phrases:
            phrase_normalized = normalize_title(phrase)
            for field_name in group.match_fields:
                field_value = fields.get(field_name, "")
                if phrase_normalized and phrase_normalized in normalize_title(field_value):
                    snippet = clean_text(field_value, maximum=240)
                    candidate.evidence.append(
                        MatchEvidence(group.name, field_name, snippet, group.weight)
                    )
                    group_matched = True
                    break
            if any(item.query == group.name for item in candidate.evidence):
                break
        if group_matched:
            searchable = normalize_title(" ".join(fields.values()))
            for term in group.supporting_terms:
                if normalize_title(term) in searchable:
                    candidate.evidence.append(
                        MatchEvidence(
                            group.name,
                            "supporting_term",
                            clean_text(term, maximum=80),
                            group.weight_per_supporting_term,
                        )
                    )
    candidate.score = sum(item.score for item in candidate.evidence)
    return candidate.score
