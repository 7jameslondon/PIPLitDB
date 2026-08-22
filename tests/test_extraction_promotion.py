from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.discovery import discover_sources, source_fingerprint
from scripts.extraction.paths import atomic_write_json, sha256_file
from scripts.extraction.promotion import PromotionError, promote_extraction
from scripts.extraction.reporting import _pipeline_code_sha256, write_validation_result
from scripts.extraction.validation import ValidationReport, validate_candidate


@dataclass(frozen=True)
class SyntheticCandidate:
    root: Path
    record_root: Path
    run_root: Path
    extraction: Path
    diagnostic: Path
    source_fingerprint: str
    report: ValidationReport


class ExtractionPromotionTests(unittest.TestCase):
    record_id = "00001"
    run_id = "pilot-synthetic"
    title = "Synthetic promotion article"

    def _make_candidate(
        self,
        directory: str,
        *,
        warning_codes: tuple[str, ...] = (
            "publisher_limitation",
            "alignment_pending",
        ),
        with_override: bool = False,
    ) -> SyntheticCandidate:
        root = Path(directory)
        metadata_root = root / "database" / "records"
        record_root = root / "papers (private)" / self.record_id
        html_root = record_root / "html"
        staging_root = root / "papers (private)" / "staging"
        run_root = staging_root / self.record_id / self.run_id
        extraction = run_root / "extraction"
        diagnostic = run_root / "extraction_diagnostic"
        for path in (metadata_root, html_root, extraction, diagnostic):
            path.mkdir(parents=True, exist_ok=True)

        (metadata_root / f"{self.record_id}.yaml").write_text(
            "\n".join(
                (
                    "document_type: research_article",
                    "publication_stage: publication",
                    f'title: "{self.title}"',
                    "authors:",
                    '  - name: "Synthetic Author"',
                    'doi: "10.0000/synthetic"',
                    "publication_year: 2020",
                    'journal: "Synthetic Journal"',
                    "language_status: english",
                    "pip_litdb_file_status:",
                    "  main_pdf: missing",
                    "  supplementary_material: missing",
                    "  full_text_html: present",
                    "pip_litdb_status: partial",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (html_root / "main.html").write_text(
            "<!doctype html><html><body><article>Synthetic source.</article></body></html>\n",
            encoding="utf-8",
        )

        override_hash: str | None = None
        override_text: str | None = None
        if with_override:
            override_path = record_root / "extraction_overrides.yaml"
            override_text = (
                'schema_version: "1.0"\n'
                f'record_id: "{self.record_id}"\n'
                "text_repairs: []\n"
            )
            override_path.write_text(override_text, encoding="utf-8")
            override_hash = sha256_file(override_path)

        sources = discover_sources(root, self.record_id)
        fingerprint = source_fingerprint(sources, override_hash)
        record_text = f"# {self.title}\n\nSynthetic body.\n"
        (extraction / "record.md").write_text(record_text, encoding="utf-8")
        self._write_manifest(extraction, diagnostic, fingerprint=fingerprint)
        atomic_write_json(
            diagnostic / "sources.json",
            {
                "schema_version": "1.0",
                "record_id": self.record_id,
                "source_fingerprint": fingerprint,
                "sources": [source.as_dict() for source in sources],
            },
        )
        (diagnostic / "coverage.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "coverage_id": "synthetic-record",
                    "content_kind": "article",
                    "source_path": (
                        f"papers (private)/{self.record_id}/html/main.html"
                    ),
                    "source_locator": "entire synthetic source",
                    "status": "included",
                    "output_path": "record.md",
                    "output_locator": {"start_line": 1, "end_line": 3},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        atomic_write_json(
            diagnostic / "quality.json",
            {
                "schema_version": "1.0",
                "record_id": self.record_id,
                "status": "needs_review" if warning_codes else "draft",
                "warnings": len(warning_codes),
                "validation": "not_run",
            },
        )
        atomic_write_json(
            diagnostic / "confidence.json",
            {
                "schema_version": "1.0",
                "categories": {
                    "synthetic": {
                        "level": "medium" if warning_codes else "high",
                        "basis": "Synthetic promotion fixture.",
                    }
                },
            },
        )
        (diagnostic / "warnings.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "code": code,
                        "severity": "structural",
                        "message": f"Synthetic accepted finding: {code}.",
                    },
                    sort_keys=True,
                )
                + "\n"
                for code in warning_codes
            ),
            encoding="utf-8",
        )
        if override_text is not None:
            (diagnostic / "overrides.yaml").write_text(
                override_text, encoding="utf-8"
            )

        report = self._write_current_validation(extraction, diagnostic)
        self.assertEqual(
            {finding.code for finding in report.findings},
            {f"diagnostic_warning_{code}" for code in warning_codes},
        )
        return SyntheticCandidate(
            root=root,
            record_root=record_root,
            run_root=run_root,
            extraction=extraction,
            diagnostic=diagnostic,
            source_fingerprint=fingerprint,
            report=report,
        )

    def _write_manifest(
        self,
        extraction: Path,
        diagnostic: Path,
        *,
        fingerprint: str,
    ) -> None:
        files = [
            {
                "path": path.relative_to(extraction).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(
                (candidate for candidate in extraction.rglob("*") if candidate.is_file()),
                key=lambda candidate: candidate.relative_to(extraction).as_posix(),
            )
        ]
        atomic_write_json(
            diagnostic / "manifest.json",
            {
                "schema_version": "1.0",
                "record_id": self.record_id,
                "run_id": self.run_id,
                "source_fingerprint": fingerprint,
                "pipeline_code_sha256": _pipeline_code_sha256(),
                "pipeline": {"pipeline": "synthetic-test"},
                "ocr_performed": False,
                "files": files,
                "assets": [],
            },
        )

    def _write_current_validation(
        self, extraction: Path, diagnostic: Path
    ) -> ValidationReport:
        report = validate_candidate(
            extraction,
            diagnostic,
            expected_title=self.title,
        )
        write_validation_result(diagnostic, report.findings, dict(report.counts))
        return report

    @staticmethod
    def _finding_codes(candidate: SyntheticCandidate) -> list[str]:
        validation = json.loads(
            (candidate.diagnostic / "validation.json").read_text(encoding="utf-8")
        )
        return [finding["code"] for finding in validation["findings"]]

    def test_refuses_a_collision_at_either_live_destination(self) -> None:
        for destination_name in ("extraction", "extraction_diagnostic"):
            with self.subTest(destination_name=destination_name), TemporaryDirectory() as directory:
                candidate = self._make_candidate(directory)
                destination = candidate.record_root / destination_name
                destination.mkdir()
                sentinel = destination / "do-not-overwrite.txt"
                sentinel.write_text("existing live output", encoding="utf-8")

                with self.assertRaisesRegex(PromotionError, "already exists|collision"):
                    promote_extraction(
                        candidate.root,
                        self.record_id,
                        run_id=self.run_id,
                        accepted_findings=self._finding_codes(candidate),
                    )

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing live output")
                self.assertTrue(candidate.run_root.is_dir())

    def test_rejects_stale_source_and_stale_override(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            source = candidate.record_root / "html" / "main.html"
            source.write_text("changed after extraction\n", encoding="utf-8")

            with self.assertRaisesRegex(PromotionError, "source|fingerprint|stale"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                )

        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory, with_override=True)
            override = candidate.record_root / "extraction_overrides.yaml"
            override.write_text(
                override.read_text(encoding="utf-8") + "source_anomalies: []\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PromotionError, "override|fingerprint|stale"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                )

    def test_rejects_candidate_built_with_stale_pipeline_code(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            manifest_path = candidate.diagnostic / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["pipeline_code_sha256"] = "0" * 64
            atomic_write_json(manifest_path, manifest)

            with self.assertRaisesRegex(PromotionError, "stale.*pipeline|pipeline.*stale"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                )

    def test_hard_manifest_hash_and_link_errors_cannot_be_accepted(self) -> None:
        cases = ("invalid_manifest_path", "output_hash_mismatch", "missing_local_link")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as directory:
                candidate = self._make_candidate(directory, warning_codes=())
                if case == "invalid_manifest_path":
                    manifest_path = candidate.diagnostic / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["files"].append(
                        {"path": "../escaped.bin", "sha256": "0" * 64, "bytes": 1}
                    )
                    atomic_write_json(manifest_path, manifest)
                elif case == "output_hash_mismatch":
                    (candidate.extraction / "record.md").write_text(
                        f"# {self.title}\n\nChanged after manifest creation.\n",
                        encoding="utf-8",
                    )
                else:
                    (candidate.extraction / "record.md").write_text(
                        f"# {self.title}\n\n[Missing asset](assets/missing.png)\n",
                        encoding="utf-8",
                    )
                    self._write_manifest(
                        candidate.extraction,
                        candidate.diagnostic,
                        fingerprint=candidate.source_fingerprint,
                    )

                report = self._write_current_validation(
                    candidate.extraction, candidate.diagnostic
                )
                codes = [finding.code for finding in report.findings]
                self.assertIn(case, codes)

                with self.assertRaisesRegex(PromotionError, "integrity|unsafe|cannot.*accept|blocking"):
                    promote_extraction(
                        candidate.root,
                        self.record_id,
                        run_id=self.run_id,
                        accepted_findings=codes,
                    )
                self.assertFalse((candidate.record_root / "extraction").exists())
                self.assertFalse(
                    (candidate.record_root / "extraction_diagnostic").exists()
                )

    def test_requires_the_exact_set_of_current_finding_codes(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            codes = self._finding_codes(candidate)
            self.assertEqual(len(codes), 2)

            for accepted in ((), (codes[0],), (*codes, "not_a_current_finding")):
                with self.subTest(accepted=accepted):
                    with self.assertRaisesRegex(PromotionError, "finding|accept"):
                        promote_extraction(
                            candidate.root,
                            self.record_id,
                            run_id=self.run_id,
                            accepted_findings=accepted,
                        )
                    self.assertFalse((candidate.record_root / "extraction").exists())
                    self.assertTrue(candidate.run_root.is_dir())

    def test_reviewed_unresolved_coverage_can_be_explicitly_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory, warning_codes=())
            coverage_path = candidate.diagnostic / "coverage.jsonl"
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            coverage["status"] = "unresolved"
            coverage["reason"] = "Synthetic publisher omission reviewed by the user."
            coverage_path.write_text(
                json.dumps(coverage, sort_keys=True) + "\n", encoding="utf-8"
            )
            report = self._write_current_validation(
                candidate.extraction, candidate.diagnostic
            )
            self.assertEqual(
                [finding.code for finding in report.findings],
                ["unresolved_coverage"],
            )

            promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=["unresolved_coverage"],
            )

            quality = json.loads(
                (
                    candidate.record_root
                    / "extraction_diagnostic"
                    / "quality.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(quality["status"], "approved")
            self.assertEqual(quality["validation"], "accepted_with_findings")

    def test_promotes_both_directories_preserves_stage_and_records_approval(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            codes = self._finding_codes(candidate)
            staged_validation = (candidate.diagnostic / "validation.json").read_bytes()
            staged_manifest_hash = sha256_file(candidate.diagnostic / "manifest.json")

            result = promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=reversed(codes),
            )

            live_extraction = candidate.record_root / "extraction"
            live_diagnostic = candidate.record_root / "extraction_diagnostic"
            self.assertTrue(live_extraction.is_dir())
            self.assertTrue(live_diagnostic.is_dir())
            self.assertTrue(candidate.extraction.is_dir())
            self.assertTrue(candidate.diagnostic.is_dir())
            self.assertEqual(
                (live_extraction / "record.md").read_bytes(),
                (candidate.extraction / "record.md").read_bytes(),
            )
            self.assertEqual(
                (live_diagnostic / "validation.json").read_bytes(), staged_validation
            )
            self.assertEqual(
                (candidate.diagnostic / "validation.json").read_bytes(), staged_validation
            )
            self.assertFalse((candidate.diagnostic / "approval.json").exists())

            approval = json.loads(
                (live_diagnostic / "approval.json").read_text(encoding="utf-8")
            )
            validation = json.loads(staged_validation.decode("utf-8"))
            quality = json.loads(
                (live_diagnostic / "quality.json").read_text(encoding="utf-8")
            )
            staged_quality = json.loads(
                (candidate.diagnostic / "quality.json").read_text(encoding="utf-8")
            )
            self.assertEqual(approval["schema_version"], "1.0")
            self.assertEqual(approval["record_id"], self.record_id)
            self.assertEqual(approval["run_id"], self.run_id)
            self.assertEqual(approval["status"], "approved")
            self.assertEqual(approval["source_fingerprint"], candidate.source_fingerprint)
            self.assertEqual(approval["candidate_manifest_sha256"], staged_manifest_hash)
            self.assertEqual(approval["accepted_finding_codes"], sorted(codes))
            self.assertEqual(approval["accepted_findings"], validation["findings"])
            self.assertTrue(approval["approved_at"])
            self.assertEqual(quality["status"], "approved")
            self.assertEqual(quality["validation"], "accepted_with_findings")
            self.assertEqual(quality["approval"], "approval.json")
            self.assertEqual(quality["accepted_finding_codes"], sorted(codes))
            self.assertEqual(quality["accepted_finding_count"], len(codes))
            self.assertEqual(staged_quality["status"], "needs_review")
            self.assertEqual(result.record_id, self.record_id)
            self.assertEqual(result.run_id, self.run_id)

    def test_second_rename_failure_rolls_back_the_first_destination(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            rename_calls: list[tuple[Path, Path]] = []

            def fail_second_rename(source: Path, destination: Path) -> None:
                rename_calls.append((Path(source), Path(destination)))
                if len(rename_calls) == 2:
                    raise OSError("synthetic second rename failure")
                os.replace(source, destination)

            with self.assertRaisesRegex(PromotionError, "synthetic second rename failure|rollback"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                    _rename=fail_second_rename,
                )

            self.assertGreaterEqual(len(rename_calls), 2)
            self.assertFalse((candidate.record_root / "extraction").exists())
            self.assertFalse((candidate.record_root / "extraction_diagnostic").exists())
            self.assertTrue((candidate.extraction / "record.md").is_file())
            self.assertFalse((candidate.diagnostic / "approval.json").exists())

    def test_record_and_run_identifiers_cannot_escape_or_use_unsafe_names(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            accepted = self._finding_codes(candidate)
            invalid_record_ids = ("1", "000001", "../01", "00/01", "abcde")
            invalid_run_ids = (
                "",
                ".",
                "..",
                ".hidden",
                "-switch",
                "../escape",
                "run/escape",
                "run\\escape",
                "C:escape",
                "run.",
                "CON",
                "con.txt",
                "COM1",
                "LPT9.log",
                "a" * 65,
            )

            for record_id in invalid_record_ids:
                with self.subTest(record_id=record_id):
                    with self.assertRaises((PromotionError, ValueError)):
                        promote_extraction(
                            candidate.root,
                            record_id,
                            run_id=self.run_id,
                            accepted_findings=accepted,
                        )
            for run_id in invalid_run_ids:
                with self.subTest(run_id=run_id):
                    with self.assertRaises((PromotionError, ValueError)):
                        promote_extraction(
                            candidate.root,
                            self.record_id,
                            run_id=run_id,
                            accepted_findings=accepted,
                        )

            self.assertFalse((candidate.record_root / "extraction").exists())
            self.assertFalse((candidate.record_root / "extraction_diagnostic").exists())
            self.assertTrue(candidate.run_root.is_dir())


if __name__ == "__main__":
    unittest.main()
