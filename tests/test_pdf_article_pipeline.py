from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.extraction.metadata import RecordMetadata
from scripts.extraction.models import (
    ArticleExtraction,
    ContentBlock,
    FigureItem,
    Section,
    SourceFile,
)
from scripts.extraction.pdf_article_extractor import (
    PdfArticleExtractionError,
    extract_pdf_article,
)
from scripts.extraction.pdf_text_extractor import (
    PdfTextDocument,
    PdfTextLine,
    PdfTextPage,
)
from scripts.extraction.pipeline import extract_record
from scripts.extraction.record_json import build_record_json, write_record_json
from scripts.extraction.validation import ValidationReport


RECORD_ID = "00001"
TITLE = "Synthetic PDF Article"
PDF_RELATIVE_PATH = f"papers (private)/{RECORD_ID}/pdf/main.pdf"
HTML_RELATIVE_PATH = f"papers (private)/{RECORD_ID}/html/main.html"


def _metadata() -> RecordMetadata:
    return RecordMetadata(
        record_id=RECORD_ID,
        title=TITLE,
        authors=("Test Author",),
        journal="Synthetic Journal",
        publication_year=2000,
        doi="10.0000/synthetic",
        document_type="research article",
    )


def _line(
    page: int,
    top: float,
    text: str,
    *,
    left: float = 72.0,
    right: float = 500.0,
    bottom: float | None = None,
    markdown: str | None = None,
    rotated: bool = False,
    source_region_id: str | None = None,
) -> PdfTextLine:
    bottom = top + 10.0 if bottom is None else bottom
    bbox = (left, top, right, bottom)
    return PdfTextLine(
        page=page,
        bbox=bbox,
        plain_text=text,
        markdown=text if markdown is None else markdown,
        source_locator=(
            f"PDF page {page}, box "
            f"[{left:.2f}, {top:.2f}, {right:.2f}, {bottom:.2f}]"
        ),
        rotated=rotated,
        source_region_id=source_region_id,
    )


def _decoded_document() -> PdfTextDocument:
    page_one = PdfTextPage(
        page=1,
        width=595.0,
        height=709.0,
        classification="native_text",
        lines=[
            _line(1, 5, "Synthetic Journal 83"),
            _line(1, 30, TITLE),
            _line(1, 45, "by Test Author"),
            _line(1, 65, "Division of Synthetic Chemistry"),
            _line(1, 80, "Dedicated to careful extraction"),
            _line(1, 105, "This abstract is native PDF text."),
            _line(
                1,
                145,
                "1. Introduction. The article begins here.",
                markdown="**1. Introduction.** The article begins here.",
            ),
        ],
    )
    page_two = PdfTextPage(
        page=2,
        width=595.0,
        height=709.0,
        classification="native_text_with_rotated_text",
        lines=[
            _line(2, 45, "The body continues outside the figure."),
            _line(2, 140, "PANEL TEXT MUST NOT ENTER THE ARTICLE BODY"),
            _line(
                2,
                315,
                "Figure 1. A deliberately synthetic caption.",
                markdown="**Figure 1.** A deliberately synthetic caption.",
            ),
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "[1] A. Author, First synthetic reference."),
            _line(2, 400, "[2] B. Author, Second synthetic reference."),
            _line(
                2,
                680,
                "Downloaded from publisher.example",
                left=580,
                right=590,
                rotated=True,
            ),
        ],
    )
    return PdfTextDocument(
        relative_path=PDF_RELATIVE_PATH,
        pages=[page_one, page_two],
        diagnostic_rows=[],
        warnings=[],
        all_pages_classified=True,
    )


def _article() -> ArticleExtraction:
    return ArticleExtraction(
        title=TITLE,
        bibliographic={},
        sections=[
            Section(
                section_id="section-body",
                heading="Body",
                blocks=[
                    ContentBlock(
                        block_id="body-001",
                        kind="paragraph",
                        markdown="Synthetic body.",
                        plain_text="Synthetic body.",
                        source_path=PDF_RELATIVE_PATH,
                        source_locator="PDF page 1",
                    )
                ],
            )
        ],
        figures=[],
        tables=[],
        references=[],
        supporting_information=[],
        repairs=[],
        warnings=[],
    )


