from __future__ import annotations

import json
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts.discovery.config import load_queries
from scripts.discovery.models import Candidate, SourceDiagnostics
from scripts.discovery.sources.arxiv import ArxivAdapter
from scripts.discovery.sources.biorxiv import BioRxivEnricher
from scripts.discovery.sources.chemrxiv import ChemRxivEnricher
from scripts.discovery.sources.crossref import CrossrefAdapter
from scripts.discovery.sources.openalex import OpenAlexAdapter
from scripts.discovery.sources.pubmed import PubMedAdapter


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FixtureClient:
    def __init__(self, source: str, *, json_values: list[object] | None = None, xml_values: list[bytes] | None = None) -> None:
        self.diagnostics = SourceDiagnostics(source)
        self.offline = True
        self.json_values = list(json_values or [])
        self.xml_values = list(xml_values or [])

    def get_json(self, *_: object, **__: object) -> object:
        return self.json_values.pop(0)

    def get_xml(self, *_: object, **__: object) -> bytes:
        return self.xml_values.pop(0)


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_queries(ROOT / "discovery" / "queries.yaml")

    def json_fixture(self, source: str, filename: str) -> object:
        return json.loads((FIXTURES / source / filename).read_text(encoding="utf-8"))

    def test_openalex_fixture_normalizes_candidate(self) -> None:
        client = FixtureClient("openalex", json_values=[self.json_fixture("openalex", "search.json")] * 10)
        result = OpenAlexAdapter(self.config, client).search(date(2025, 1, 1), date(2025, 6, 30))
        self.assertTrue(result.diagnostics.complete)
        self.assertEqual(result.candidates[0].doi, "10.1234/pip.1")
        self.assertEqual(result.candidates[0].canonical_date, date(2025, 2, 3))

    def test_crossref_fixture_preserves_online_and_print_dates(self) -> None:
        client = FixtureClient("crossref", json_values=[self.json_fixture("crossref", "search.json")] * 10)
        result = CrossrefAdapter(self.config, client).search(date(2025, 1, 1), date(2025, 6, 30))
        kinds = {item.kind for item in result.candidates[0].dates}
        self.assertEqual(kinds, {"online", "print"})
        self.assertEqual(result.candidates[0].document_type, "research_article")

    def test_pubmed_fixture_uses_esearch_then_efetch(self) -> None:
        client = FixtureClient(
            "pubmed",
            json_values=[self.json_fixture("pubmed", "search.json")],
            xml_values=[(FIXTURES / "pubmed" / "fetch.xml").read_bytes()],
        )
        result = PubMedAdapter(self.config, client).search(date(2025, 1, 1), date(2025, 6, 30))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].discovered_by, {"pubmed": "12345678"})

    def test_arxiv_fixture_strips_version_suffix(self) -> None:
        client = FixtureClient("arxiv", xml_values=[(FIXTURES / "arxiv" / "search.xml").read_bytes()] * 10)
        result = ArxivAdapter(self.config, client).search(date(2025, 1, 1), date(2025, 6, 30))
        self.assertEqual(result.candidates[0].discovered_by, {"arxiv": "2502.01234"})
        self.assertEqual(result.candidates[0].publication_stage, "preprint")

    def test_biorxiv_fixture_prefers_latest_metadata_and_first_posted_date(self) -> None:
        candidate = Candidate("Old title", doi="10.1101/2025.01.01.123456")
        client = FixtureClient("biorxiv", json_values=[self.json_fixture("biorxiv", "details.json")])
        enriched = BioRxivEnricher(client).enrich(candidate)
        self.assertIn("revised", enriched.title)
        self.assertEqual(enriched.canonical_date, date(2025, 1, 1))
        self.assertEqual(enriched.relationships[0]["doi"], "10.1234/pip.2")

    def test_chemrxiv_is_explicitly_aggregator_derived(self) -> None:
        candidate = Candidate("ChemRxiv PIP", doi="10.26434/chemrxiv-2025-example")
        ChemRxivEnricher.enrich(candidate)
        self.assertTrue(any("aggregator-derived" in warning for warning in candidate.warnings))


if __name__ == "__main__":
    unittest.main()
