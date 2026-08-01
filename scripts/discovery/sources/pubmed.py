"""NCBI PubMed ESearch/EFetch discovery adapter."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import date
from typing import Iterable

from ..config import DiscoveryConfig
from ..http import HttpClient, HttpFailure
from ..models import Author, Candidate, SourceResult
from ..normalize import add_date, canonical_doi_url, clean_text, date_from_parts, normalize_doi


class PubMedAdapter:
    name = "pubmed"
    search_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    fetch_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    def __init__(self, config: DiscoveryConfig, client: HttpClient) -> None:
        self.config = config
        self.client = client

    def search(self, start: date, end: date) -> SourceResult:
        candidates: list[Candidate] = []
        diagnostics = self.client.diagnostics
        email = os.environ.get("DISCOVERY_CONTACT_EMAIL")
        if not email and not self.client.offline:
            diagnostics.complete = False
            diagnostics.error = "DISCOVERY_CONTACT_EMAIL is required for live PubMed discovery"
            return SourceResult(self.name, [], diagnostics)
        common = {
            "tool": "pip_litdb_discovery",
            "email": email,
            "api_key": os.environ.get("NCBI_API_KEY"),
        }
        try:
            phrases = [
                f'"{phrase}"[Title/Abstract]'
                for group in self.config.groups_for(self.name)
                for phrase in group.phrases
            ]
            term = "(" + " OR ".join(phrases) + ")"
            payload = self.client.get_json(
                self.search_endpoint,
                {
                    **common,
                    "db": "pubmed",
                    "term": term,
                    "datetype": "pdat",
                    "mindate": start.strftime("%Y/%m/%d"),
                    "maxdate": end.strftime("%Y/%m/%d"),
                    "retmode": "json",
                    "retmax": self.config.max_candidates,
                    "sort": "pub date",
                },
            )
            result = payload.get("esearchresult") if isinstance(payload, dict) else None
            identifiers = result.get("idlist") if isinstance(result, dict) else None
            if not isinstance(identifiers, list):
                raise HttpFailure("PubMed ESearch response is missing esearchresult.idlist")
            for offset in range(0, len(identifiers), 200):
                batch = [str(value) for value in identifiers[offset : offset + 200] if str(value).isdigit()]
                if not batch:
                    continue
                body = self.client.get_xml(
                    self.fetch_endpoint,
                    {**common, "db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
                )
                root = ET.fromstring(body)
                for article in root.findall(".//PubmedArticle"):
                    candidate = self._candidate(article)
                    if candidate:
                        candidates.append(candidate)
            diagnostics.result_count = len(candidates)
        except (HttpFailure, ET.ParseError, TypeError, ValueError) as exc:
            diagnostics.complete = False
            diagnostics.error = str(exc)
        return SourceResult(self.name, candidates[: self.config.max_candidates], diagnostics)

    @staticmethod
    def _text(element: ET.Element | None) -> str:
        return clean_text("".join(element.itertext()) if element is not None else "")

    @staticmethod
    def _date(element: ET.Element | None) -> date | None:
        if element is None:
            return None
        values: list[int] = []
        month_names = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        year_text = element.findtext("Year")
        if not year_text or not year_text.isdigit():
            return None
        values.append(int(year_text))
        month_text = (element.findtext("Month") or "1").strip()
        values.append(int(month_text) if month_text.isdigit() else month_names.get(month_text[:3].casefold(), 1))
        day_text = (element.findtext("Day") or "1").strip()
        values.append(int(day_text) if day_text.isdigit() else 1)
        return date_from_parts(values)

    def _candidate(self, article: ET.Element) -> Candidate | None:
        citation = article.find("MedlineCitation")
        data = citation.find("Article") if citation is not None else None
        if citation is None or data is None:
            return None
        pmid = clean_text(citation.findtext("PMID"), maximum=30)
        title = self._text(data.find("ArticleTitle"))
        if not pmid or not title:
            return None
        doi = None
        for identifier in article.findall(".//ArticleId"):
            if identifier.get("IdType") == "doi":
                doi = normalize_doi(identifier.text)
        authors: list[Author] = []
        for item in data.findall(".//Author")[:200]:
            collective = clean_text(item.findtext("CollectiveName"), maximum=300)
            name = collective or clean_text(
                " ".join(filter(None, [item.findtext("ForeName"), item.findtext("LastName")])), maximum=300
            )
            if name:
                orcid = None
                for identifier in item.findall("Identifier"):
                    if identifier.get("Source", "").casefold() == "orcid":
                        orcid = clean_text(identifier.text, maximum=100) or None
                authors.append(Author(name, orcid))
        publication_types = {
            clean_text(node.text, maximum=100).casefold() for node in data.findall(".//PublicationType")
        }
        if "review" in publication_types:
            document_type = "review"
        elif publication_types & {"published erratum", "retraction of publication", "corrected and republished article"}:
            document_type = "correction"
        elif publication_types & {"journal article", "clinical trial", "comparative study"}:
            document_type = "research_article"
        else:
            document_type = None
        journal = self._text(data.find(".//Journal/Title"))
        abstract = " ".join(self._text(node) for node in data.findall(".//Abstract/AbstractText"))
        candidate = Candidate(
            title=title,
            authors=authors,
            doi=doi,
            url=canonical_doi_url(doi) or f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            abstract=clean_text(abstract, maximum=20_000),
            journal=journal or None,
            work_type=", ".join(sorted(publication_types)) or None,
            document_type=document_type,
            publication_stage="publication",
            discovered_by={self.name: pmid},
        )
        add_date(candidate, self._date(data.find(".//ArticleDate")), "online", self.name)
        add_date(candidate, self._date(data.find(".//JournalIssue/PubDate")), "print", self.name)
        for field_name in ("title", "authors", "doi", "url", "journal", "work_type"):
            candidate.add_provenance(field_name, self.name, getattr(candidate, field_name))
        return candidate
