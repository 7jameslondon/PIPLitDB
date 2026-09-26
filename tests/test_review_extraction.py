from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reportlab.pdfgen.canvas import Canvas

from scripts import review_extraction as runner


class ReviewRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / "papers (private)"
        self.record = self.private / "00001"
        (self.record / "pdf").mkdir(parents=True)
        (self.record / "html").mkdir()
        self.pdf = self.record / "pdf/main.pdf"
        canvas = Canvas(str(self.pdf))
        for text in ("Synthetic review article", "Second page: 0.05 mM, -12 and all footnotes."):
            canvas.drawString(50, 700, text)
            canvas.showPage()
        canvas.save()
        (self.record / "html/main.html").write_text(
            '<html><body><article><h1>Synthetic review article</h1>'
            '<h2>Results</h2><p>Every value: 0.05 mM and -12.</p></article></body></html>')
        metadata = self.root / "database/records/00001.yaml"
        metadata.parent.mkdir(parents=True)
        metadata.write_text('title: Synthetic review article\nauthors: [Test Author]\n'
                            'journal: Synthetic Journal\npublication_year: 2020\n'
                            'doi: 10.0000/synthetic\ndocument_type: research_article\n'
                            'pip_litdb_status: ready_for_extraction\n'
                            'jamies_human_only_notes: [human_tag_exact]\n')
        self.metadata = metadata
        self.before = metadata.read_bytes()
        (self.root / "extraction_viewer.html").write_text("synthetic viewer")

    def prepare(self, name="r01", **kwargs):
        result = runner.prepare(self.root, "00001", name, **kwargs)
        return result, Path(result["receipt"])

    def test_prepare_preserves_every_page_and_lossless_candidate(self):
        result, receipt_path = self.prepare()
        receipt = runner._json(receipt_path)
        canonical = runner._json(Path(receipt["candidate"]["path"]))
        self.assertEqual(canonical, runner._json(Path(result["reading_view"])))
        index = runner._json(Path(result["source_index"]))["sources"]
        pdf = next(row for row in index if row["format"] == "application/pdf")
        self.assertEqual(len(pdf["views"]), 2)
        views = Path(result["source_index"]).parent
        native = runner._json(views / pdf["native_text"])
        self.assertIn("all footnotes", native["pages"][1]["text"])
        self.assertTrue(all((views / name).stat().st_size > 0 for name in pdf["views"]))
        self.assertEqual(self.before, self.metadata.read_bytes())
        self.assertFalse((self.record / "extraction").exists())
        self.assertEqual(receipt["scientific_review"], "pending")
        self.assertLess(len(json.dumps(result)), 3000)

    def test_existing_candidate_is_never_overwritten(self):
        _, receipt = self.prepare()
        before = runner._json(receipt)["candidate"]
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()
        self.assertEqual(runner.completion._file(Path(before["path"])), before)

    def test_reuse_requires_exact_sources_settings_and_saved_artifacts(self):
        _, receipt = self.prepare()
        sources = Path(runner._json(receipt)["source_receipt"]["path"])
        _, second = self.prepare("r02", reuse_sources=sources)
        self.assertEqual(runner._json(second)["source_receipt"]["path"], str(sources))
        with self.assertRaisesRegex(ValueError, "different source bytes"):
            self.prepare("r03", reuse_sources=sources, page_dpi=160)
        (sources.parent / "index.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "views changed"):
            self.prepare("r04", reuse_sources=sources)

    def test_changed_source_cannot_reuse_preparation(self):
        _, receipt = self.prepare()
        sources = Path(runner._json(receipt)["source_receipt"]["path"])
        with (self.record / "html/main.html").open("a") as stream:
            stream.write("Changed source")
        with self.assertRaisesRegex(ValueError, "different source bytes"):
            self.prepare("r02", reuse_sources=sources)

    def test_changed_candidate_blocks_freeze_before_tests_or_browser(self):
        _, receipt = self.prepare()
        candidate = Path(runner._json(receipt)["candidate"]["path"])
        candidate.write_text("{}")
        with patch.object(runner, "_tests") as tests, patch.object(runner, "_staged_viewer") as browser:
            with self.assertRaisesRegex(ValueError, "candidate changed"):
                runner.freeze(self.root, "00001", "r01")
        tests.assert_not_called()
        browser.assert_not_called()

    def frozen(self):
        self.prepare()
        with patch.object(runner, "_tests", return_value={"counts": {"passed": 1}}), \
             patch.object(runner, "_staged_viewer", return_value=({"report": {"path": "synthetic"}}, {})):
            return runner.freeze(self.root, "00001", "r01")

    def test_freeze_records_pending_judgment_and_does_not_create_reviews(self):
        frozen = self.frozen()
        receipt = runner._json(Path(frozen["freeze"]))
        self.assertEqual(receipt["scientific_review"], "pending")
        self.assertEqual(receipt["visual_inspection"], "pending")
        self.assertFalse((self.private / "staging/00001/r01/extraction_diagnostic/reviews").exists())

    def test_code_change_after_freeze_blocks_handoff(self):
        frozen = self.frozen()
        (self.root / "tests").mkdir()
        (self.root / "tests/new.py").write_text("# changed runtime\n")
        with self.assertRaisesRegex(ValueError, "code or inputs changed"):
            runner.handoff(self.root, "00001", "r01", freeze_path=Path(frozen["freeze"]), repro_run_id="clean")
        self.assertFalse((self.private / "staging/00001/clean").exists())

    def test_test_failure_does_not_write_freeze_or_run_viewer(self):
        self.prepare()
        with patch.object(runner, "_tests", side_effect=ValueError("full tests did not pass")), \
             patch.object(runner, "_staged_viewer") as browser:
            with self.assertRaisesRegex(ValueError, "tests did not pass"):
                runner.freeze(self.root, "00001", "r01")
        browser.assert_not_called()
        self.assertEqual(list((self.private / "diagnostics/review-runner").rglob("freeze.json")), [])

    def test_staged_viewer_allows_asset_heavy_records_to_finish(self):
        run = self.private / "staging/00001/r01"
        output = self.private / "diagnostics/review-runner/00001/r01/freeze-test"
        output.mkdir(parents=True)
        (output / "viewer").mkdir()
        with patch.object(runner.completion, "_node_executable", return_value="node"), \
             patch.object(runner.completion, "_viewer", return_value={"status": "passed"}) as viewer, \
             patch.object(runner, "_files", return_value={}):
            runner._staged_viewer(self.root, "00001", run, output, "msedge")
        self.assertEqual(viewer.call_args.args[3], runner.STAGED_VIEWER_TIMEOUT_SECONDS)
        self.assertEqual(runner.STAGED_VIEWER_TIMEOUT_SECONDS, 600)

    def complete_fixture(self, with_reviews=True):
        self.prepare()

        def tests(root, output):
            output.mkdir()
            log = output / "unittest.log"
            log.write_text("Synthetic full-suite receipt fixture.\n")
            report = output / "report.json"
            runner.atomic_write_json(report, {
                "status": "passed", "exit_code": 0, "issues": [],
                "counts": {"tests_run": 1, "passed": 1, "skipped": 0, "failures": 0,
                           "errors": 0, "expected_failures": 0, "unexpected_successes": 0},
                "command": ["python", "run_tests.py", "--worker", str(report), "discover", "-s", "tests", "-v"],
                "working_directory": str(root), "log": runner.completion._file(log)})
            return runner.completion._test_evidence(report, self.private)

        def viewer(root, record_id, run, output, channel):
            directory = output / "viewer"
            directory.mkdir(parents=True)
            report = directory / "report.json"
            report.write_text('{"status":"passed","fixture":true}')
            return {"report": runner.completion._file(report)}, runner._files(output, directory)

        with patch.object(runner, "_tests", side_effect=tests), \
             patch.object(runner, "_staged_viewer", side_effect=viewer):
            frozen = runner.freeze(self.root, "00001", "r01")
        if with_reviews:
            reviews = self.private / "staging/00001/r01/extraction_diagnostic/reviews"
            reviews.mkdir()
            for name in ("text_reading", "scientific_notation", "figures", "ai_readiness", "adversarial", "adjudication"):
                (reviews / f"{name}.md").write_text(
                    "Synthetic self-review test fixture. "
                    "diagnostic_warning_automated_pdf_html_alignment_not_implemented\n")
        return Path(frozen["freeze"])

    def test_handoff_reproduces_canonical_bytes_but_never_approves(self):
        frozen = self.complete_fixture()
        result = runner.handoff(self.root, "00001", "r01", freeze_path=frozen, repro_run_id="clean")
        self.assertEqual(result["status"], "machine_checks_passed")
        self.assertEqual(result["primary_source_and_visual_judgment"], "required")
        self.assertFalse(result["promoted"])
        self.assertEqual(self.before, self.metadata.read_bytes())
        self.assertFalse((self.record / "extraction").exists())
        self.assertFalse((self.record / "extraction_diagnostic/approval.json").exists())
        self.assertEqual(runner.promotion._tree_snapshot(self.private / "staging/00001/r01/extraction"),
                         runner.promotion._tree_snapshot(self.private / "staging/00001/clean/extraction"))

    def test_missing_actual_review_reports_blocks_handoff(self):
        frozen = self.complete_fixture(with_reviews=False)
        with self.assertRaisesRegex(RuntimeError, "reviews directory"):
            runner.handoff(self.root, "00001", "r01", freeze_path=frozen, repro_run_id="clean")
        self.assertFalse((self.private / "staging/00001/clean").exists())

    def test_changed_viewer_evidence_blocks_handoff(self):
        frozen = self.complete_fixture()
        receipt = runner._json(frozen)
        Path(receipt["viewer"]["report"]["path"]).write_text("altered")
        with self.assertRaisesRegex(ValueError, "viewer evidence changed"):
            runner.handoff(self.root, "00001", "r01", freeze_path=frozen, repro_run_id="clean")
        self.assertFalse((self.private / "staging/00001/clean").exists())

    def test_unsafe_record_and_run_ids_are_rejected_before_writes(self):
        for record, run in (("../00001", "r01"), ("00001", "../escape"), ("00001", "NUL")):
            with self.assertRaises(ValueError):
                runner.prepare(self.root, record, run)
        self.assertFalse((self.private / "diagnostics").exists())


if __name__ == "__main__":
    unittest.main()
