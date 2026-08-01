"""Direct bioRxiv DOI enrichment for aggregator-discovered preprints."""

from __future__ import annotations

from ..http import HttpClient, HttpFailure
from ..models import Author, Candidate
from ..normalize import add_date, clean_text, date_from_parts, normalize_doi


class BioRxivEnricher:
    name = "biorxiv"
    endpoint = "https://api.biorxiv.org/details/biorxiv/{doi}/na/json"

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def applicable(self, candidate: Candidate) -> bool:
        return bool(candidate.doi and candidate.doi.startswith("10.1101/"))

    def enrich(self, candidate: Candidate) -> Candidate:
        if not self.applicable(candidate):
            return candidate
        try:
            payload = self.client.get_json(self.endpoint.format(doi=candidate.doi), {})
            records = payload.get("collection") if isinstance(payload, dict) else None
            if not isinstance(records, list) or not records:
                raise HttpFailure("bioRxiv response has no collection records")
            records = [item for item in records if isinstance(item, dict)]
            records.sort(key=lambda item: int(item.get("version") or 0))
            item = records[-1]
            first = records[0]
            identifier = normalize_doi(item.get("doi")) or candidate.doi
            title = clean_text(item.get("title"), maximum=2000)
            author_names = [
                clean_text(value, maximum=300)
                for value in str(item.get("authors") or "").split(";")[:200]
            ]
            if title:
                candidate.add_provenance("title", self.name, title)
                candidate.title = title
            if any(author_names):
                candidate.authors = [Author(name) for name in author_names if name]
                candidate.add_provenance("authors", self.name, candidate.authors)
            abstract = clean_text(item.get("abstract"), maximum=20_000)
            if abstract:
                candidate.abstract = abstract
            candidate.journal = "bioRxiv"
            candidate.publication_stage = "preprint"
            candidate.document_type = "research_article"
            add_date(candidate, date_from_parts(first.get("date")), "first_posted", self.name)
            add_date(candidate, date_from_parts(item.get("date")), "latest_version", self.name)
            published = normalize_doi(item.get("published"))
            if published:
                candidate.relationships.append({"type": "is_preprint_of", "doi": published})
            candidate.enriched_by[self.name] = identifier
        except (HttpFailure, TypeError, ValueError) as exc:
            candidate.warnings.append(f"bioRxiv enrichment failed: {exc}")
        return candidate
