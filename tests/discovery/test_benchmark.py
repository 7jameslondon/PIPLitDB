from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.discovery.benchmark import BenchmarkPaper, evaluate_window, load_manifest, render_backtest_reports
from scripts.discovery.dates import DateWindow
from scripts.discovery.models import Candidate


class BenchmarkTests(unittest.TestCase):
    def test_unrecorded_placeholder_cannot_masquerade_as_baseline(self) -> None:
        path = Path(__file__).resolve().parent / "benchmark" / "known-papers.yaml"
        with self.assertRaisesRegex(ValueError, "not been recorded"):
            load_manifest(path)

    def test_manifest_requires_temporal_split_and_exact_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.yaml"
            path.write_text(
                "version: 1\nstatus: recorded\nas_of: 2025-12-31\npapers:\n"
                "  - pip_litdb_id: '00514'\n    doi: 10.1234/a\n    canonical_date: 2025-02-03\n"
                "    date_kind: online\n    date_source: Crossref\n    publication_stage: publication\n"
                "    union_eligible: true\n    source_eligibility: {crossref: 10.1234/a}\n    evaluation_split: holdout\n",
                encoding="utf-8",
            )
            as_of, papers = load_manifest(path)
            self.assertEqual(as_of, date(2025, 12, 31))
            self.assertEqual(papers[0].evaluation_split, "holdout")

    def test_evaluation_keeps_novel_candidates_separate_from_misses(self) -> None:
        paper = BenchmarkPaper("00514", "10.1234/a", date(2025, 2, 3), "online", "Crossref", "publication", True, {"crossref": "10.1234/a"}, "holdout")
        recovered = Candidate("Recovered", doi="10.1234/a", discovered_by={"crossref": "10.1234/a"})
        novel = Candidate("Novel", doi="10.1234/new", discovered_by={"openalex": "W1"})
        result = evaluate_window(DateWindow(date(2025, 1, 1), date(2025, 6, 30)), [paper], [recovered, novel], sources=["crossref", "openalex"], full_database_existing=1)
        self.assertEqual(result.recovered_count, 1)
        self.assertEqual(result.novel_identifiers, ["doi:10.1234/new"])

    def test_completed_window_metrics_do_not_double_count_current_window(self) -> None:
        first = evaluate_window(DateWindow(date(2025, 1, 1), date(2025, 6, 30), "completed"), [], [], sources=[], full_database_existing=0)
        current = evaluate_window(DateWindow(date(2025, 2, 1), date(2025, 8, 1), "current"), [], [], sources=[], full_database_existing=0)
        payload, markdown, csv_text = render_backtest_reports([first, current], as_of=date(2025, 8, 1), config_digest="a" * 64, run_id="run")
        self.assertEqual(payload["overall"]["completed_window_hidden"], 0)
        self.assertIn("retrospective", markdown)
        self.assertEqual(len(csv_text.splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
