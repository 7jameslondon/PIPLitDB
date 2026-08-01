"""Crossref works discovery adapter."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from ..config import DiscoveryConfig
from ..http import HttpClient, HttpFailure
from ..models import Author, Candidate, SourceResult
from ..normalize import (
    add_date,
    canonical_doi_url,
    clean_text,
    date_from_parts,
    map_work_type,
    normalize_doi,
    normalize_url,
)


class CrossrefAdapter:
    name = "crossref"
    endpoint = "https://api.crossref.org/works"

    def __init__(self, config: DiscoveryConfig, client: HttpClient) -> None:
        self.config = config
        self.client = client

    def search(self, start: date, end: date) -> SourceResult:
        candidates: list[Candidate] = []
        diagnostics = self.client.diagnostics
        contact = os.environ.get("DISCOVERY_CONTACT_EMAIL")
        if not contact and not self.client.offline:
            diagnostics.complete = False
            diagnostics.error = "DISCOVERY_CONTACT_EMAIL is required for live Crossref discovery"
            return SourceResult(self.name, [], diagnostics)
        try:
            for group in self.config.groups_for(self.name):
                for phrase in group.phrases:
                    cursor = "*"
                    while cursor:
                        payload = self.client.get_json(
                            self.endpoint,
                            {
                                "query.bibliographic": phrase,
                                "filter": f"from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()}",
                                "rows": 100,
                                "cursor": cursor,
                                "mailto": contact,
                                "select": (
                                    "DOI,title,author,abstract,URL,type,container-title,published-online,"
                                    "published-print,issued,created,relation,subtype"
                                ),
                            },
                        )
                        message = payload.get("message") if isinstance(payload, dict) else None
                        items = message.get("items") if isinstance(message, dict) else None
                        if not isinstance(items, list):
                            raise HttpFailure("Crossref response is missing message.items")
                        for item in items:
                            candidate = self._candidate(item)
                            if candidate:
                                candidates.append(candidate)
                        next_cursor = message.get("next-cursor")
                        cursor = str(next_cursor) if next_cursor and items else ""
                        if len(candidates) >= self.config.max_candidates:
                            cursor = ""
            diagnostics.result_count = len(candidates)
        except (HttpFailure, TypeError, ValueError) as exc:
            diagnostics.complete = False
            diagnostics.error = str(exc)
        return SourceResult(self.name, candidates[: self.config.max_candidates], diagnostics)

    @staticmethod
    def _first(value: Any) -> Any:
        return value[0] if isinstance(value, list) and value else value

    def _candidate(self, item: Any) -> Candidate | None:
        if not isinstance(item, dict):
            return None
        doi = normalize_doi(item.get("DOI"))
        title = clean_text(self._first(item.get("title")), maximum=2000)
        if not title or not doi:
            return None
        subtype = clean_text(item.get("subtype"), maximum=100).casefold()
        container = clean_text(self._first(item.get("container-title")), maximum=500)
        preprint = item.get("type") == "posted-content" or subtype == "preprint" or container.casefold() in {
            "biorxiv",
            "chemrxiv",
            "medrxiv",
        }
        document_type, stage = map_work_type(item.get("type"), preprint=preprint)
        authors: list[Author] = []
        for author in (item.get("author") or [])[:200]:
            if not isinstance(author, dict):
                continue
            name = clean_text(" ".join(filter(None, [author.get("given"), author.get("family")])), maximum=300)
            orcid = clean_text(author.get("ORCID"), maximum=100) or None
            if name:
                authors.append(Author(name, orcid))
        candidate = Candidate(
            title=title,
            authors=authors,
            doi=doi,
            url=canonical_doi_url(doi) or normalize_url(item.get("URL")),
            abstract=clean_text(item.get("abstract"), maximum=20_000),
            journal=container or ("ChemRxiv" if "chemrxiv" in doi else None),
            work_type=clean_text(item.get("type"), maximum=100) or None,
            document_type=document_type,
            publication_stage=stage,
            discovered_by={self.name: doi},
        )
        date_fields = (
            ("published-online", "online"),
            ("published-print", "print"),
            ("issued", "issued"),
            ("created", "created"),
        )
        for field_name, kind in date_fields:
            raw = item.get(field_name)
            parts = raw.get("date-parts") if isinstance(raw, dict) else None
            add_date(candidate, date_from_parts(self._first(parts)), kind, self.name)
        relations = item.get("relation")
        if isinstance(relations, dict):
            for relationship, values in relations.items():
                if not isinstance(values, list):
                    continue
                for related in values[:20]:
                    if isinstance(related, dict):
                        related_doi = normalize_doi(related.get("id"))
                        if related_doi:
                            candidate.relationships.append({"type": str(relationship), "doi": related_doi})
        for field_name in ("title", "authors", "doi", "url", "journal", "work_type"):
            candidate.add_provenance(field_name, self.name, getattr(candidate, field_name))
        return candidate
