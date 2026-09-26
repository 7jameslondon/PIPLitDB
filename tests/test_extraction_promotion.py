from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from scripts.extraction.discovery import discover_sources, source_fingerprint
from scripts.extraction.paths import atomic_write_json, sha256_file
from scripts.extraction.finalization import finalize_extraction
from scripts.extraction.promotion import (
    PromotionError,
    STANDING_POLICY_APPROVAL,
    cleanup_approved_staging,
    promote_extraction,
)
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
        override_at_root: bool = True,
        initial_status: str = "partial",
        include_human_notes: bool = False,
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

        metadata_lines = [
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
        ]
        if include_human_notes:
            metadata_lines.extend(
                ("jamies_human_only_notes:", "  - preserve_this_exactly")
            )
        metadata_lines.extend((f"pip_litdb_status: {initial_status}", ""))
        (metadata_root / f"{self.record_id}.yaml").write_text(
            "\n".join(metadata_lines),
            encoding="utf-8",
        )
        (html_root / "main.html").write_text(
            "<!doctype html><html><body><article>Synthetic source.</article></body></html>\n",
            encoding="utf-8",
        )

        override_hash: str | None = None
        override_text: str | None = None
        if with_override:
            override_text = (
                'schema_version: "1.0"\n'
                f'record_id: "{self.record_id}"\n'
                "text_repairs: []\n"
            )
            staged_override = diagnostic / "overrides.yaml"
            staged_override.write_text(override_text, encoding="utf-8")
            override_hash = sha256_file(staged_override)
            if override_at_root:
                (record_root / "extraction_overrides.yaml").write_text(
                    override_text, encoding="utf-8"
                )

        sources = discover_sources(root, self.record_id)
        fingerprint = source_fingerprint(sources, override_hash)
        record_text = f"# {self.title}\n\nSynthetic body.\n"
        (extraction / "record.md").write_text(record_text, encoding="utf-8")
        self._write_manifest(
            extraction,
            diagnostic,
            fingerprint=fingerprint,
            override_hash=override_hash,
        )
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
        override_hash: str | None = None,
        run_id: str | None = None,
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
        manifest = {
                "schema_version": "1.0",
                "record_id": self.record_id,
                "run_id": run_id or self.run_id,
                "source_fingerprint": fingerprint,
                "pipeline_code_sha256": _pipeline_code_sha256(),
                "pipeline": {"pipeline": "synthetic-test"},
                "ocr_performed": False,
                "files": files,
                "assets": [],
            }
        if override_hash is not None:
            override_path = diagnostic / "overrides.yaml"
            manifest.update(
                {
                    "override_snapshot_sha256": override_hash,
                    "override_snapshot_bytes": override_path.stat().st_size,
                }
            )
        atomic_write_json(diagnostic / "manifest.json", manifest)

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

    def _set_public_status(self, candidate: SyntheticCandidate, status: str) -> None:
        metadata_path = (
            candidate.root / "database" / "records" / f"{self.record_id}.yaml"
        )
        text = metadata_path.read_text(encoding="utf-8")
        text = text.replace("pip_litdb_status: partial", f"pip_litdb_status: {status}")
        metadata_path.write_text(text, encoding="utf-8")

    def _add_standing_policy_evidence(
        self,
        candidate: SyntheticCandidate,
        *,
        reproducibility_run_id: str = "pilot-synthetic-repro",
    ) -> str:
        reviews = candidate.diagnostic / "reviews"
        reviews.mkdir()
        role_names = (
            "01_text_and_reading_order.json",
            "02_scientific_notation_equations_tables.json",
            "03_figures_schemes_supplements.json",
            "04_ai_readiness_consistency.json",
            "05_adversarial_completeness.json",
        )
        for role_name in role_names:
            (reviews / role_name).write_text("{}\n", encoding="utf-8")
        codes = self._finding_codes(candidate)
        (reviews / "adjudication.json").write_text(
            json.dumps({"accepted_finding_codes": codes}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        reproducibility_root = (
            candidate.run_root.parent / reproducibility_run_id
        )
        shutil.copytree(candidate.extraction, reproducibility_root / "extraction")
        shutil.copytree(
            candidate.diagnostic,
            reproducibility_root / "extraction_diagnostic",
        )
        manifest_path = (
            reproducibility_root / "extraction_diagnostic" / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["run_id"] = reproducibility_run_id
        atomic_write_json(manifest_path, manifest)
        return reproducibility_run_id

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

    def test_promotes_and_cleans_up_a_diagnostic_only_reviewed_override(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(
                directory,
                with_override=True,
                override_at_root=False,
            )
            self.assertFalse(
                (candidate.record_root / "extraction_overrides.yaml").exists()
            )

            promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=self._finding_codes(candidate),
            )
            live_override = (
                candidate.record_root / "extraction_diagnostic" / "overrides.yaml"
            )
            self.assertEqual(
                live_override.read_bytes(),
                (candidate.diagnostic / "overrides.yaml").read_bytes(),
            )

            self._set_public_status(candidate, "extracted_approved")
            checked = cleanup_approved_staging(
                candidate.root,
                self.record_id,
                remove=False,
            )
            self.assertTrue(checked.checked_only)
            cleanup_approved_staging(candidate.root, self.record_id)
            self.assertFalse(candidate.run_root.parent.exists())
            self.assertTrue(live_override.is_file())

    def test_cleanup_removes_matching_temporary_record_override(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory, with_override=True)
            working_override = candidate.record_root / "extraction_overrides.yaml"

            promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=self._finding_codes(candidate),
            )
            self._set_public_status(candidate, "extracted_approved")

            cleanup_approved_staging(
                candidate.root, self.record_id, remove=False
            )
            self.assertTrue(working_override.is_file())

            cleanup_approved_staging(candidate.root, self.record_id)
            self.assertFalse(working_override.exists())
            self.assertTrue(
                (
                    candidate.record_root
                    / "extraction_diagnostic"
                    / "overrides.yaml"
                ).is_file()
            )

    def test_rejects_an_unbound_diagnostic_only_override(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(
                directory,
                with_override=True,
                override_at_root=False,
            )
            manifest_path = candidate.diagnostic / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("override_snapshot_sha256")
            manifest.pop("override_snapshot_bytes")
            atomic_write_json(manifest_path, manifest)

            with self.assertRaisesRegex(PromotionError, "bound to the manifest"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                )

    def test_post_approval_cleanup_removes_only_the_record_staging_tree(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            sibling = (
                candidate.root
                / "papers (private)"
                / "staging"
                / "00002"
                / "sibling-run"
                / "keep.txt"
            )
            sibling.parent.mkdir(parents=True)
            sibling.write_text("unrelated staging", encoding="utf-8")

            promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=self._finding_codes(candidate),
            )
            self._set_public_status(candidate, "extracted_approved")
            live_record = candidate.record_root / "extraction" / "record.md"
            live_approval = (
                candidate.record_root / "extraction_diagnostic" / "approval.json"
            )

            checked = cleanup_approved_staging(
                candidate.root, self.record_id, remove=False
            )
            self.assertTrue(checked.checked_only)
            self.assertFalse(checked.removed)
            self.assertEqual(checked.removed_run_ids, (self.run_id,))
            self.assertTrue(candidate.run_root.is_dir())

            result = cleanup_approved_staging(candidate.root, self.record_id)

            self.assertTrue(result.removed)
            self.assertEqual(result.approved_run_id, self.run_id)
            self.assertEqual(result.removed_run_ids, (self.run_id,))
            self.assertFalse(candidate.run_root.parent.exists())
            self.assertTrue(live_record.is_file())
            self.assertTrue(live_approval.is_file())
            self.assertEqual(sibling.read_text(encoding="utf-8"), "unrelated staging")

            repeated = cleanup_approved_staging(candidate.root, self.record_id)
            self.assertFalse(repeated.removed)
            self.assertEqual(repeated.removed_run_ids, ())

    def test_post_approval_cleanup_tolerates_disappearing_cache_descendant(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=self._finding_codes(candidate),
            )
            self._set_public_status(candidate, "extracted_approved")
            live_record = candidate.record_root / "extraction" / "record.md"
            live_record_bytes = live_record.read_bytes()
            original_rmtree = shutil.rmtree

            def remove_with_cache_race(path: Path, **kwargs: object) -> None:
                onexc = kwargs.get("onexc")
                self.assertIsNotNone(onexc)
                transient = Path(path) / "profile" / "cache" / "transient.bin"
                transient.parent.mkdir(parents=True)
                transient.write_bytes(b"transient")
                transient.unlink()
                assert callable(onexc)
                onexc(
                    os.unlink,
                    str(transient),
                    FileNotFoundError(2, "synthetic cache race", str(transient)),
                )
                original_rmtree(path)

            with mock.patch(
                "scripts.extraction.promotion.shutil.rmtree",
                side_effect=remove_with_cache_race,
            ):
                result = cleanup_approved_staging(candidate.root, self.record_id)

            self.assertTrue(result.removed)
            self.assertFalse(candidate.run_root.parent.exists())
            self.assertEqual(live_record.read_bytes(), live_record_bytes)

    def test_post_approval_cleanup_does_not_suppress_real_removal_errors(self) -> None:
        for error in (
            PermissionError(13, "synthetic permission failure"),
            OSError(5, "synthetic I/O failure"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                TemporaryDirectory() as directory,
            ):
                candidate = self._make_candidate(directory)
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                )
                self._set_public_status(candidate, "extracted_approved")

                def fail_removal(path: Path, **kwargs: object) -> None:
                    onexc = kwargs.get("onexc")
                    self.assertIsNotNone(onexc)
                    assert callable(onexc)
                    onexc(os.unlink, str(Path(path) / "locked.bin"), error)

                with mock.patch(
                    "scripts.extraction.promotion.shutil.rmtree",
                    side_effect=fail_removal,
                ):
                    with self.assertRaises(type(error)):
                        cleanup_approved_staging(candidate.root, self.record_id)

                self.assertTrue(candidate.run_root.is_dir())
                self.assertTrue((candidate.record_root / "extraction").is_dir())

    def test_post_approval_cleanup_rejects_non_descendant_missing_paths(self) -> None:
        for failed_path_kind in ("root", "sibling"):
            with (
                self.subTest(failed_path_kind=failed_path_kind),
                TemporaryDirectory() as directory,
            ):
                candidate = self._make_candidate(directory)
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    accepted_findings=self._finding_codes(candidate),
                )
                self._set_public_status(candidate, "extracted_approved")

                def fail_removal(path: Path, **kwargs: object) -> None:
                    onexc = kwargs.get("onexc")
                    self.assertIsNotNone(onexc)
                    assert callable(onexc)
                    failed_path = (
                        Path(path)
                        if failed_path_kind == "root"
                        else Path(path).parent / "outside.bin"
                    )
                    onexc(
                        os.unlink,
                        str(failed_path),
                        FileNotFoundError(2, "synthetic missing path", str(failed_path)),
                    )

                with mock.patch(
                    "scripts.extraction.promotion.shutil.rmtree",
                    side_effect=fail_removal,
                ):
                    with self.assertRaises(FileNotFoundError):
                        cleanup_approved_staging(candidate.root, self.record_id)

                self.assertTrue(candidate.run_root.is_dir())
                self.assertTrue((candidate.record_root / "extraction").is_dir())

    def test_post_approval_cleanup_refuses_unapproved_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                accepted_findings=self._finding_codes(candidate),
            )

            with self.assertRaisesRegex(PromotionError, "extracted_approved"):
                cleanup_approved_staging(candidate.root, self.record_id)

            self.assertTrue(candidate.run_root.is_dir())
            self.assertTrue((candidate.record_root / "extraction").is_dir())

    def test_post_approval_cleanup_refuses_missing_live_approval(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory)
            self._set_public_status(candidate, "extracted_approved")

            with self.assertRaisesRegex(PromotionError, "approved live extraction"):
                cleanup_approved_staging(candidate.root, self.record_id)

            self.assertTrue(candidate.run_root.is_dir())

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

    def test_standing_policy_records_truthful_authority_and_cleanup_accepts_it(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(
                directory,
                warning_codes=(
                    "automated_pdf_html_alignment_not_implemented",
                ),
            )
            repro_run_id = self._add_standing_policy_evidence(candidate)

            result = promote_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                approval_mode=STANDING_POLICY_APPROVAL,
                reproducibility_run_id=repro_run_id,
            )

            approval = json.loads(
                (
                    candidate.record_root
                    / "extraction_diagnostic"
                    / "approval.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result.approval_mode, STANDING_POLICY_APPROVAL)
            self.assertEqual(
                approval["approved_by"], "primary_agent_under_standing_policy"
            )
            self.assertEqual(approval["approval_mode"], STANDING_POLICY_APPROVAL)
            self.assertEqual(
                approval["approval_policy"],
                "automatic_after_protocol_finalization",
            )
            self.assertEqual(
                approval["finalization_evidence"]["reproducibility"]["run_id"],
                repro_run_id,
            )

            self._set_public_status(candidate, "extracted_approved")
            cleanup = cleanup_approved_staging(candidate.root, self.record_id)
            self.assertTrue(cleanup.removed)

    def test_standing_policy_blocks_unknown_findings_and_missing_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(
                directory,
                warning_codes=("publisher_limitation",),
            )
            with self.assertRaisesRegex(PromotionError, "new or unresolved"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    approval_mode=STANDING_POLICY_APPROVAL,
                    reproducibility_run_id="unused-repro",
                )

        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(directory, warning_codes=())
            with self.assertRaisesRegex(PromotionError, "review"):
                promote_extraction(
                    candidate.root,
                    self.record_id,
                    run_id=self.run_id,
                    approval_mode=STANDING_POLICY_APPROVAL,
                    reproducibility_run_id="missing-repro",
                )

    def test_finalizer_updates_status_queue_and_removes_staging(self) -> None:
        with TemporaryDirectory() as directory:
            candidate = self._make_candidate(
                directory,
                warning_codes=(),
                initial_status="ready_for_extraction",
                include_human_notes=True,
            )
            repro_run_id = self._add_standing_policy_evidence(candidate)
            queue = candidate.root / "tmp" / "EXTRACTION_QUEUE.md"
            queue.parent.mkdir()
            queue.write_text(
                "# Extraction Queue\n\n1. **`00001`** — Ready for extraction.\n",
                encoding="utf-8",
            )

            result = finalize_extraction(
                candidate.root,
                self.record_id,
                run_id=self.run_id,
                reproducibility_run_id=repro_run_id,
            )

            metadata = (
                candidate.root / "database" / "records" / "00001.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("pip_litdb_status: extracted_approved", metadata)
            self.assertIn(
                "jamies_human_only_notes:\n  - preserve_this_exactly", metadata
            )
            self.assertIn(
                "Extraction completed and approved.",
                queue.read_text(encoding="utf-8"),
            )
            self.assertTrue(result.metadata_changed)
            self.assertTrue(result.queue_changed)
            self.assertTrue(result.staging_removed)
            self.assertFalse(candidate.run_root.parent.exists())

    def test_standing_policy_replacement_archives_the_prior_approved_revision(self) -> None:
        with TemporaryDirectory() as directory:
            original = self._make_candidate(directory, warning_codes=())
            promote_extraction(
                original.root,
                self.record_id,
                run_id=self.run_id,
            )
            self._set_public_status(original, "extracted_approved")
            old_live_bytes = (
                original.record_root / "extraction" / "record.md"
            ).read_bytes()

            replacement_run_id = "replacement-run"
            replacement_root = original.run_root.parent / replacement_run_id
            replacement_extraction = replacement_root / "extraction"
            replacement_diagnostic = replacement_root / "extraction_diagnostic"
            shutil.copytree(original.extraction, replacement_extraction)
            shutil.copytree(original.diagnostic, replacement_diagnostic)
            (replacement_extraction / "record.md").write_text(
                f"# {self.title}\n\nImproved replacement body.\n",
                encoding="utf-8",
            )
            replacement_sources = discover_sources(original.root, self.record_id)
            replacement_fingerprint = source_fingerprint(replacement_sources, None)
            self._write_manifest(
                replacement_extraction,
                replacement_diagnostic,
                fingerprint=replacement_fingerprint,
                run_id=replacement_run_id,
            )
            atomic_write_json(
                replacement_diagnostic / "sources.json",
                {
                    "schema_version": "1.0",
                    "record_id": self.record_id,
                    "source_fingerprint": replacement_fingerprint,
                    "sources": [source.as_dict() for source in replacement_sources],
                },
            )
            report = self._write_current_validation(
                replacement_extraction, replacement_diagnostic
            )
            replacement = SyntheticCandidate(
                root=original.root,
                record_root=original.record_root,
                run_root=replacement_root,
                extraction=replacement_extraction,
                diagnostic=replacement_diagnostic,
                source_fingerprint=replacement_fingerprint,
                report=report,
            )
            repro_run_id = self._add_standing_policy_evidence(
                replacement,
                reproducibility_run_id="replacement-repro",
            )

            result = promote_extraction(
                original.root,
                self.record_id,
                run_id=replacement_run_id,
                approval_mode=STANDING_POLICY_APPROVAL,
                reproducibility_run_id=repro_run_id,
                replace=True,
            )

            self.assertEqual(result.replaced_run_id, self.run_id)
            self.assertIsNotNone(result.archived_previous_root)
            assert result.archived_previous_root is not None
            self.assertEqual(
                (
                    result.archived_previous_root / "extraction" / "record.md"
                ).read_bytes(),
                old_live_bytes,
            )
            self.assertIn(
                "Improved replacement body.",
                (
                    original.record_root / "extraction" / "record.md"
                ).read_text(encoding="utf-8"),
            )
            approval = json.loads(
                (
                    original.record_root
                    / "extraction_diagnostic"
                    / "approval.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                approval["replacement"]["replaced_run_id"], self.run_id
            )

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
