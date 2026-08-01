"""OpenAlex works search adapter."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from ..config import DiscoveryConfig
from ..http import HttpClient, HttpFailure
from ..models import Author, Candidate, SourceDiagnostics, SourceResult
from ..normalize import (
    add_date,
    canonical_doi_url,
    clean_text,
    date_from_parts,
    inverted_abstract,
    map_work_type,
    normalize_doi,
    normalize_url,
)


class OpenAlexAdapter:
    name = "openalex"
    endpoint = "https://api.openalex.org/works"

    def __init__(self, config: DiscoveryConfig, client: HttpClient) -> None:
        self.config = config
        self.client = client

    def search(self, start: date, end: date) -> SourceResult:
        candidates: list[Candidate] = []
        diagnostics = self.client.diagnostics
        api_key = os.environ.get("OPENALEX_API_KEY")
        if not api_key and not self.client.offline:
            diagnostics.complete = False
            diagnostics.error = "OPENALEX_API_KEY is required for live OpenAlex discovery"
            return SourceResult(self.name, [], diagnostics)
        try:
            for group in self.config.groups_for(self.name):
                for phrase in group.phrases:
                    cursor = "*"
                    while cursor:
                        payload = self.client.get_json(
                            self.endpoint,
                            {
                                "api_key": api_key,
                                "search": phrase,
                                "filter": (
                                    f"from_publication_date:{start.isoformat()},"
                                    f"to_publication_date:{end.isoformat()}"
                                ),
                                "per_page": 100,
                                "cursor": cursor,
                                "select": (
                                    "id,doi,display_name,title,publication_date,type,authorships,"
                                    "primary_location,locations,abstract_inverted_index,ids,"
                                    "best_oa_location,updated_date"
                                ),
                            },
                        )
                        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                            raise HttpFailure("OpenAlex response is missing a results list")
                        for work in payload["results"]:
                            candidate = self._candidate(work)
                            if candidate:
                                candidates.append(candidate)
                        next_cursor = (payload.get("meta") or {}).get("next_cursor")
                        cursor = str(next_cursor) if next_cursor and payload["results"] else ""
                        if len(candidates) >= self.config.max_candidates:
                            cursor = ""
            diagnostics.result_count = len(candidates)
        except (HttpFailure, TypeError, ValueError) as exc:
            diagnostics.complete = False
            diagnostics.error = str(exc)
        return SourceResult(self.name, candidates[: self.config.max_candidates], diagnostics)

    def _candidate(self, work: Any) -> Candidate | None:
        if not isinstance(work, dict):
            return None
        title = clean_text(work.get("display_name") or work.get("title"), maximum=2000)
        identifier = clean_text(work.get("id"), maximum=300).rsplit("/", 1)[-1]
        if not title or not identifier:
            return None
        doi = normalize_doi(work.get("doi"))
        location = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
        source = location.get("source") if isinstance(location.get("source"), dict) else {}
        source_name = clean_text(source.get("display_name"), maximum=500)
        source_type = clean_text(source.get("type"), maximum=100).casefold()
        preprint = source_type == "repository" or source_name.casefold() in {"biorxiv", "chemrxiv", "arxiv"}
        document_type, stage = map_work_type(work.get("type"), preprint=preprint)
        authors: list[Author] = []
        for authorship in (work.get("authorships") or [])[:200]:
            author = authorship.get("author") if isinstance(authorship, dict) else None
            if not isinstance(author, dict):
                continue
            name = clean_text(author.get("display_name"), maximum=300)
            orcid = clean_text(author.get("orcid"), maximum=100) or None
            if name:
                authors.append(Author(name, orcid))
        candidate = Candidate(
            title=title,
            authors=authors,
            doi=doi,
            url=canonical_doi_url(doi)
            or normalize_url(location.get("landing_page_url"))
            or normalize_url((work.get("best_oa_location") or {}).get("landing_page_url")),
            abstract=inverted_abstract(work.get("abstract_inverted_index")),
            journal=source_name or None,
            work_type=clean_text(work.get("type"), maximum=100) or None,
            document_type=document_type,
            publication_stage=stage,
            discovered_by={self.name: identifier},
        )
        add_date(candidate, date_from_parts(work.get("publication_date")), "publication", self.name)
        add_date(candidate, date_from_parts(work.get("updated_date")), "updated", self.name)
        for field_name in ("title", "authors", "doi", "url", "journal", "work_type"):
            candidate.add_provenance(field_name, self.name, getattr(candidate, field_name))
        return candidate
