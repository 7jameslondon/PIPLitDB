"""arXiv Atom discovery adapter."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from ..config import DiscoveryConfig
from ..http import HttpClient, HttpFailure
from ..models import Author, Candidate, SourceResult
from ..normalize import add_date, canonical_doi_url, clean_text, normalize_doi, normalize_url


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


class ArxivAdapter:
    name = "arxiv"
    endpoint = "https://export.arxiv.org/api/query"

    def __init__(self, config: DiscoveryConfig, client: HttpClient) -> None:
        self.config = config
        self.client = client

    def search(self, start: date, end: date) -> SourceResult:
        candidates: list[Candidate] = []
        diagnostics = self.client.diagnostics
        try:
            for group in self.config.groups_for(self.name):
                for phrase in group.phrases:
                    offset = 0
                    while True:
                        query = (
                            f'(ti:"{phrase}" OR abs:"{phrase}") AND '
                            f"submittedDate:[{start.strftime('%Y%m%d')}0000 TO {end.strftime('%Y%m%d')}2359]"
                        )
                        body = self.client.get_xml(
                            self.endpoint,
                            {
                                "search_query": query,
                                "start": offset,
                                "max_results": 100,
                                "sortBy": "submittedDate",
                                "sortOrder": "descending",
                            },
                        )
                        root = ET.fromstring(body)
                        entries = root.findall(f"{ATOM}entry")
                        for entry in entries:
                            candidate = self._candidate(entry)
                            if candidate:
                                candidates.append(candidate)
                        offset += len(entries)
                        if len(entries) < 100 or len(candidates) >= self.config.max_candidates:
                            break
            diagnostics.result_count = len(candidates)
        except (HttpFailure, ET.ParseError, TypeError, ValueError) as exc:
            diagnostics.complete = False
            diagnostics.error = str(exc)
        return SourceResult(self.name, candidates[: self.config.max_candidates], diagnostics)

    def _candidate(self, entry: ET.Element) -> Candidate | None:
        raw_id = clean_text(entry.findtext(f"{ATOM}id"), maximum=300)
        title = clean_text(entry.findtext(f"{ATOM}title"), maximum=2000)
        if not raw_id or not title:
            return None
        arxiv_id = VERSION_RE.sub("", raw_id.rstrip("/").rsplit("/", 1)[-1])
        doi = normalize_doi(entry.findtext(f"{ARXIV}doi"))
        journal_ref = clean_text(entry.findtext(f"{ARXIV}journal_ref"), maximum=500)
        authors = [
            Author(name)
            for node in entry.findall(f"{ATOM}author")[:200]
            if (name := clean_text(node.findtext(f"{ATOM}name"), maximum=300))
        ]
        candidate = Candidate(
            title=title,
            authors=authors,
            doi=doi,
            url=canonical_doi_url(doi) or normalize_url(raw_id),
            abstract=clean_text(entry.findtext(f"{ATOM}summary"), maximum=20_000),
            journal=journal_ref or "arXiv",
            work_type="preprint",
            document_type="research_article",
            publication_stage="preprint",
            discovered_by={self.name: arxiv_id},
        )
        try:
            published = date.fromisoformat((entry.findtext(f"{ATOM}published") or "")[:10])
        except ValueError:
            published = None
        try:
            updated = date.fromisoformat((entry.findtext(f"{ATOM}updated") or "")[:10])
        except ValueError:
            updated = None
        add_date(candidate, published, "first_posted", self.name)
        add_date(candidate, updated, "latest_version", self.name)
        for field_name in ("title", "authors", "doi", "url", "journal", "work_type"):
            candidate.add_provenance(field_name, self.name, getattr(candidate, field_name))
        return candidate