class PdfArticleExtractionTests(unittest.TestCase):
    def test_pdf_structure_excludes_visuals_captions_and_page_furniture(self) -> None:
        crop_specs = [
            {
                "asset_id": "figure_001",
                "category": "figure",
                "label": "Figure 1",
                "source_path": PDF_RELATIVE_PATH,
                "page": 2,
                "box": [70, 100, 525, 300],
                "caption_page": 2,
                "caption_box": [70, 305, 525, 340],
            }
        ]
        config = {
            "source_path": PDF_RELATIVE_PATH,
            "exclude_regions": [
                {
                    "pages": "all",
                    "box": [0, 0, 595, 20],
                    "reason": "running journal header",
                }
            ],
        }

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=_decoded_document(),
        ) as decode:
            article = extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                config,
                crop_specs=crop_specs,
            )

        decode.assert_called_once()
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].label, "Figure 1")
        self.assertEqual(
            article.figures[0].caption_plain,
            "Figure 1. A deliberately synthetic caption.",
        )
        self.assertEqual(
            article.figures[0].caption_markdown,
            "Figure 1. A deliberately synthetic caption.",
        )
        body = "\n".join(
            block.plain_text
            for section in article.sections
            for block in section.blocks
        )
        self.assertIn("The body continues outside the figure.", body)
        for excluded in (
            "Synthetic Journal 83",
            "PANEL TEXT MUST NOT ENTER THE ARTICLE BODY",
            "A deliberately synthetic caption",
            "Downloaded from publisher.example",
        ):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, body)
        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "[1] A. Author, First synthetic reference.",
                "[2] B. Author, Second synthetic reference.",
            ],
        )
        self.assertEqual(article.text_extraction["source_role"], "main_pdf")
        self.assertFalse(article.text_extraction["ocr_performed"])
        page_summaries = [
            row
            for row in article.page_diagnostics
            if row.get("kind") == "page_summary"
        ]
        self.assertEqual([row["page"] for row in page_summaries], [1, 2])
        self.assertTrue(all(not row["ocr_performed"] for row in page_summaries))
        page_two_summary = next(row for row in page_summaries if row["page"] == 2)
        self.assertEqual(
            page_two_summary["excluded_output_ids"],
            {"caption": ["figure_001"], "visual_asset": ["figure_001"]},
        )

    def test_scanned_caption_can_use_source_reviewed_value(self) -> None:
        crop_specs = [
            {
                "asset_id": "figure_001",
                "category": "figure",
                "label": "Figure 1",
                "source_path": PDF_RELATIVE_PATH,
                "page": 2,
                "box": [70, 100, 525, 300],
                "caption_page": 2,
                "caption_box": [70, 305, 525, 340],
                "caption_reviewed_value": (
                    "Figure 1. Source-reviewed 5′-TCCT-3′ caption."
                ),
            }
        ]

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=_decoded_document(),
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                crop_specs=crop_specs,
            )

        self.assertEqual(
            article.figures[0].caption_plain,
            "Figure 1. Source-reviewed 5′-TCCT-3′ caption.",
        )
        self.assertEqual(
            article.figures[0].source_locator,
            "PDF page 2, caption box [70.00, 305.00, 525.00, 340.00]",
        )

    def test_same_line_experimental_heading_preserves_plain_text(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. Main result.",
                            markdown="**1. Results.** Main result.",
                        ),
                        _line(
                            1,
                            120,
                            "Experimental Part General procedure follows.",
                            markdown="**Experimental Part** General procedure follows.",
                        ),
                    ],
                )
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata()
            )

        experimental = next(
            section for section in article.sections if section.heading == "Experimental Part"
        )
        self.assertEqual(experimental.blocks[0].markdown, "General procedure follows.")
        self.assertEqual(experimental.blocks[0].plain_text, "General procedure follows.")

    def test_wrapped_numbered_headings_keep_full_title_and_body(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. Main result.",
                            markdown="**1. Results.** Main result.",
                        ),
                        _line(
                            1,
                            120,
                            "2. Long Scientific Poly-",
                            markdown="**2. Long Scientific Poly-**",
                        ),
                        _line(
                            1,
                            132,
                            "amides. – Body text begins.",
                            markdown="**amides.** – *Body text begins.*",
                        ),
                        _line(
                            1,
                            160,
                            "3. MPE·Fe",
                            markdown="**3. MPE·Fe**",
                        ),
                        _line(
                            1,
                            172,
                            "II Footprint Titrations. – Methods follow.",
                            markdown=(
                                "<sup>**II**</sup> **Footprint Titrations.** "
                                "– Methods follow."
                            ),
                        ),
                    ],
                )
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata()
            )

        polyamide = next(
            section
            for section in article.sections
            if section.heading == "2. Long Scientific Poly-amides"
        )
        footprint = next(
            section
            for section in article.sections
            if section.heading == "3. MPE·FeII Footprint Titrations"
        )
        self.assertEqual(polyamide.blocks[0].markdown, "Body text begins.")
        self.assertEqual(polyamide.blocks[0].plain_text, "Body text begins.")
        self.assertEqual(
            polyamide.blocks[0].source_geometry,
            [
                {"page": 1, "bbox": [72.0, 120.0, 500.0, 130.0]},
                {"page": 1, "bbox": [72.0, 132.0, 500.0, 142.0]},
            ],
        )
        self.assertEqual(footprint.blocks[0].markdown, "Methods follow.")
        self.assertEqual(footprint.blocks[0].plain_text, "Methods follow.")
        self.assertEqual(
            footprint.blocks[0].source_geometry,
            [
                {"page": 1, "bbox": [72.0, 160.0, 500.0, 170.0]},
                {"page": 1, "bbox": [72.0, 172.0, 500.0, 182.0]},
            ],
        )

    def test_colon_at_page_break_keeps_one_paragraph_and_page_range(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. UV (MeOH):",
                            markdown="**1. Results.** UV (MeOH):",
                        ),
                    ],
                ),
                PdfTextPage(
                    page=2,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[_line(2, 70, "245, 336 (37445).")],
                ),
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata()
            )

        block = article.sections[-1].blocks[0]
        self.assertEqual(block.plain_text, "UV (MeOH): 245, 336 (37445).")
        self.assertEqual(
            block.source_locator,
            (
                "PDF pages 1, 2; exact per-line page/bbox geometry is stored "
                "in source_geometry"
            ),
        )
        self.assertEqual(
            block.source_geometry,
            [
                {"page": 1, "bbox": [72.0, 90.0, 500.0, 100.0]},
                {"page": 2, "bbox": [72.0, 70.0, 500.0, 80.0]},
            ],
        )

    def test_ordered_ocr_column_transition_keeps_unfinished_paragraph(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="ocr_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. The sentence continues into the",
                            markdown="**1. Results.** The sentence continues into the",
                            source_region_id="left-column",
                        ),
                        _line(
                            1,
                            90,
                            "right column without a paragraph break.",
                            left=300.0,
                            source_region_id="right-column",
                        ),
                    ],
                )
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        article = extract_pdf_article(
            Path("synthetic.pdf"),
            PDF_RELATIVE_PATH,
            _metadata(),
            text_document=document,
        )

        blocks = article.sections[-1].blocks
        self.assertEqual(len(blocks), 1)
        self.assertEqual(
            blocks[0].plain_text,
            "The sentence continues into the right column without a paragraph break.",
        )

    def test_empty_experimental_parent_heading_keeps_pdf_coverage(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. Main result.",
                            markdown="**1. Results.** Main result.",
                        ),
                        _line(
                            1,
                            120,
                            "Experimental Part",
                            markdown="**Experimental Part**",
                        ),
                        _line(
                            1,
                            150,
                            "1. General. General procedure.",
                            markdown="**1. General.** General procedure.",
                        ),
                    ],
                )
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata()
            )

        experimental = next(
            section for section in article.sections if section.heading == "Experimental Part"
        )
        self.assertEqual(experimental.blocks, [])
        self.assertEqual(experimental.source_path, PDF_RELATIVE_PATH)
        self.assertEqual(
            experimental.source_locator,
            "PDF page 1, box [72.00, 120.00, 500.00, 130.00]",
        )
        self.assertEqual(
            experimental.source_geometry,
            [{"page": 1, "bbox": [72.0, 120.0, 500.0, 130.0]}],
        )

        rendered = build_record_json(_metadata(), article, [], [])
        coverage = next(
            row
            for row in rendered.coverage
            if row.get("output_id") == experimental.section_id
        )
        self.assertEqual(coverage["source_path"], PDF_RELATIVE_PATH)
        self.assertEqual(coverage["source_locator"], experimental.source_locator)
        self.assertEqual(coverage["source_geometry"], experimental.source_geometry)

    def test_same_line_references_heading_preserves_first_reference(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. Main result.",
                            markdown="**1. Results.** Main result.",
                        ),
                        _line(
                            1,
                            120,
                            "References [1] A. Author, First reference.",
                            markdown="**References** [1] A. Author, First reference.",
                        ),
                        _line(1, 140, "[2] B. Author, Second reference."),
                    ],
                )
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata()
            )

        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "[1] A. Author, First reference.",
                "[2] B. Author, Second reference.",
            ],
        )

    def test_reference_numbering_must_start_at_one(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(
                            1,
                            90,
                            "1. Results. Main result.",
                            markdown="**1. Results.** Main result.",
                        ),
                        _line(1, 120, "REFERENCES", markdown="**References**"),
                        _line(1, 140, "[2] B. Author, Orphaned second reference."),
                    ],
                )
            ],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            with self.assertRaisesRegex(
                PdfArticleExtractionError, "must start at 1 and remain contiguous"
            ):
                extract_pdf_article(
                    Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata()
                )


