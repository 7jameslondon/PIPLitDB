from __future__ import annotations

import unittest
from datetime import date

from scripts.discovery.models import Author, Candidate, DateValue
from scripts.discovery.reconcile import reconcile_candidates


def candidate(source: str, *, stage: str = "publication", title: str = "PIP paper", doi: str = "10.1234/pip.1") -> Candidate:
    return Candidate(
        title,
        [Author("Ada Example")],
        doi=doi,
        journal="Journal",
        document_type="research_article",
        publication_stage=stage,
        dates=[DateValue(date(2025, 1, 2), "online", source)],
        discovered_by={source: source + "-id"},
    )


class ReconcileTests(unittest.TestCase):
    def test_same_doi_and_stage_collapse_with_source_roles(self) -> None:
        crossref = candidate("crossref")
        openalex = candidate("openalex")
        values = reconcile_candidates([openalex, crossref])
        self.assertEqual(len(values), 1)
        self.assertEqual(set(values[0].discovered_by), {"crossref", "openalex"})

    def test_preprint_and_publication_are_not_collapsed(self) -> None:
        values = reconcile_candidates([
            candidate("crossref", doi="10.1234/publication"),
            candidate("arxiv", stage="preprint", doi="10.1234/preprint"),
        ])
        self.assertEqual(len(values), 2)
        self.assertTrue(any("relationship suggestion" in warning for value in values for warning in value.warnings))

    def test_same_doi_with_conflicting_stage_is_one_candidate(self) -> None:
        values = reconcile_candidates([candidate("crossref"), candidate("arxiv", stage="preprint")])
        self.assertEqual(len(values), 1)
        self.assertTrue(any("conflicting publication_stage" in warning for warning in values[0].warnings))

    def test_conflicting_metadata_is_reported(self) -> None:
        first = candidate("crossref", title="Preferred title")
        second = candidate("openalex", title="Alternative title")
        value = reconcile_candidates([second, first])[0]
        self.assertEqual(value.title, "Preferred title")
        self.assertTrue(any("conflicting title" in warning for warning in value.warnings))


if __name__ == "__main__":
    unittest.main()
