from __future__ import annotations

import unittest
from datetime import date

from scripts.discovery.config import QueryGroup
from scripts.discovery.models import Candidate
from scripts.discovery.normalize import (
    canonical_doi_url,
    inverted_abstract,
    normalize_doi,
    normalize_title,
    score_candidate,
)


class NormalizeTests(unittest.TestCase):
    def test_doi_normalization_removes_resolver_and_decodes(self) -> None:
        self.assertEqual(normalize_doi(" HTTPS://DOI.ORG/10.1234/ABC%2FDef "), "10.1234/abc/def")
        self.assertEqual(canonical_doi_url("10.1234/abc"), "https://doi.org/10.1234/abc")

    def test_malformed_doi_is_none(self) -> None:
        self.assertIsNone(normalize_doi("doi: not-a-doi"))

    def test_title_normalization_handles_unicode_and_punctuation(self) -> None:
        self.assertEqual(normalize_title("Pyrrole–Imidazole  Polyamide!"), "pyrrole imidazole polyamide")

    def test_openalex_inverted_abstract_is_ordered(self) -> None:
        self.assertEqual(inverted_abstract({"world": [1], "hello": [0]}), "hello world")

    def test_scoring_records_bounded_explicit_evidence(self) -> None:
        group = QueryGroup("exact", ("pyrrole-imidazole polyamide",), 8, ("DNA",), 1, ("title", "abstract"), ("openalex",))
        candidate = Candidate("A pyrrole-imidazole polyamide", abstract="DNA binding")
        self.assertEqual(score_candidate(candidate, [group]), 9)
        self.assertEqual({item.field for item in candidate.evidence}, {"title", "supporting_term"})


if __name__ == "__main__":
    unittest.main()
