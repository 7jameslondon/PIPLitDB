from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.discovery.config import Exclusion
from scripts.discovery.deduplicate import classify_candidate, load_database
from scripts.discovery.models import Author, Candidate, DateValue


RECORD = """document_type: research_article
publication_stage: publication
title: "A pyrrole-imidazole polyamide paper"
authors:
  - name: "Ada Example"
doi: "10.1234/existing"
url: "https://doi.org/10.1234/existing"
publication_year: 2025
journal: "Journal"
"""


def candidate(*, doi: str = "10.1234/new", title: str = "A new pyrrole-imidazole polyamide paper") -> Candidate:
    return Candidate(
        title,
        [Author("Ada Example")],
        doi=doi,
        url=f"https://doi.org/{doi}",
        journal="Journal",
        document_type="research_article",
        publication_stage="publication",
        dates=[DateValue(date(2025, 2, 1), "online", "crossref")],
        discovered_by={"crossref": doi},
        score=8,
    )


class DeduplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        records = Path(self.temporary.name)
        (records / "00001.yaml").write_text(RECORD, encoding="utf-8")
        self.index = load_database(records)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_doi_is_existing(self) -> None:
        value = candidate(doi="10.1234/existing")
        self.assertEqual(classify_candidate(value, self.index, [], threshold=6), "existing")
        self.assertEqual(value.matched_record_ids, ["00001"])

    def test_exact_title_year_is_possible_duplicate(self) -> None:
        value = candidate(title="A pyrrole imidazole polyamide paper")
        self.assertEqual(classify_candidate(value, self.index, [], threshold=6), "possible_duplicate")

    def test_low_score_stays_report_only(self) -> None:
        value = candidate()
        value.score = 1
        self.assertEqual(classify_candidate(value, self.index, [], threshold=6), "below_threshold")

    def test_exact_exclusion_wins(self) -> None:
        value = candidate()
        exclusion = Exclusion("doi:10.1234/new", "Reviewed false positive", date(2026, 1, 1))
        self.assertEqual(classify_candidate(value, self.index, [exclusion], threshold=6), "excluded")

    def test_explicit_related_doi_keeps_distinct_version(self) -> None:
        value = candidate()
        value.publication_stage = "preprint"
        value.relationships = [{"type": "is_preprint_of", "doi": "10.1234/existing"}]
        self.assertEqual(classify_candidate(value, self.index, [], threshold=6), "related_version")


if __name__ == "__main__":
    unittest.main()
