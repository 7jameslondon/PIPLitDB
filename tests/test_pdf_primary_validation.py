from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.validation import (
    _validate_coverage,
    _validate_pdf_block_provenance,
    _validate_pdf_page_analysis,
    validate_candidate,
)


PDF_PATH = "pdf/main.pdf"


def _summary(
    page: int,
    *,
    classification: object = "native_text",
    ocr_performed: object = False,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": "page_summary",
        "source_path": PDF_PATH,
        "page": page,
        "classification": classification,
        "ocr_performed": ocr_performed,
    }


def _loaded() -> dict[str, object]:
    return {
        "manifest.json": {
            "text_extraction": {
                "source_role": "main_pdf",
                "source_path": PDF_PATH,
                "ocr_performed": True,
            },
            "page_analysis": "page_analysis.jsonl",
            "ocr_performed": True,
        },
        "sources.json": {
            "sources": [
                {
                    "role": "main_pdf",
                    "path": PDF_PATH,
                    "page_count": 2,
                }
            ]
        },
        "page_analysis.jsonl": [
            _summary(1),
            _summary(2, classification="image_only", ocr_performed=True),
        ],
    }


class PdfPrimaryPageValidationTests(unittest.TestCase):
    def test_complete_contiguous_pdf_page_ledger_passes(self) -> None:
        self.assertEqual(_validate_pdf_page_analysis(_loaded()), [])

    def test_html_primary_manifest_is_unchanged(self) -> None:
        loaded = {
            "manifest.json": {
                "text_extraction": {"source_role": "main_html"},
                "page_analysis": "wrong.jsonl",
                "ocr_performed": "not-a-boolean",
            },
            "sources.json": {"sources": []},
        }

        self.assertEqual(_validate_pdf_page_analysis(loaded), [])

    def test_rejects_missing_reference_source_and_page_analysis(self) -> None:
        loaded = {
            "manifest.json": {
                "text_extraction": {
                    "source_role": "main_pdf",
                    "source_path": PDF_PATH,
                    "ocr_performed": False,
                },
                "ocr_performed": False,
            },
            "sources.json": {"sources": []},
        }

        codes = {finding.code for finding in _validate_pdf_page_analysis(loaded)}
        self.assertEqual(
            codes,
            {
                "pdf_main_source_invalid",
                "pdf_page_analysis_missing",
                "pdf_page_analysis_reference_invalid",
            },
        )

    def test_rejects_malformed_duplicate_missing_and_unexpected_summaries(self) -> None:
        loaded = _loaded()
        loaded["manifest.json"]["ocr_performed"] = False  # type: ignore[index]
        loaded["manifest.json"]["text_extraction"]["ocr_performed"] = False  # type: ignore[index]
        loaded["page_analysis.jsonl"] = [
            _summary(1, classification="unclassified"),
            _summary(1, ocr_performed="false"),
            _summary(3, ocr_performed=True),
            {
                "kind": "glyph_decoding",
                "status": "unresolved",
                "font": "SyntheticFont",
                "page": 1,
            },
        ]

        codes = {finding.code for finding in _validate_pdf_page_analysis(loaded)}
        self.assertTrue(
            {
                "pdf_ocr_summary_mismatch",
                "pdf_page_summary_duplicate",
                "pdf_page_summary_malformed",
                "pdf_page_summary_missing",
                "pdf_page_summary_unexpected",
                "pdf_unresolved_glyph",
            }.issubset(codes)
        )

    def test_rejects_nonboolean_manifest_ocr_flags(self) -> None:
        loaded = _loaded()
        loaded["manifest.json"]["ocr_performed"] = 1  # type: ignore[index]
        loaded["manifest.json"]["text_extraction"]["ocr_performed"] = None  # type: ignore[index]

        findings = _validate_pdf_page_analysis(loaded)
        self.assertEqual(
            [finding.code for finding in findings].count(
                "pdf_manifest_ocr_flag_invalid"
            ),
            2,
        )

    def test_exact_pdf_block_and_heading_provenance_passes(self) -> None:
        loaded = _loaded()
        locator = (
            "PDF pages 1, 2; exact per-line page/bbox geometry is stored "
            "in source_geometry"
        )
        geometry = [
            {"page": 1, "bbox": [10.0, 20.0, 100.0, 30.0]},
            {"page": 2, "bbox": [10.0, 40.0, 100.0, 50.0]},
        ]
        loaded["blocks.jsonl"] = [
            {
                "block_id": "main-paragraph-0001",
                "source_path": PDF_PATH,
                "source_locator": locator,
                "source_geometry": geometry,
            }
        ]
        loaded["coverage.jsonl"] = [
            {
                "coverage_id": "heading-results",
                "content_kind": "section_heading",
                "status": "included",
                "output_id": "section-results",
                "source_path": PDF_PATH,
                "source_locator": "PDF page 1, box [10.00, 10.00, 100.00, 15.00]",
                "source_geometry": [
                    {"page": 1, "bbox": [10.0, 10.0, 100.0, 15.0]}
                ],
            },
            {
                "coverage_id": "content-main-paragraph-0001",
                "content_kind": "paragraph",
                "status": "included",
                "output_id": "main-paragraph-0001",
                "source_path": PDF_PATH,
                "source_locator": locator,
                "source_geometry": geometry,
            },
        ]

        self.assertEqual(_validate_pdf_block_provenance(loaded), [])

    def test_rejects_false_multpage_geometry_claim_and_heading_source(self) -> None:
        loaded = _loaded()
        old_locator = (
            "PDF pages 1-2; per-line geometry is recorded in "
            "extraction_diagnostic/page_analysis.jsonl"
        )
        loaded["blocks.jsonl"] = [
            {
                "block_id": "main-paragraph-0001",
                "source_path": PDF_PATH,
                "source_locator": old_locator,
            }
        ]
        loaded["coverage.jsonl"] = [
            {
                "coverage_id": "heading-experimental-part",
                "content_kind": "section_heading",
                "status": "included",
                "output_id": "section-experimental-part",
                "source_path": "generated",
                "source_locator": "section-experimental-part",
            },
            {
                "coverage_id": "content-main-paragraph-0001",
                "content_kind": "paragraph",
                "status": "included",
                "output_id": "main-paragraph-0001",
                "source_path": PDF_PATH,
                "source_locator": old_locator,
            },
        ]

        codes = {
            finding.code for finding in _validate_pdf_block_provenance(loaded)
        }
        self.assertTrue(
            {
                "pdf_block_geometry_missing",
                "pdf_block_locator_unsupported",
                "pdf_section_heading_provenance_invalid",
            }.issubset(codes)
        )

    def test_rejects_unsupported_single_page_pdf_locator(self) -> None:
        loaded = _loaded()
        geometry = [{"page": 1, "bbox": [10.0, 20.0, 100.0, 30.0]}]
        loaded["blocks.jsonl"] = [
            {
                "block_id": "main-paragraph-0001",
                "source_path": PDF_PATH,
                "source_locator": "section-experimental-part",
                "source_geometry": geometry,
            }
        ]
        loaded["coverage.jsonl"] = [
            {
                "coverage_id": "heading-experimental-part",
                "content_kind": "section_heading",
                "status": "included",
                "output_id": "section-experimental-part",
                "source_path": PDF_PATH,
                "source_locator": "section-experimental-part",
                "source_geometry": geometry,
            },
            {
                "coverage_id": "content-main-paragraph-0001",
                "content_kind": "paragraph",
                "status": "included",
                "output_id": "main-paragraph-0001",
                "source_path": PDF_PATH,
                "source_locator": "section-experimental-part",
                "source_geometry": geometry,
            },
        ]

        codes = {
            finding.code for finding in _validate_pdf_block_provenance(loaded)
        }
        self.assertIn("pdf_block_locator_unsupported", codes)
        self.assertIn("pdf_section_heading_provenance_invalid", codes)

    def test_included_coverage_requires_source_path_and_locator(self) -> None:
        findings = _validate_coverage(
            [
                {
                    "coverage_id": "heading-experimental-part",
                    "content_kind": "section_heading",
                    "status": "included",
                    "source_path": "",
                    "source_locator": "section-experimental-part",
                }
            ]
        )

        self.assertIn(
            "included_coverage_provenance_missing",
            {finding.code for finding in findings},
        )

    def test_validate_candidate_runs_pdf_page_analysis_audit(self) -> None:
        loaded = _loaded()
        loaded["page_analysis.jsonl"].append(  # type: ignore[union-attr]
            {
                "kind": "glyph_decoding",
                "status": "unresolved",
                "page": 1,
                "glyph_name": "C999",
            }
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            extraction.mkdir()
            diagnostic.mkdir()
            (extraction / "record.md").write_text(
                "# Synthetic PDF article\n",
                encoding="utf-8",
            )
            for name in ("manifest.json", "sources.json"):
                (diagnostic / name).write_text(
                    json.dumps(loaded[name]) + "\n",
                    encoding="utf-8",
                )
            (diagnostic / "page_analysis.jsonl").write_text(
                "\n".join(
                    json.dumps(row)
                    for row in loaded["page_analysis.jsonl"]  # type: ignore[union-attr]
                )
                + "\n",
                encoding="utf-8",
            )
            (diagnostic / "coverage.jsonl").write_text(
                json.dumps(
                    {
                        "coverage_id": "record",
                        "content_kind": "article",
                        "status": "included",
                        "source_path": "generated",
                        "source_locator": "synthetic validation fixture",
                        "output_path": "record.md",
                        "output_locator": {"start_line": 1, "end_line": 1},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (diagnostic / "quality.json").write_text("{}\n", encoding="utf-8")
            (diagnostic / "confidence.json").write_text(
                json.dumps(
                    {
                        "categories": {
                            "main_text": {
                                "level": "high",
                                "basis": "synthetic PDF fixture",
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic PDF article",
            )

        codes = {item.code for item in report.findings}
        self.assertIn("pdf_unresolved_glyph", codes)
        self.assertIn("pdf_blocks_missing", codes)


if __name__ == "__main__":
    unittest.main()
