from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from scripts.extraction.models import (
    ArticleExtraction,
    ContentBlock,
    FigureItem,
    HtmlExtraction,
    Section,
    SourceFile,
)
from scripts.extraction.reporting import (
    _confidence_document,
    _pdf_exclusion_coverage,
    _repair_rows,
    _reconciliation_rows,
    _source_coverage,
    write_diagnostics,
    write_validation_result,
)
from scripts.extraction.validation import validate_candidate


def _article(**changes: object) -> ArticleExtraction:
    values: dict[str, object] = {
        "title": "Synthetic article",
        "bibliographic": {},
        "sections": [
            Section(
                section_id="section-001",
                heading="Results",
                blocks=[
                    ContentBlock(
                        block_id="block-001",
                        kind="paragraph",
                        markdown="Synthetic body text.",
                        plain_text="Synthetic body text.",
                        source_path="html/main.html",
                        source_locator="//p[1]",
                    )
                ],
            )
        ],
        "figures": [],
        "tables": [],
        "references": [],
        "supporting_information": [],
        "repairs": [],
        "warnings": [],
    }
    values.update(changes)
    return ArticleExtraction(**values)  # type: ignore[arg-type]


def _json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ArticleReportingTests(unittest.TestCase):
    def test_supplement_repairs_are_included_in_private_repair_rows(self) -> None:
        supplement_repair = {
            "schema_version": "1.0",
            "repair_id": "supplement_001-text-replacement-001",
            "mode": "exact_literal",
            "pattern": "gra�tude",
            "replacement": "gratitude",
            "occurrences": 2,
            "reason": "The native font map lost the ti ligature.",
            "evidence": "Both rendered words read gratitude.",
            "source_path": "supplementary/consent.pdf",
            "source_sha256": "a" * 64,
            "source_locator": "native-text;exact-literal-replacement",
        }

        rows = _repair_rows(
            _article(),
            [SimpleNamespace(repairs=[supplement_repair])],
        )

        self.assertEqual(rows, [supplement_repair])
        self.assertIsNot(rows[0], supplement_repair)

    def test_html_primary_pdf_without_unique_outputs_is_duplicate_coverage(self) -> None:
        sources = [
            SourceFile(
                role="main_pdf",
                path=Path("unused.pdf"),
                relative_path="pdf/main.pdf",
                size=123,
                sha256="0" * 64,
                detected_format="application/pdf",
                page_count=1,
            ),
            SourceFile(
                role="main_html",
                path=Path("unused.html"),
                relative_path="html/main.html",
                size=456,
                sha256="1" * 64,
                detected_format="text/html",
            ),
        ]

        rows = _source_coverage(sources, _article(), [], [])

        pdf = rows[0]
        self.assertEqual(pdf["status"], "duplicate")
        self.assertEqual(pdf["output_ids"], [])
        self.assertIn("secondary verification source", pdf["reason"])

    def test_html_extraction_is_a_backward_alias_with_html_defaults(self) -> None:
        self.assertIs(HtmlExtraction, ArticleExtraction)
        article = _article()

        reconciliation = _reconciliation_rows(article, [], [])
        body = reconciliation[0]
        ocr = reconciliation[1]
        confidence = _confidence_document(article, [], [], [])

        self.assertEqual(body["selected_source"], "main_html")
        self.assertEqual(body["supporting_source"], "main_pdf")
        self.assertIn("publisher HTML is primary", body["reason"])
        self.assertNotIn("00559", json.dumps(reconciliation))
        self.assertIn("no OCR was performed", ocr["reason"])
        self.assertIn(
            "publisher HTML",
            confidence["categories"]["main_text"]["basis"],
        )
        self.assertEqual(
            confidence["categories"]["scientific_tables"]["level"],
            "not_applicable",
        )
        self.assertEqual(
            confidence["categories"]["supplementary_material"]["level"],
            "not_applicable",
        )
        self.assertEqual(
            confidence["categories"]["graphical_abstract_image"]["level"],
            "not_applicable",
        )

    def test_supplement_only_tables_are_scientific_tables(self) -> None:
        supplement = SimpleNamespace(warnings=[], tables=[object()])

        confidence = _confidence_document(_article(), [supplement], [], [])

        self.assertEqual(
            confidence["categories"]["scientific_tables"]["level"],
            "medium",
        )
        self.assertIn(
            "machine-ready JSON",
            confidence["categories"]["scientific_tables"]["basis"],
        )

    def test_source_anomaly_can_close_only_its_named_graphical_coverage(self) -> None:
        article = _article(
            figures=[
                FigureItem(
                    figure_id="graphical_abstract",
                    source_id="graphical-abstract",
                    label="Graphical Abstract",
                    kind="graphical_abstract",
                    caption_markdown="Authored summary.",
                    caption_plain="Authored summary.",
                    source_path="html/main.html",
                    source_locator="//figure[1]",
                )
            ]
        )
        source = SourceFile(
            role="main_html",
            path=Path("unused.html"),
            relative_path="html/main.html",
            size=456,
            sha256="1" * 64,
            detected_format="text/html",
        )
        source_anomaly = {
            "schema_version": "1.0",
            "anomaly_id": "graphical-abstract-image-unavailable",
            "coverage_id": "unresolved-graphical-abstract-image",
            "source_path": "html/main.html",
            "source_locator": "//figure[1]",
            "observed": "The archived figure container has no image bytes.",
            "assessment": "The source omitted the graphical-abstract pixels.",
            "disposition": "Retain the prose and do not fabricate an image.",
        }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            extraction.mkdir()
            (extraction / "record.md").write_text(
                "# Synthetic article\n\nSynthetic body text.\n",
                encoding="utf-8",
            )
            diagnostic = root / "extraction_diagnostic"
            write_diagnostics(
                diagnostic,
                extraction,
                record_id="00001",
                run_id="source-limitation",
                fingerprint="1" * 64,
                sources=[source],
                article=article,
                supplements=[],
                assets=[],
                coverage=[],
                override_text=None,
                source_anomalies=[source_anomaly],
            )

            coverage = _json_lines(diagnostic / "coverage.jsonl")
            graphical = next(
                row
                for row in coverage
                if row.get("coverage_id")
                == "unresolved-graphical-abstract-image"
            )
            report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic article",
            )

        self.assertEqual(graphical["status"], "intentionally_excluded")
        self.assertIn("graphical-abstract-image-unavailable", graphical["reason"])
        self.assertNotIn(
            "unresolved_coverage",
            {finding.code for finding in report.findings},
        )
        self.assertNotIn(
            "diagnostic_warning_graphical_abstract_image_unavailable",
            {finding.code for finding in report.findings},
        )

    def test_pdf_exclusions_are_explicitly_accounted_for(self) -> None:
        article = _article(
            text_extraction={
                "source_role": "main_pdf",
                "source_path": "pdf/main.pdf",
                "method": "native-glyph-layout",
                "ocr_performed": False,
            },
            page_diagnostics=[
                {
                    "kind": "page_summary",
                    "page": 1,
                    "excluded_lines": {
                        "title_or_authors": 2,
                        "configured_furniture": 1,
                        "rotated_furniture": 1,
                    },
                }
            ],
        )

        rows = _pdf_exclusion_coverage(article)

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {row["status"] for row in rows},
            {"duplicate", "intentionally_excluded"},
        )

    def test_pdf_caption_coverage_uses_exact_asset_ids(self) -> None:
        figures = [
            FigureItem(
                figure_id="figure_001",
                source_id=None,
                label="Figure 1",
                kind="figure",
                caption_markdown="Figure 1 caption.",
                caption_plain="Figure 1 caption.",
                source_path="pdf/main.pdf",
                source_locator="PDF page 1, caption box [1.00, 2.00, 3.00, 4.00]",
            ),
            FigureItem(
                figure_id="scheme_001",
                source_id=None,
                label="Scheme 1",
                kind="scheme",
                caption_markdown="Scheme 1",
                caption_plain="Scheme 1",
                source_path="pdf/main.pdf",
                source_locator="PDF page 2, caption box [1.00, 2.00, 3.00, 4.00]",
            ),
        ]
        article = _article(
            figures=figures,
            text_extraction={
                "source_role": "main_pdf",
                "source_path": "pdf/main.pdf",
                "method": "native-glyph-layout",
                "ocr_performed": False,
            },
            page_diagnostics=[
                {
                    "kind": "page_summary",
                    "page": 1,
                    "excluded_lines": {"caption": 1},
                    "excluded_output_ids": {"caption": ["figure_001"]},
                },
                {
                    "kind": "page_summary",
                    "page": 2,
                    "excluded_lines": {"caption": 1},
                    "excluded_output_ids": {"caption": ["scheme_001"]},
                },
            ],
        )

        rows = _pdf_exclusion_coverage(article)

        self.assertEqual(rows[0]["output_ids"], ["figure_001"])
        self.assertEqual(rows[1]["output_ids"], ["scheme_001"])

    def test_reconciliation_omits_table_boilerplate_without_tables(self) -> None:
        article = _article(
            text_extraction={
                "source_role": "main_pdf",
                "source_path": "pdf/main.pdf",
                "method": "native-glyph-layout",
                "ocr_performed": False,
            }
        )

        rows = _reconciliation_rows(
            article,
            [],
            [
                {
                    "asset_id": "figure_001",
                    "category": "figure",
                    "label": "Figure 1",
                    "source_path": "pdf/main.pdf",
                }
            ],
        )

        self.assertNotIn("table", rows[0]["content"])
        self.assertEqual(rows[2]["supporting_source"], "PDF caption structure")

    def test_reconciliation_uses_pdf_structure_for_supplement_table_asset(self) -> None:
        article = _article(
            text_extraction={
                "source_role": "main_html",
                "source_path": "html/main.html",
                "method": "publisher-html-semantic-extraction",
                "ocr_performed": False,
            }
        )

        rows = _reconciliation_rows(
            article,
            [],
            [
                {
                    "asset_id": "supplement_001_table_s1",
                    "category": "supplement_table",
                    "label": "Table S1",
                    "source_path": "supplementary/supplementary.pdf",
                }
            ],
        )

        self.assertEqual(rows[2]["selected_source"], "supplementary/supplementary.pdf")
        self.assertEqual(rows[2]["supporting_source"], "PDF table structure")
        self.assertNotIn("HTML caption structure", json.dumps(rows[2]))

    def test_image_only_supplement_reporting_does_not_claim_native_text(self) -> None:
        supplement = SimpleNamespace(
            supplement_id="supplement_001",
            source=SimpleNamespace(relative_path="supplementary/image-only.pdf"),
            blocks=[],
            figures=[object()],
            tables=[],
        )

        rows = _reconciliation_rows(_article(), [supplement], [])

        source_row = rows[-1]
        self.assertIn("reviewed visual content", source_row["reason"])
        self.assertNotIn("native text extracted", source_row["reason"])

    def test_reviewed_supplement_blocks_do_not_imply_native_text(self) -> None:
        supplement = SimpleNamespace(
            supplement_id="supplement_001",
            source=SimpleNamespace(relative_path="supplementary/scanned.pdf"),
            blocks=[object()], figures=[], tables=[],
        )
        row = _reconciliation_rows(_article(), [supplement], [])[-1]
        self.assertIn("source text represented", row["reason"])
        self.assertNotIn("native text extracted", row["reason"])

    def test_pdf_page_renders_and_table_cells_do_not_inherit_html_captions(self) -> None:
        assets = [
            {"asset_id": "page1", "category": "supplement_page_render",
             "source_path": "supplementary/scanned.pdf"},
            {"asset_id": "cell1", "category": "table_cell",
             "source_path": "pdf/main.pdf"},
        ]
        rows = _reconciliation_rows(_article(), [], assets)
        self.assertIsNone(rows[-2]["supporting_source"])
        self.assertEqual(rows[-1]["supporting_source"], "PDF table structure")

    def test_exclusion_only_supplement_reporting_acknowledges_reviewed_text(self) -> None:
        supplement = SimpleNamespace(
            supplement_id="supplement_002",
            source=SimpleNamespace(relative_path="supplementary/captions.pdf"),
            blocks=[],
            figures=[],
            tables=[],
            exclusions=[
                {
                    "content_kind": "figure_caption",
                    "status": "intentionally_excluded",
                }
            ],
        )

        rows = _reconciliation_rows(_article(), [supplement], [])

        source_row = rows[-1]
        self.assertIn("reviewed source text", source_row["reason"])
        self.assertIn("consolidated structured items", source_row["reason"])
        self.assertNotIn("no native text", source_row["reason"])

    def test_duplicate_only_supplement_reconciliation_uses_reviewed_disposition(self) -> None:
        page_map = "pages=1-2->pdf/main.pdf#pages=1-2"
        evidence = "Both rendered pages are byte-identical."
        supplement = SimpleNamespace(
            supplement_id="supplement_002",
            source=SimpleNamespace(relative_path="supplementary/combined.pdf"),
            blocks=[],
            figures=[],
            tables=[],
            exclusions=[
                {
                    "content_kind": "supplement_source_content",
                    "status": "duplicate",
                    "source_locator": page_map,
                    "reason": "Publisher convenience bundle duplicates main.pdf.",
                    "evidence": evidence,
                }
            ],
        )

        rows = _reconciliation_rows(_article(), [supplement], [])

        source_row = rows[-1]
        self.assertEqual(source_row["status"], "duplicate")
        self.assertEqual(source_row["supporting_source"], page_map)
        self.assertEqual(source_row["evidence"], evidence)
        self.assertIn("convenience bundle duplicates", source_row["reason"])
        self.assertNotIn("no native text", source_row["reason"])

    def test_pdf_primary_reporting_records_ocr_and_page_analysis(self) -> None:
        pdf_block = ContentBlock(
            block_id="block-001",
            kind="paragraph",
            markdown="Synthetic PDF body text.",
            plain_text="Synthetic PDF body text.",
            source_path="pdf/main.pdf",
            source_locator="PDF page 1, box [72.00, 100.00, 500.00, 112.00]",
            source_geometry=[
                {"page": 1, "bbox": [72.0, 100.0, 500.0, 112.0]}
            ],
        )
        article = _article(
            sections=[
                Section(
                    section_id="section-001",
                    heading="Results",
                    blocks=[pdf_block],
                )
            ],
            text_extraction={
                "source_role": "main_pdf",
                "source_path": "pdf/main.pdf",
                "method": "synthetic-pdf-glyph-layout-with-ocr-fallback",
                "ocr_performed": True,
            },
            page_diagnostics=[
                {
                    "page": 1,
                    "source_path": "pdf/main.pdf",
                    "method": "native-glyph-layout",
                    "ocr_performed": False,
                },
                {
                    "page": 2,
                    "source_path": "pdf/main.pdf",
                    "method": "ocr-fallback",
                    "ocr_performed": True,
                },
            ],
        )
        source = SourceFile(
            role="main_pdf",
            path=Path("unused.pdf"),
            relative_path="pdf/main.pdf",
            size=123,
            sha256="0" * 64,
            detected_format="pdf",
            page_count=2,
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            extraction.mkdir()
            (extraction / "record.md").write_text(
                "# Synthetic article\n\nSynthetic PDF body text.\n",
                encoding="utf-8",
            )
            diagnostic = root / "extraction_diagnostic"

            write_diagnostics(
                diagnostic,
                extraction,
                record_id="00001",
                run_id="synthetic-pdf",
                fingerprint="1" * 64,
                sources=[source],
                article=article,
                supplements=[],
                assets=[],
                coverage=[],
                override_text=None,
                source_anomalies=[],
                expected_counts={"pages": 2, "main_figures": 0},
            )

            reconciliation = _json_lines(diagnostic / "reconciliation.jsonl")
            coverage = _json_lines(diagnostic / "coverage.jsonl")
            confidence = json.loads(
                (diagnostic / "confidence.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (diagnostic / "manifest.json").read_text(encoding="utf-8")
            )
            independent_report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic article",
            )
            pages = _json_lines(diagnostic / "page_analysis.jsonl")
            blocks = _json_lines(diagnostic / "blocks.jsonl")
            status = write_validation_result(diagnostic, [], {})
            quality = json.loads(
                (diagnostic / "quality.json").read_text(encoding="utf-8")
            )

        self.assertEqual(reconciliation[0]["selected_source"], "pdf/main.pdf")
        self.assertIsNone(reconciliation[0]["supporting_source"])
        self.assertIn(
            "local region-based OCR",
            reconciliation[0]["reason"],
        )
        self.assertIn("OCR was performed", reconciliation[1]["reason"])
        self.assertNotIn("HTML", json.dumps(reconciliation))
        self.assertIn("primary semantic source", coverage[0]["reason"])
        self.assertNotIn(
            "HTML",
            confidence["categories"]["main_text"]["basis"],
        )
        self.assertTrue(confidence["probabilistic"])
        self.assertTrue(manifest["ocr_performed"])
        self.assertEqual(
            manifest["expected_counts"],
            {"main_figures": 0, "pages": 2},
        )
        self.assertEqual(
            independent_report.expected_counts,
            {"main_figures": 0, "pages": 2},
        )
        self.assertEqual(
            manifest["text_extraction"]["method"],
            "synthetic-pdf-glyph-layout-with-ocr-fallback",
        )
        self.assertEqual(manifest["page_analysis"], "page_analysis.jsonl")
        self.assertEqual([page["page"] for page in pages], [1, 2])
        self.assertTrue(all(page["schema_version"] == "1.0" for page in pages))
        self.assertEqual(blocks[0]["source_geometry"], pdf_block.source_geometry)
        self.assertEqual(status, "needs_review")
        self.assertEqual(quality["status"], "needs_review")
        self.assertEqual(quality["review"], "not_performed")
        self.assertEqual(
            confidence["categories"]["main_text"]["level"], "medium"
        )


if __name__ == "__main__":
    unittest.main()
