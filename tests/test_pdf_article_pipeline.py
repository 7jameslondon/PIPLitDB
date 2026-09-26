from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
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
    _glyph_overrides,
    extract_pdf_article,
)
from scripts.extraction.pdf_text_extractor import (
    PdfTextDocument,
    PdfTextLine,
    PdfTextPage,
)
from scripts.extraction.pipeline import (
    ExtractionError,
    _apply_rich_text_overrides,
    _reconcile_article_title,
    extract_record,
)
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
    def test_pdf_article_config_accepts_unicode_codepoint_glyph_overrides(self) -> None:
        self.assertEqual(
            _glyph_overrides(
                {
                    "glyph_overrides": [
                        {"font": "TimesNewRomanPSMT", "code": "U+0001", "value": "μ"},
                        {"font": "TimesNewRomanPSMT", "code": "u+81", "value": "•"},
                        {"font": "TimesNewRomanPSMT", "code": "raw:3", "value": "Δ"},
                    ]
                }
            ),
            {
                "TimesNewRomanPSMT": {
                    "U+0001": "μ",
                    "U+0081": "•",
                    "RAW:3": "Δ",
                }
            },
        )

    def test_configured_region_boundary_forces_paragraph_break(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(
                            1,
                            70,
                            "1. Introduction. The model terms are",
                            source_region_id="region-before",
                        ),
                        _line(
                            1,
                            90,
                            "where the next paragraph defines them.",
                            source_region_id="region-after",
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
            config={"paragraph_break_before_regions": ["region-after"]},
            text_document=document,
        )

        self.assertEqual(
            [block.plain_text for block in article.sections[0].blocks],
            [
                "The model terms are",
                "where the next paragraph defines them.",
            ],
        )

    def test_configured_region_boundary_rejects_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(PdfArticleExtractionError, "duplicates"):
            extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                config={
                    "paragraph_break_before_regions": ["region-after", "region-after"]
                },
                text_document=_decoded_document(),
            )

    def test_configured_abstract_heading_uses_heading_not_body_geometry(self) -> None:
        abstract_heading = _line(
            1,
            50,
            "ABSTRACT",
            left=72,
            right=150,
            source_region_id="abstract-heading",
        )
        abstract_body = _line(
            1,
            75,
            "Exact abstract body.",
            source_region_id="abstract-body",
        )
        introduction_heading = _line(
            1,
            110,
            "INTRODUCTION",
            source_region_id="introduction-heading",
        )
        introduction_body = _line(1, 135, "Article body.")
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        abstract_heading,
                        abstract_body,
                        introduction_heading,
                        introduction_body,
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
            config={
                "abstract_region_ids": ["abstract-body"],
                "section_headings": [
                    {
                        "region_id": "abstract-heading",
                        "title": "Abstract",
                        "level": 2,
                    },
                    {
                        "region_id": "introduction-heading",
                        "title": "Introduction",
                        "level": 2,
                    },
                ],
            },
            text_document=document,
        )

        self.assertEqual(
            [section.section_id for section in article.sections],
            ["section-abstract", "section-introduction"],
        )
        abstract = article.sections[0]
        self.assertEqual(abstract.source_geometry, [{"page": 1, "bbox": list(abstract_heading.bbox)}])
        self.assertEqual(
            abstract.blocks[0].source_geometry,
            [{"page": 1, "bbox": list(abstract_body.bbox)}],
        )
        self.assertEqual(abstract.blocks[0].plain_text, "Exact abstract body.")

    def test_configured_front_matter_can_preserve_specific_unlabeled_rich_kind(self) -> None:
        article = extract_pdf_article(
            Path("synthetic.pdf"),
            PDF_RELATIVE_PATH,
            _metadata(),
            config={
                "front_matter_regions": [
                    {
                        "page": 1,
                        "box": [72, 40, 500, 60],
                        "field": "byline",
                        "kind": "byline",
                        "reviewed_value": "Test Author¹",
                        "reviewed_markdown": "<strong>Test Author¹</strong>",
                    }
                ]
            },
            text_document=_decoded_document(),
        )

        block = article.front_matter[0]
        self.assertEqual(block.block_id, "front-matter-byline")
        self.assertEqual(block.kind, "byline")
        self.assertEqual(block.plain_text, "Test Author¹")
        self.assertEqual(block.markdown, "<strong>Test Author¹</strong>")

    def test_source_pinned_rich_text_override_changes_only_formatting(self) -> None:
        article = _article()
        source = SourceFile(
            role="main_pdf",
            path=Path("synthetic.pdf"),
            relative_path=PDF_RELATIVE_PATH,
            size=10,
            sha256="a" * 64,
            detected_format="pdf",
            page_count=1,
        )
        spec = {
            "target_id": "body-001",
            "expected_plain_text": "Synthetic body.",
            "expected_markdown": "Synthetic body.",
            "replacement_markdown": "Synthetic <em>body</em>.",
            "source_path": PDF_RELATIVE_PATH,
            "source_sha256": "a" * 64,
            "source_locator": "PDF page 1, body paragraph",
            "reason": "Restore directly reviewed emphasis.",
            "evidence": "The source prints body in italics.",
        }

        _apply_rich_text_overrides(article, [spec], [source])

        block = article.sections[0].blocks[0]
        self.assertEqual(block.plain_text, "Synthetic body.")
        self.assertEqual(block.markdown, "Synthetic <em>body</em>.")
        self.assertEqual(article.repairs[0].repair_id, "rich-text-override-001")
        self.assertEqual(
            article.repairs[0].evidence,
            "PDF page 1, body paragraph: The source prints body in italics.",
        )

    def test_rich_text_override_rejects_stale_or_text_changing_markup(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("synthetic.pdf"),
            relative_path=PDF_RELATIVE_PATH,
            size=10,
            sha256="a" * 64,
            detected_format="pdf",
            page_count=1,
        )
        base = {
            "target_id": "body-001",
            "expected_plain_text": "Synthetic body.",
            "expected_markdown": "Synthetic body.",
            "replacement_markdown": "Synthetic <em>body</em>.",
            "source_path": PDF_RELATIVE_PATH,
            "source_sha256": "a" * 64,
            "source_locator": "PDF page 1, body paragraph",
            "reason": "Restore directly reviewed emphasis.",
            "evidence": "The source prints body in italics.",
        }
        stale = dict(base, expected_markdown="Stale body.")
        with self.assertRaisesRegex(ExtractionError, "did not match target"):
            _apply_rich_text_overrides(_article(), [stale], [source])
        contradictory = dict(
            base,
            replacement_markdown="Synthetic <em>different body</em>.",
        )
        with self.assertRaisesRegex(ExtractionError, "changes visible plain text"):
            _apply_rich_text_overrides(_article(), [contradictory], [source])

    def test_configured_wrapped_heading_preserves_following_body_text(self) -> None:
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
                            70,
                            "1. Introduction. Introductory text.",
                            markdown="**1. Introduction.** Introductory text.",
                        ),
                        _line(
                            1,
                            90,
                            "Long Scientific",
                            markdown="**Long** <strong><em>Scientific</em></strong>",
                        ),
                        _line(
                            1,
                            102,
                            "Heading Body begins here.",
                            markdown="**Heading** Body begins here.",
                        ),
                        _line(1, 114, "It continues on the next line."),
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
            config={
                "section_headings": [
                    {
                        "page": 1,
                        "box": [70, 85, 501, 110],
                        "title": "Long Scientific Heading",
                        "level": 3,
                        "source_heading_text": "Long Scientific Heading",
                    }
                ]
            },
            text_document=document,
        )

        section = next(
            section
            for section in article.sections
            if section.heading == "Long Scientific Heading"
        )
        self.assertEqual(section.heading, "Long Scientific Heading")
        self.assertEqual(section.level, 3)
        self.assertEqual(
            [block.plain_text for block in section.blocks],
            ["Body begins here. It continues on the next line."],
        )
        self.assertEqual(
            [block.markdown for block in section.blocks],
            ["Body begins here. It continues on the next line."],
        )

    def test_double_spaced_same_margin_lines_form_paragraphs_by_indent(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="native_text",
                    lines=[
                        _line(1, 30, "Introduction", markdown="**Introduction**"),
                        _line(1, 100, "First paragraph", left=95, right=180),
                        _line(1, 100, "begins here", left=205, right=270),
                        _line(1, 130, "and continues at the ordinary margin.", left=72),
                        _line(1, 160, "Second paragraph begins here.", left=95),
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
            config={"source_path": PDF_RELATIVE_PATH},
            text_document=document,
        )

        self.assertEqual(
            [block.plain_text for block in article.sections[0].blocks],
            [
                "First paragraph begins here and continues at the ordinary margin.",
                "Second paragraph begins here.",
            ],
        )

    def test_bare_numbered_pdf_references_are_contiguous_entries(self) -> None:
        document = _decoded_document()
        document.pages[1].lines[3:] = [
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "1 A. Author, First synthetic reference."),
            _line(2, 390, "439."),
            _line(2, 400, "2 B. Author, Second synthetic reference."),
        ]

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                {"source_path": PDF_RELATIVE_PATH},
            )

        self.assertEqual(
            [block.block_id for block in article.references],
            ["reference-001", "reference-002"],
        )
        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "1 A. Author, First synthetic reference. 439.",
                "2 B. Author, Second synthetic reference.",
            ],
        )

    def test_parenthesized_pdf_references_are_contiguous_entries(self) -> None:
        document = _decoded_document()
        document.pages[1].lines[3:] = [
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "(1) A. Author, First synthetic reference."),
            _line(2, 390, "439."),
            _line(2, 400, "(2) B. Author, Second synthetic reference."),
        ]

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                {"source_path": PDF_RELATIVE_PATH},
            )

        self.assertEqual(
            [block.block_id for block in article.references],
            ["reference-001", "reference-002"],
        )
        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "(1) A. Author, First synthetic reference. 439.",
                "(2) B. Author, Second synthetic reference.",
            ],
        )

    def test_closing_parenthesis_pdf_references_are_contiguous_entries(self) -> None:
        document = _decoded_document()
        document.pages[1].lines[3:] = [
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "1) A. Author, First synthetic reference."),
            _line(2, 390, "439."),
            _line(2, 400, "2) B. Author, Second synthetic reference."),
        ]

        with patch(
            "scripts.extraction.pdf_article_extractor.extract_pdf_text",
            return_value=document,
        ):
            article = extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                {"source_path": PDF_RELATIVE_PATH},
            )

        self.assertEqual(
            [block.block_id for block in article.references],
            ["reference-001", "reference-002"],
        )
        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "1) A. Author, First synthetic reference. 439.",
                "2) B. Author, Second synthetic reference.",
            ],
        )

    def test_fullwidth_closing_parenthesis_ocr_references_are_contiguous(self) -> None:
        document = _decoded_document()
        document.pages[1].lines[3:] = [
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "1） A. Author, First OCR reference."),
            _line(2, 400, "2） B. Author, Second OCR reference."),
        ]

        article = extract_pdf_article(
            Path("synthetic.pdf"),
            PDF_RELATIVE_PATH,
            _metadata(),
            {"source_path": PDF_RELATIVE_PATH},
            text_document=document,
        )

        self.assertEqual(
            [block.block_id for block in article.references],
            ["reference-001", "reference-002"],
        )

    def test_unnumbered_author_year_references_use_reviewable_hanging_indents(self) -> None:
        document = _decoded_document()
        document.pages[1].lines[3:] = [
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "Alpha A, Beta B (2001) First reference.", left=72),
            _line(2, 390, "Journal 1:1–10", left=84),
            _line(2, 400, "Bravo B (2002a) Second reference.", left=72),
            _line(2, 410, "Journal 2:20–30", left=84),
        ]
        document.pages.append(
            PdfTextPage(
                page=3,
                width=595.0,
                height=709.0,
                classification="native_text",
                lines=[
                    _line(3, 45, "Continued title text.", left=84),
                    _line(3, 55, "Charlie C (2003) Third reference.", left=72),
                ],
            )
        )

        article = extract_pdf_article(
            Path("synthetic.pdf"),
            PDF_RELATIVE_PATH,
            _metadata(),
            {"source_path": PDF_RELATIVE_PATH},
            text_document=document,
        )

        self.assertEqual(
            [block.block_id for block in article.references],
            ["reference-001", "reference-002", "reference-003"],
        )
        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "Alpha A, Beta B (2001) First reference. Journal 1:1–10",
                "Bravo B (2002a) Second reference. Journal 2:20–30 Continued title text.",
                "Charlie C (2003) Third reference.",
            ],
        )

    def test_unnumbered_reference_text_without_author_year_start_fails_closed(self) -> None:
        document = _decoded_document()
        document.pages[1].lines[3:] = [
            _line(2, 355, "REFERENCES", markdown="**References**"),
            _line(2, 380, "Unreviewed unnumbered bibliography prose."),
        ]

        with self.assertRaisesRegex(
            PdfArticleExtractionError,
            "first numbered or author-year entry",
        ):
            extract_pdf_article(
                Path("synthetic.pdf"),
                PDF_RELATIVE_PATH,
                _metadata(),
                {"source_path": PDF_RELATIVE_PATH},
                text_document=document,
            )

    def test_reviewed_multi_page_abstract_regions_are_extracted_once(self) -> None:
        document = _decoded_document()
        document.pages[0].lines[:] = [
            _line(1, 30, TITLE),
            _line(1, 45, "by Test Author"),
            _line(1, 105, "Abstract first page."),
        ]
        document.pages[1].lines[:] = [
            _line(2, 45, "Abstract second page."),
            _line(2, 75, "Introduction", markdown="**Introduction**"),
            _line(2, 100, "Article body."),
        ]

        article = extract_pdf_article(
            Path("synthetic.pdf"),
            PDF_RELATIVE_PATH,
            _metadata(),
            {
                "source_path": PDF_RELATIVE_PATH,
                "abstract_regions": [
                    {"page": 1, "box": [70, 100, 510, 120]},
                    {"page": 2, "box": [70, 40, 510, 60]},
                ],
                "exclude_regions": [
                    {
                        "pages": [1],
                        "box": [0, 0, 595, 90],
                        "reason": "reviewed title and author furniture",
                    }
                ],
            },
            text_document=document,
        )

        self.assertEqual(
            article.sections[0].blocks[0].plain_text,
            "Abstract first page. Abstract second page.",
        )
        body = " ".join(
            block.plain_text
            for section in article.sections[1:]
            for block in section.blocks
        )
        self.assertEqual(body, "Article body.")
        self.assertNotIn("Abstract first page", body)
        self.assertNotIn("Abstract second page", body)

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

    def test_unstyled_numbered_procedure_is_not_a_section_heading(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="image_only",
                    lines=[
                        _line(1, 30, TITLE),
                        _line(1, 45, "by Test Author"),
                        _line(1, 75, "Introduction"),
                        _line(
                            1,
                            100,
                            "1. Prepare a 1.14× DNA solution. Add buffer.",
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

        introduction = next(
            section for section in article.sections if section.heading == "Introduction"
        )
        self.assertEqual(len(introduction.blocks), 1)
        self.assertEqual(
            introduction.blocks[0].plain_text,
            "1. Prepare a 1.14× DNA solution. Add buffer.",
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

    def test_ordered_ocr_column_transition_ignores_geometry_and_ocr_punctuation(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[
                PdfTextPage(
                    page=1,
                    width=595.0,
                    height=709.0,
                    classification="ocr_text",
                    lines=[
                        _line(
                            1,
                            90,
                            "1. Results. The source line ends with a comma.",
                            markdown="**1. Results.** The source line ends with a comma.",
                            source_region_id="left-column",
                        ),
                        _line(
                            1,
                            30,
                            "the sentence continues in the next reviewed column.",
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
            (
                "The source line ends with a comma. the sentence continues "
                "in the next reviewed column."
            ),
        )

    def test_hyphenated_word_continuation_overrides_hanging_indent(self) -> None:
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
                            "1. Methods.",
                            markdown="**1. Methods.**",
                        ),
                        _line(1, 102, "(ii) Polymerase chain re-"),
                        _line(1, 114, "action was used.", left=84.0),
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
            "(ii) Polymerase chain re-action was used.",
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

    def test_singular_reference_heading_routes_natural_and_configured_entries(self) -> None:
        for title in ("REFERENCE", "Reference", "rEfErEnCe"):
            for configured in (False, True):
                with self.subTest(title=title, configured=configured):
                    document = PdfTextDocument(
                        relative_path=PDF_RELATIVE_PATH,
                        pages=[PdfTextPage(
                            page=1,
                            width=595.0,
                            height=709.0,
                            classification="native_text",
                            lines=[
                                _line(1, 30, TITLE),
                                _line(1, 45, "by Test Author"),
                                _line(1, 90, "1. Results. Main result.",
                                      markdown="**1. Results.** Main result."),
                                _line(1, 120, title),
                                _line(1, 140, "[1] A. Author, First reference."),
                                _line(1, 160, "[2] B. Author, Second reference."),
                            ],
                        )],
                        diagnostic_rows=[],
                        warnings=[],
                        all_pages_classified=True,
                    )
                    config = {}
                    if configured:
                        config["section_headings"] = [{
                            "page": 1,
                            "box": [70, 115, 501, 135],
                            "title": title,
                        }]

                    article = extract_pdf_article(
                        Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata(),
                        config=config, text_document=document,
                    )

                    self.assertEqual(
                        [block.plain_text for block in article.references],
                        ["[1] A. Author, First reference.",
                         "[2] B. Author, Second reference."],
                    )
                    self.assertEqual(
                        [block.plain_text for section in article.sections
                         for block in section.blocks],
                        ["Main result."],
                    )
                    self.assertFalse(any(
                        section.heading.casefold() == "reference"
                        for section in article.sections
                    ))

    def test_singular_same_line_reference_heading_preserves_first_entry(self) -> None:
        document = PdfTextDocument(
            relative_path=PDF_RELATIVE_PATH,
            pages=[PdfTextPage(
                page=1,
                width=595.0,
                height=709.0,
                classification="native_text",
                lines=[
                    _line(1, 30, TITLE),
                    _line(1, 45, "by Test Author"),
                    _line(1, 90, "1. Results. Main result.",
                          markdown="**1. Results.** Main result."),
                    _line(1, 120, "Reference [1] A. Author, First reference.",
                          markdown="**Reference** [1] A. Author, First reference."),
                    _line(1, 140, "[2] B. Author, Second reference."),
                ],
            )],
            diagnostic_rows=[],
            warnings=[],
            all_pages_classified=True,
        )

        article = extract_pdf_article(
            Path("synthetic.pdf"), PDF_RELATIVE_PATH, _metadata(),
            text_document=document,
        )

        self.assertEqual(
            [block.plain_text for block in article.references],
            ["[1] A. Author, First reference.", "[2] B. Author, Second reference."],
        )
        self.assertFalse(any(
            section.heading.casefold() == "reference" for section in article.sections
        ))

    def test_reviewed_reference_start_retains_prefatory_notes_as_article_text(self) -> None:
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
                        _line(
                            1,
                            80,
                            "1. Results. Main result.",
                            markdown="**1. Results.** Main result.",
                            source_region_id="native-left-column",
                        ),
                        _line(1, 110, "Notes and references"),
                        _line(1, 130, "The authors declare no competing interest."),
                        _line(1, 150, "[1] A. Author, First reference."),
                        _line(1, 170, "[2] B. Author, Second reference."),
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
            config={
                "section_headings": [
                    {
                        "page": 1,
                        "box": [70, 105, 501, 125],
                        "title": "Notes and references",
                        "level": 2,
                    }
                ],
                "reference_start": {"page": 1, "box": [70, 145, 501, 165]},
            },
            text_document=document,
        )

        notes = next(
            section
            for section in article.sections
            if section.heading == "Notes and references"
        )
        self.assertEqual(
            [block.plain_text for block in notes.blocks],
            ["The authors declare no competing interest."],
        )
        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "[1] A. Author, First reference.",
                "[2] B. Author, Second reference.",
            ],
        )
        self.assertTrue(
            any(
                row.get("kind") == "configured_reference_start"
                for row in article.page_diagnostics
            )
        )
        page_summary = next(
            row for row in article.page_diagnostics if row.get("kind") == "page_summary"
        )
        self.assertFalse(page_summary["ocr_performed"])

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
    def test_title_identity_accepts_only_safe_presentation_equivalents(self) -> None:
        article = _article()
        article.title = "Synthetic MMP-9 Article"
        metadata = RecordMetadata(
            record_id=RECORD_ID,
            title="Synthetic MMP\u20109 Article",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )

        _reconcile_article_title(article, metadata)

        self.assertEqual(article.title, metadata.title)
        article.title = "Synthetic pK_{a} Article"
        script_metadata = RecordMetadata(
            record_id=RECORD_ID,
            title="Synthetic pKa Article",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )
        _reconcile_article_title(article, script_metadata)
        self.assertEqual(article.title, script_metadata.title)

        article.title = 'Synthetic “quoted” Author’s Article'
        quote_metadata = RecordMetadata(
            record_id=RECORD_ID,
            title='Synthetic "quoted" Author\'s Article',
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )
        _reconcile_article_title(article, quote_metadata)
        self.assertEqual(article.title, quote_metadata.title)

        article.title = "Synthetic pK_{b} Article"
        with self.assertRaisesRegex(ExtractionError, "title does not match"):
            _reconcile_article_title(article, script_metadata)

        article.title = "Synthetic MMP\u20139 Article"
        with self.assertRaisesRegex(ExtractionError, "title does not match"):
            _reconcile_article_title(article, metadata)

        article.title = "Synthetic «quoted» Author's Article"
        with self.assertRaisesRegex(ExtractionError, "title does not match"):
            _reconcile_article_title(article, quote_metadata)

    def test_title_identity_accepts_terminal_stop_with_cite_card_doi(self) -> None:
        article = _article()
        article.title = "Synthetic Article"
        metadata = RecordMetadata(
            record_id=RECORD_ID,
            title="Synthetic Article.",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )

        with TemporaryDirectory() as temporary:
            source_html = Path(temporary) / "main.html"
            source_html.write_text(
                '<html><body><div id="getCitation">'
                '<a href="https://doi.org/10.0000/synthetic">DOI</a>'
                "</div></body></html>",
                encoding="utf-8",
            )
            _reconcile_article_title(article, metadata, source_html=source_html)

        self.assertEqual(article.title, metadata.title)

    def test_title_identity_accepts_unicode_minus_as_hyphen_with_cite_card_doi(self) -> None:
        article = _article()
        article.title = "Synthetic Polyamide\u2212DNA Article"
        metadata = RecordMetadata(
            record_id=RECORD_ID,
            title="Synthetic Polyamide-DNA Article",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )

        with TemporaryDirectory() as temporary:
            source_html = Path(temporary) / "main.html"
            source_html.write_text(
                '<html><body><div id="getCitation">'
                '<a href="https://doi.org/10.0000/synthetic">DOI</a>'
                "</div></body></html>",
                encoding="utf-8",
            )
            _reconcile_article_title(article, metadata, source_html=source_html)

        self.assertEqual(article.title, metadata.title)

    def test_title_identity_rejects_unicode_minus_with_reference_doi_only(self) -> None:
        article = _article()
        article.title = "Synthetic Polyamide\u2212DNA Article"
        metadata = RecordMetadata(
            record_id=RECORD_ID,
            title="Synthetic Polyamide-DNA Article",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )

        with TemporaryDirectory() as temporary:
            source_html = Path(temporary) / "main.html"
            source_html.write_text(
                '<html><body><div id="references">'
                '<a href="https://doi.org/10.0000/synthetic">DOI</a>'
                "</div></body></html>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtractionError, "title does not match"):
                _reconcile_article_title(article, metadata, source_html=source_html)

    def test_title_identity_rejects_terminal_stop_with_reference_doi_only(self) -> None:
        article = _article()
        article.title = "Synthetic Article"
        metadata = RecordMetadata(
            record_id=RECORD_ID,
            title="Synthetic Article.",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/synthetic",
            document_type="research article",
        )

        with TemporaryDirectory() as temporary:
            source_html = Path(temporary) / "main.html"
            source_html.write_text(
                '<html><body><div id="references">'
                '<a href="https://doi.org/10.0000/synthetic">DOI</a>'
                "</div></body></html>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExtractionError, "title does not match"):
                _reconcile_article_title(article, metadata, source_html=source_html)

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

    def _run(self, *, include_html: bool, include_pdf_supplement: bool = False):
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
        if include_pdf_supplement:
            sources.append(
                self._source(
                    root,
                    "supplement",
                    f"papers (private)/{RECORD_ID}/supplementary/figures.pdf",
                    b"%PDF-image-only-supplement",
                    "application/pdf",
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
        _result, article, mocks = self._run(
            include_html=True, include_pdf_supplement=True
        )
        pdf_extractor = mocks[3]
        html_extractor = mocks[4]
        pdf_text_verifier = mocks[12]

        html_extractor.assert_called_once()
        pdf_extractor.assert_not_called()
        self.assertEqual(html_extractor.call_args.args[1], HTML_RELATIVE_PATH)
        self.assertIn(
            "automated_pdf_html_alignment_not_implemented",
            {warning["code"] for warning in article.warnings},
        )
        verified_sources = pdf_text_verifier.call_args.args[1]
        self.assertEqual([source.role for source in verified_sources], ["main_pdf"])

    @unittest.skipUnless(os.name == "nt", "Windows ACL regression test")
    def test_staged_run_keeps_parent_acl_inheritance_on_windows(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is required to inspect the Windows ACL")

        result, _article, _mocks = self._run(include_html=False)
        literal_path = str(result.run_root).replace("'", "''")
        inspection = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::Out.Write((Get-Acl -LiteralPath "
                f"'{literal_path}').AreAccessRulesProtected)",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(inspection.returncode, 0, inspection.stderr)
        self.assertEqual(inspection.stdout.strip().casefold(), "false")


if __name__ == "__main__":
    unittest.main()
