from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
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
    _reconciliation_rows,
    write_diagnostics,
    write_validation_result,
)


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
            )

            reconciliation = _json_lines(diagnostic / "reconciliation.jsonl")
            coverage = _json_lines(diagnostic / "coverage.jsonl")
            confidence = json.loads(
                (diagnostic / "confidence.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (diagnostic / "manifest.json").read_text(encoding="utf-8")
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