class PipelineSourceSelectionTests(unittest.TestCase):
    def _source(
        self, root: Path, role: str, relative_path: str, content: bytes, detected: str
    ) -> SourceFile:
        path = root / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return SourceFile(
            role=role,
            path=path,
            relative_path=relative_path,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            detected_format=detected,
        )

    def _run(self, *, include_html: bool):
        temporary = TemporaryDirectory()
        root = Path(temporary.name)
        (root / "papers (private)" / "staging").mkdir(parents=True)
        metadata_source = self._source(
            root,
            "public_metadata",
            f"database/records/{RECORD_ID}.yaml",
            b"title: synthetic\n",
            "text/yaml",
        )
        pdf_source = self._source(
            root, "main_pdf", PDF_RELATIVE_PATH, b"%PDF-synthetic", "application/pdf"
        )
        sources = [metadata_source, pdf_source]
        if include_html:
            sources.append(
                self._source(
                    root,
                    "main_html",
                    HTML_RELATIVE_PATH,
                    b"<html><article>Synthetic</article></html>",
                    "text/html",
                )
            )

        article = _article()
        validation = ValidationReport(
            extraction_dir="extraction",
            diagnostic_dir="extraction_diagnostic",
            expected_title=TITLE,
            findings=(),
            counts={"main_figures": 0},
            expected_counts={"main_figures": 0},
            checked_files=1,
        )

        def write_diagnostics(diagnostic_root: Path, _extraction_root: Path, **_kwargs):
            diagnostic_root.mkdir()

        patches = [
            patch("scripts.extraction.pipeline.discover_sources", return_value=sources),
            patch("scripts.extraction.pipeline.load_record_metadata", return_value=_metadata()),
            patch(
                "scripts.extraction.pipeline._load_override",
                return_value=(
                    {
                        "pdf_text": {"source_path": PDF_RELATIVE_PATH},
                        "expected_counts": {"main_figures": 0},
                    },
                    None,
                    None,
                ),
            ),
            patch("scripts.extraction.pipeline.extract_pdf_article", return_value=article),
            patch("scripts.extraction.pipeline.extract_html", return_value=article),
            patch("scripts.extraction.pipeline.extract_supplements", return_value=[]),
            patch("scripts.extraction.pipeline.render_pdf_crops", return_value=[]),
            patch(
                "scripts.extraction.pipeline.write_record_json",
                side_effect=write_record_json,
            ),
            patch(
                "scripts.extraction.pipeline.write_diagnostics",
                side_effect=write_diagnostics,
            ),
            patch("scripts.extraction.pipeline.validate_candidate", return_value=validation),
            patch("scripts.extraction.pipeline.write_validation_result", return_value="reviewed"),
            patch("scripts.extraction.pipeline._verify_source_snapshots"),
            patch(
                "scripts.extraction.pipeline._verify_pdf_text",
                return_value=[{"page_count": 1, "pages_with_native_text": 1}],
            ),
        ]
        mocks = [item.start() for item in patches]
        self.addCleanup(temporary.cleanup)
        for item in patches:
            self.addCleanup(item.stop)

        result = extract_record(root, RECORD_ID, run_id="synthetic-run")
        return result, article, mocks

    def test_pdf_only_record_selects_pdf_article_path_and_expected_counts(self) -> None:
        result, _article_result, mocks = self._run(include_html=False)
        pdf_extractor = mocks[3]
        html_extractor = mocks[4]
        validator = mocks[9]

        pdf_extractor.assert_called_once()
        html_extractor.assert_not_called()
        self.assertEqual(pdf_extractor.call_args.args[1], PDF_RELATIVE_PATH)
        self.assertEqual(
            pdf_extractor.call_args.args[3], {"source_path": PDF_RELATIVE_PATH}
        )
        self.assertEqual(
            validator.call_args.kwargs["expected_counts"], {"main_figures": 0}
        )
        self.assertTrue((result.extraction_root / "record.json").is_file())
        self.assertFalse((result.extraction_root / "record.md").exists())

    def test_html_remains_primary_when_both_html_and_pdf_exist(self) -> None:
        _result, article, mocks = self._run(include_html=True)
        pdf_extractor = mocks[3]
        html_extractor = mocks[4]

        html_extractor.assert_called_once()
        pdf_extractor.assert_not_called()
        self.assertEqual(html_extractor.call_args.args[1], HTML_RELATIVE_PATH)
        self.assertIn(
            "automated_pdf_html_alignment_not_implemented",
            {warning["code"] for warning in article.warnings},
        )


if __name__ == "__main__":
    unittest.main()
