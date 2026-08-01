from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.discovery.models import Author, Candidate, DateValue
from scripts.discovery.records import allocate_records, serialize_record, write_records


ROOT = Path(__file__).resolve().parents[2]
VALID_RECORD = """document_type: research_article
publication_stage: publication
title: "Existing paper"
authors:
  - name: "Ada Existing"
doi: "10.1234/existing"
url: "https://doi.org/10.1234/existing"
publication_year: 2024
journal: "Journal"
pip_litdb_status: verified
"""


def candidate(doi: str = "10.1234/new") -> Candidate:
    return Candidate(
        "A pyrrole-imidazole polyamide paper",
        [Author("Ada Example")],
        doi=doi,
        url=f"https://doi.org/{doi}",
        journal="Journal of PIP Studies",
        document_type="research_article",
        publication_stage="publication",
        dates=[DateValue(date(2025, 2, 3), "online", "crossref")],
        score=8,
        disposition="new",
        discovered_by={"crossref": doi},
    )


class RecordTests(unittest.TestCase):
    def test_allocation_is_deterministic_and_monotonic(self) -> None:
        allocated = allocate_records([candidate("10.1234/z"), candidate("10.1234/a")], start_after=513)
        self.assertEqual([(item[0], item[1].doi) for item in allocated], [("00514", "10.1234/a"), ("00515", "10.1234/z")])

    def test_yaml_uses_canonical_field_order_and_omits_blanks(self) -> None:
        content = allocate_records([candidate()], start_after=1)[0][2]
        self.assertLess(content.index("document_type"), content.index("title"))
        self.assertNotIn("related_papers", content)
        self.assertIn('pip_litdb_status: "needs_review"', content)

    def test_publication_record_year_prefers_print_over_online_year(self) -> None:
        value = candidate()
        value.dates = [
            DateValue(date(2024, 12, 15), "online", "crossref"),
            DateValue(date(2025, 2, 1), "print", "crossref"),
        ]
        content = allocate_records([value], start_after=1)[0][2]
        self.assertIn("publication_year: 2025", content)

    def test_write_records_runs_temporary_and_ephemeral_commit_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "database" / "schema", root / "database" / "schema")
            shutil.copytree(ROOT / "database" / "vocabularies", root / "database" / "vocabularies")
            (root / "database" / "records").mkdir()
            (root / "database" / "records" / "00001.yaml").write_text(VALID_RECORD, encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "base"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
            )
            generated = write_records(root, [candidate()], base="HEAD")
            self.assertEqual(generated, {"00002": "doi:10.1234/new"})
            self.assertTrue((root / "database" / "records" / "00002.yaml").is_file())
            status = subprocess.run(["git", "status", "--short"], cwd=root, check=True, stdout=subprocess.PIPE, text=True).stdout
            self.assertIn("?? database/records/00002.yaml", status)

    def test_explicit_preprint_relationship_updates_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "database" / "schema", root / "database" / "schema")
            shutil.copytree(ROOT / "database" / "vocabularies", root / "database" / "vocabularies")
            (root / "database" / "records").mkdir()
            (root / "database" / "records" / "00001.yaml").write_text(VALID_RECORD, encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "base"],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
            )
            preprint = candidate("10.1234/preprint")
            preprint.publication_stage = "preprint"
            preprint.relationships = [{"type": "is_preprint_of", "doi": "10.1234/existing"}]
            preprint.disposition = "related_version"
            write_records(root, [preprint], base="HEAD")
            existing = (root / "database" / "records" / "00001.yaml").read_text(encoding="utf-8")
            generated = (root / "database" / "records" / "00002.yaml").read_text(encoding="utf-8")
            self.assertIn('relationship_type: "has_preprint"', existing)
            self.assertIn('relationship_type: "is_preprint_of"', generated)


if __name__ == "__main__":
    unittest.main()
