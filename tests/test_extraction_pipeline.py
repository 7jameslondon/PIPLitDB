from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

try:
    from lxml import html as lxml_html
except ModuleNotFoundError:
    lxml_html = None

from scripts.extraction.paths import (
    UnsafePathError,
    canonical_json,
    ensure_within,
    sha256_file,
    validate_record_id,
)
from scripts.extraction.models import (
    ContentBlock,
    EmbeddedAsset,
    FigureItem,
    SourceFile,
    SupplementExtraction,
    TableCell,
    TableItem,
    TablePart,
)
from scripts.extraction.metadata import RecordMetadata, load_record_metadata
from scripts.extraction.pipeline import (
    ExtractionError,
    _apply_front_matter_overrides,
    _apply_main_pdf_visual_additions,
    _apply_main_table_overrides,
    _apply_section_equation_additions,
    _apply_supplement_table_overrides,
    _apply_reference_entries,
    _apply_supplement_block_additions,
    _apply_supporting_information_additions,
    _attach_assets,
    _bind_html_supplement_figures,
    _crop_specs_for_pdf_source,
    _default_override_path,
    _effective_crop_specs,
    _record_missing_asset_warnings,
    _reconcile_article_title,
    _project_pdf_scientific_markup,
    _remove_duplicate_main_figures_promoted_from_supplements,
    _remove_redundant_supporting_figure_captions,
    _remove_redundant_supplement_table_titles,
    _resolve_override_path,
    _validate_crop_sources,
    _validate_text_repair_sources,
)
from scripts.extraction.renderer import typed_table_footnotes, write_table_derivatives
from scripts.extraction.rich_text import (
    block_markup_to_safe_html,
    plain_text_from_safe_html,
    rich_text_matches_plain,
)
from scripts.extraction.table_schema import TableSchemaError, validate_table_payload
from scripts.extraction.supplements import extract_supplements
from scripts.extraction.validation import (
    _required_asset_findings,
    _table_derivative_findings,
    validate_candidate,
)

if lxml_html is not None:
    from scripts.extraction.html_extractor import (
        _flat_acs_snapshot_root,
        _flat_sciencedirect_snapshot_root,
        _flat_wiley_snapshot_root,
        _display_equation_containers,
        _requires_huge_html_parser,
        _segmented_inline_content,
        _sciencedirect_reference,
        extract_html,
    )
else:  # pragma: no cover - documents the optional dependency boundary
    extract_html = None


class ExtractionMetadataTests(unittest.TestCase):
    def test_pdf_scientific_plain_tokens_receive_rich_projection(self) -> None:
        pdf_path = "papers (private)/00001/pdf/main.pdf"
        pdf_block = ContentBlock(
            block_id="main-paragraph-0001",
            kind="paragraph",
            markdown="K_{a}=10^{9} M^{−1}",
            plain_text="K_{a}=10^{9} M^{−1}",
            source_path=pdf_path,
            source_locator="PDF page 1, box [1, 1, 2, 2]",
        )
        reviewed_block = ContentBlock(
            block_id="main-paragraph-0002",
            kind="paragraph",
            markdown="<em>K</em>_{a}",
            plain_text="K_{a}",
            source_path=pdf_path,
            source_locator="PDF page 1, box [1, 2, 2, 3]",
        )
        article = SimpleNamespace(
            sections=[
                SimpleNamespace(
                    heading="Preparation of ^{32}P",
                    source_path=pdf_path,
                    blocks=[pdf_block, reviewed_block],
                )
            ],
            front_matter=[],
            references=[],
            supporting_information=[],
            figures=[],
        )

        _project_pdf_scientific_markup(article, pdf_path)

        self.assertEqual(article.sections[0].heading, "Preparation of <sup>32</sup>P")
        self.assertEqual(
            pdf_block.markdown,
            "K<sub>a</sub>=10<sup>9</sup> M<sup>−1</sup>",
        )
        self.assertEqual(reviewed_block.markdown, "<em>K</em><sub>a</sub>")

    def test_article_title_identity_accepts_prime_quote_glyph_variants(self) -> None:
        article = SimpleNamespace(title="5′-TG-3′ reader")
        metadata = RecordMetadata(
            record_id="00001",
            title="5‘-TG-3‘ reader",
            authors=("Author",),
            journal="Journal",
            publication_year=2006,
            doi="10.1000/example",
            document_type="research_article",
        )

        _reconcile_article_title(article, metadata)

        self.assertEqual(article.title, metadata.title)

    def test_html_supplement_figures_bind_to_the_only_discovered_supplement(self) -> None:
        source_path = "papers (private)/00001/html/main.html"
        figures = [
            FigureItem(
                figure_id=f"html_supplement_figure_s{number}",
                source_id=f"aep-figure-id{number}",
                label=f"Supplementary Fig. S{number}",
                kind="figure",
                caption_markdown=f"Supplementary Fig. S{number}. Reviewed.",
                caption_plain=f"Supplementary Fig. S{number}. Reviewed.",
                source_path=source_path,
                source_locator=f"/html/body/article/figure[{number}]",
            )
            for number in (1, 2)
        ]
        assets = [
            EmbeddedAsset(
                asset_id=figure.figure_id,
                category="figure",
                label=figure.label,
                media_type="image/png",
                output_path=f"figures/main/{figure.figure_id}.png",
                data=b"pixels",
                source_path=source_path,
                source_locator=figure.source_locator,
            )
            for figure in figures
        ]
        main = FigureItem(
            figure_id="figure_001",
            source_id="fig1",
            label="Figure 1",
            kind="figure",
            caption_markdown="Figure 1. Main.",
            caption_plain="Figure 1. Main.",
            source_path=source_path,
            source_locator="/html/body/article/figure[1]",
        )
        article = SimpleNamespace(
            figures=[main, *figures],
            embedded_assets=[
                EmbeddedAsset(
                    asset_id="figure_001",
                    category="figure",
                    label="Figure 1",
                    media_type="image/png",
                    output_path="figures/main/figure_001.png",
                    data=b"main",
                    source_path=source_path,
                    source_locator=main.source_locator,
                ),
                *assets,
            ],
        )
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=SourceFile(
                role="supplement",
                path=Path("supplement.doc"),
                relative_path="papers (private)/00001/supplementary/supplement.doc",
                size=1,
                sha256="a" * 64,
                detected_format="application/msword",
            ),
            copied_path="supplementary/supplement_001/supplement.doc",
            blocks=[],
            figures=[],
            warnings=[],
        )

        _bind_html_supplement_figures(article, [supplement])

        self.assertEqual(article.figures, [main])
        self.assertEqual(
            [figure.figure_id for figure in supplement.figures],
            ["supplement_001_figure_s1", "supplement_001_figure_s2"],
        )
        self.assertEqual(
            supplement.asset_ids,
            ["supplement_001_figure_s1", "supplement_001_figure_s2"],
        )
        self.assertEqual(
            [asset.category for asset in article.embedded_assets],
            ["figure", "supplement_figure", "supplement_figure"],
        )
        self.assertEqual(
            [asset.output_path for asset in article.embedded_assets[1:]],
            [
                "figures/supplement_001/figure_s1.png",
                "figures/supplement_001/figure_s2.png",
            ],
        )

        unbound_figure = FigureItem(
            figure_id="html_supplement_figure_s9",
            source_id="aep-figure-id9",
            label="Supplementary Fig. S9",
            kind="figure",
            caption_markdown="Supplementary Fig. S9. Reviewed.",
            caption_plain="Supplementary Fig. S9. Reviewed.",
            source_path=source_path,
            source_locator="/html/body/article/figure[9]",
        )
        unbound_asset = EmbeddedAsset(
            asset_id=unbound_figure.figure_id,
            category="figure",
            label=unbound_figure.label,
            media_type="image/png",
            output_path="figures/main/html_supplement_figure_s9.png",
            data=b"pixels",
            source_path=source_path,
            source_locator=unbound_figure.source_locator,
        )
        unbound_article = SimpleNamespace(
            figures=[unbound_figure], embedded_assets=[unbound_asset]
        )

        _bind_html_supplement_figures(
            unbound_article,
            [SimpleNamespace(), SimpleNamespace()],
        )

        self.assertEqual(unbound_article.figures, [unbound_figure])
        self.assertEqual(unbound_figure.kind, "supplement_figure")
        self.assertEqual(
            unbound_article.embedded_assets[0].category, "supplement_figure"
        )
        self.assertEqual(
            unbound_article.embedded_assets[0].output_path,
            "figures/html_supplement/figure_s9.png",
        )

    def test_supplement_table_cell_crop_is_validated_and_attached(self) -> None:
        table = TableItem(
            table_id="supplement_001_table_1",
            source_id="Table 1",
            label="Table 1",
            title_markdown="Table 1",
            title_plain="Table 1",
            parts=[],
            footnotes_markdown=[],
            footnotes_plain=[],
            source_path="papers (private)/00001/supplementary/table1.pdf",
            source_locator="PDF page 1",
            source_kind="image",
        )
        supplement = SimpleNamespace(figures=[], tables=[table])
        article = SimpleNamespace(figures=[], tables=[])
        crop = {
            "asset_id": "supplement_001_table_1_cell_1",
            "category": "table_cell",
            "parent_table_id": table.table_id,
            "compound_id": "part_01_row_002_column_001",
            "output_path": "tables/supplement_001/table_1_cells/cell_1.png",
        }

        self.assertEqual(
            _effective_crop_specs(article, [crop], [supplement]),
            [crop],
        )
        _attach_assets(
            article,
            [supplement],
            [
                {
                    **crop,
                    "output_path": "tables/supplement_001/table_1_cells/cell_1.png",
                }
            ],
        )
        self.assertEqual(
            table.structure_assets,
            {
                "part_01_row_002_column_001": (
                    "tables/supplement_001/table_1_cells/cell_1.png"
                )
            },
        )

    def test_reviewed_standalone_tiff_table_keeps_image_and_semantics(self) -> None:
        try:
            from PIL import Image
        except ModuleNotFoundError:
            self.skipTest("Pillow is not installed")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "table-s1.tif"
            Image.new("RGB", (24, 16), "white").save(source_path, format="TIFF")
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path=(
                    "papers (private)/00001/supplementary/table-s1.tif"
                ),
                size=source_path.stat().st_size,
                sha256=sha256_file(source_path),
                detected_format="image/tiff",
            )
            asset_id = "supplement_001_table_s1"
            render_spec = {
                "source_path": source.relative_path,
                "source_sha256": source.sha256,
                "asset_id": asset_id,
                "label": "Table S1",
                "source_locator": "frame=1;reviewed-image-table",
                "output_path": (
                    "supplementary/supplement_001/tables/table_s1.png"
                ),
                "expected_frames": 1,
                "frame": 1,
                "reason": "The publisher supplied a standalone raster table.",
                "evidence": "The exact TIFF was reviewed cell by cell.",
            }
            extraction_root = root / "extraction"
            supplements = extract_supplements(
                [source],
                extraction_root,
                standalone_image_table_specs=[render_spec],
            )
            supplement = supplements[0]
            self.assertEqual(supplement.figures, [])
            self.assertEqual(len(supplement.assets), 1)
            self.assertEqual(
                supplement.assets[0]["category"], "supplement_image"
            )
            self.assertTrue(
                (extraction_root / render_spec["output_path"]).is_file()
            )

            table_spec = {
                "operation": "add_document_image_table",
                "insert_index": 1,
                "source_path": source.relative_path,
                "source_sha256": source.sha256,
                "source_media_sha256": supplement.assets[0]["sha256"],
                "table_id": asset_id,
                "label": "Table S1",
                "title_plain": "Table S1. Reviewed values.",
                "title_markdown": "<strong>Table S1.</strong> Reviewed values.",
                "parts": [
                    {
                        "rows": [
                            [
                                {"text": "Gene", "header": True},
                                {"text": "Value", "header": True},
                            ],
                            [{"text": "VEGF"}, {"text": "42"}],
                        ]
                    }
                ],
                "footnotes": [],
                "source_locator": "frame=1;reviewed-image-table",
                "reason": "Recover the raster table as semantic cells.",
                "evidence": "The exact TIFF was reviewed cell by cell.",
            }
            _apply_supplement_table_overrides(
                supplements, [table_spec], [source]
            )
            self.assertEqual(supplement.tables[0].source_kind, "image")
            self.assertEqual(supplement.tables[0].parts[0].rows[1][0].text, "VEGF")
            self.assertEqual(supplement.assets[0]["category"], "supplement_table")

    def test_main_pdf_visual_addition_is_review_and_source_hash_gated(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=12,
            sha256="a" * 64,
            detected_format="application/pdf",
        )
        article = SimpleNamespace(figures=[])
        spec = {
            "asset_id": "graphical_abstract",
            "category": "graphical_abstract",
            "label": "Graphical Abstract",
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "page": 4,
            "box": [10, 20, 200, 180],
            "caption_page": 4,
            "caption_box": [220, 20, 400, 180],
            "caption_reviewed_value": "Reviewed source summary.",
            "reason": "The HTML archive omits this authored PDF visual.",
            "evidence": "The reviewed PDF page contains the artwork and summary.",
        }

        _apply_main_pdf_visual_additions(article, [spec], [source])
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].figure_id, "graphical_abstract")
        self.assertEqual(article.figures[0].kind, "graphical_abstract")
        self.assertEqual(article.figures[0].caption_plain, "Reviewed source summary.")
        self.assertIn("visual box [10.00, 20.00, 200.00, 180.00]", article.figures[0].source_locator)

        _apply_main_pdf_visual_additions(article, [spec], [source])
        self.assertEqual(len(article.figures), 1)

        with self.assertRaisesRegex(ExtractionError, "complete reviewed evidence"):
            _apply_main_pdf_visual_additions(
                SimpleNamespace(figures=[]),
                [dict(spec, source_sha256="b" * 64)],
                [source],
            )

    def test_section_equation_addition_is_source_hash_and_anchor_gated(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=12,
            sha256="a" * 64,
            detected_format="application/pdf",
        )
        anchor = ContentBlock(
            block_id="main-paragraph-0001",
            kind="paragraph",
            markdown="Before equation.",
            plain_text="Before equation.",
            source_path="papers (private)/00001/html/main.html",
            source_locator="p[1]",
        )
        section = SimpleNamespace(section_id="section-results", blocks=[anchor])
        article = SimpleNamespace(sections=[section])
        spec = {
            "section_id": "section-results",
            "after_block_id": anchor.block_id,
            "block_id": "main-equation-0001",
            "plain_text": "x_{1}=2 (1)",
            "markdown": "<em>x</em><sub>1</sub>=2 (1)",
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_locator": "page=2;display-equation=1",
            "source_geometry": [{"page": 2, "bbox": [10, 20, 100, 40]}],
            "reason": "The HTML snapshot omits the numbered equation.",
            "evidence": "The reviewed PDF contains the complete display.",
        }

        _apply_section_equation_additions(article, [spec], [source])
        self.assertEqual(
            [block.block_id for block in section.blocks],
            ["main-paragraph-0001", "main-equation-0001"],
        )
        self.assertEqual(section.blocks[1].kind, "equation")

        stale_article = SimpleNamespace(
            sections=[SimpleNamespace(section_id="section-results", blocks=[anchor])]
        )
        with self.assertRaisesRegex(ExtractionError, "complete discovered-source"):
            _apply_section_equation_additions(
                stale_article, [dict(spec, source_sha256="b" * 64)], [source]
            )

        missing_anchor_article = SimpleNamespace(
            sections=[SimpleNamespace(section_id="section-results", blocks=[anchor])]
        )
        with self.assertRaisesRegex(ExtractionError, "anchor did not match once"):
            _apply_section_equation_additions(
                missing_anchor_article,
                [dict(spec, after_block_id="main-paragraph-9999")],
                [source],
            )

    def test_add_document_supplement_table_is_source_hash_gated(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("supplement.pdf"),
            relative_path="papers (private)/00001/supplementary/supplement.pdf",
            size=12,
            sha256="a" * 64,
            detected_format="application/pdf",
        )
        supplement = SimpleNamespace(source=source, tables=[])
        spec = {
            "operation": "add_document_table",
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "table_id": "supplement_001_table_s1",
            "label": "Table S1",
            "title_plain": "Table S1. Native PDF table.",
            "title_markdown": "<strong>Table S1.</strong> Native PDF table.",
            "parts": [
                {
                    "part_id": "supplement_001_table_s1_part_001",
                    "rows": [
                        [{"text": "Header", "header": True}],
                        [{"text": "Value"}],
                    ],
                }
            ],
            "footnotes": [],
            "source_locator": "page=3;native-vector-table",
            "reason": "The generic PDF text stream does not infer table cells.",
            "evidence": "The reviewed PDF contains the native table.",
        }

        _apply_supplement_table_overrides([supplement], [spec], [source])
        self.assertEqual(len(supplement.tables), 1)
        self.assertEqual(supplement.tables[0].source_kind, "document")
        self.assertFalse(supplement.tables[0].requires_source_image)

        with self.assertRaisesRegex(ExtractionError, "invalid supplement_table"):
            _apply_supplement_table_overrides(
                [SimpleNamespace(source=source, tables=[])],
                [dict(spec, source_sha256="b" * 64)],
                [source],
            )

    def test_add_document_image_table_claims_exact_reviewed_visual(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("supplement.doc"),
            relative_path="papers (private)/00001/supplementary/supplement.doc",
            size=12,
            sha256="a" * 64,
            detected_format="application/msword",
        )
        image_sha256 = "b" * 64
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=source,
            copied_path="supplementary/supplement_001/supplement.doc",
            blocks=[],
            figures=[],
            warnings=[],
            assets=[
                {
                    "asset_id": "supplement_001_embedded_visual_001",
                    "category": "supplement_image",
                    "label": "Embedded visual",
                    "output_path": "figures/supplement_001/embedded_visual_001.png",
                    "sha256": image_sha256,
                }
            ],
            asset_ids=["supplement_001_embedded_visual_001"],
        )
        spec = {
            "operation": "add_document_image_table",
            "insert_index": 1,
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_media_sha256": image_sha256,
            "table_id": "supplement_001_table_s2",
            "label": "Table S2",
            "title_plain": "Table S2. Authored image table.",
            "title_markdown": "<strong>Table S2.</strong> Authored image table.",
            "parts": [
                {
                    "rows": [
                        [{"text": "Header", "header": True}],
                        [{"text": "Value"}],
                    ]
                }
            ],
            "footnotes": [],
            "source_locator": "docx-part=word/media/image9.jpeg;body-child=112",
            "reason": "The legacy Word table is authored as one raster drawing.",
            "evidence": "The source hash and visual hash identify it exactly.",
        }

        _apply_supplement_table_overrides([supplement], [spec], [source])

        self.assertEqual(len(supplement.tables), 1)
        self.assertEqual(supplement.tables[0].source_kind, "image")
        self.assertTrue(supplement.tables[0].requires_source_image)
        self.assertEqual(
            supplement.assets[0]["asset_id"], "supplement_001_table_s2"
        )
        self.assertEqual(supplement.assets[0]["category"], "supplement_table")
        self.assertEqual(supplement.asset_ids, ["supplement_001_table_s2"])

        stale = deepcopy(spec)
        stale["source_media_sha256"] = "c" * 64
        with self.assertRaisesRegex(ExtractionError, "exactly one reviewed"):
            _apply_supplement_table_overrides(
                [
                    SupplementExtraction(
                        supplement_id="supplement_001",
                        source=source,
                        copied_path="supplementary/supplement_001/supplement.doc",
                        blocks=[],
                        figures=[],
                        warnings=[],
                        assets=[],
                    )
                ],
                [stale],
                [source],
            )

    def test_pdf_ocr_crop_filter_keeps_only_current_pdf(self) -> None:
        main_path = "papers (private)/00083/main.pdf"
        crops = [
            {"asset_id": "figure_001", "source_path": main_path, "page": 2},
            {
                "asset_id": "supplement_003_figure_002",
                "source_path": "papers (private)/00083/supplementary/supplementary_3.pdf",
                "pages": [{"page": 3}, {"page": 4}],
            },
        ]

        self.assertEqual(
            _crop_specs_for_pdf_source(crops, main_path),
            [crops[0]],
        )

    def test_supplement_table_override_is_exactly_source_and_value_gated(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("table.docx"),
            relative_path="papers (private)/00001/supplementary/table.docx",
            size=12,
            sha256="a" * 64,
            detected_format=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        table = TableItem(
            table_id="supplement_001_table_001",
            source_id="Table 1",
            label="Table 1",
            title_markdown="Table 1",
            title_plain="Table 1",
            parts=[
                TablePart(
                    part_id="supplement_001_table_001_part_001",
                    rows=[[TableCell("Header", "Header", True)]],
                )
            ],
            footnotes_markdown=["Supplementary Table 1", "Reagents used"],
            footnotes_plain=["Supplementary Table 1", "Reagents used"],
            source_path=source.relative_path,
            source_locator="docx-part=word/document.xml;table=1",
            source_kind="document",
        )
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=source,
            copied_path="supplementary/supplement_001/table.docx",
            blocks=[],
            figures=[],
            warnings=[],
            tables=[table],
        )
        spec = {
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "table_index": 1,
            "expected_label": "Table 1",
            "expected_title_plain": "Table 1",
            "expected_title_markdown": "Table 1",
            "expected_footnotes_plain": ["Supplementary Table 1", "Reagents used"],
            "expected_footnotes_markdown": ["Supplementary Table 1", "Reagents used"],
            "label": "Table S1",
            "title_plain": "Supplementary Table 1 — Reagents used",
            "title_markdown": "<strong>Supplementary Table 1.</strong> Reagents used",
            "footnotes_plain": [],
            "footnotes_markdown": [],
            "source_locator": (
                "docx-part=word/document.xml;body-child=1;table=1;"
                "reviewed-title-body-children=9-10"
            ),
            "reason": "The rendered title follows the table in OOXML body order.",
            "evidence": "Exact source hash and parser-produced metadata.",
        }

        _apply_supplement_table_overrides([supplement], [spec], [source])
        self.assertEqual(table.label, "Table S1")
        self.assertEqual(table.title_plain, "Supplementary Table 1 — Reagents used")
        self.assertEqual(table.footnotes_plain, [])

        near_miss = deepcopy(spec)
        near_miss["expected_footnotes_plain"] = ["different"]
        with self.assertRaisesRegex(
            ExtractionError, "did not match parsed table"
        ):
            _apply_supplement_table_overrides(
                [
                    SupplementExtraction(
                        supplement_id="supplement_001",
                        source=source,
                        copied_path="supplementary/supplement_001/table.docx",
                        blocks=[],
                        figures=[],
                        warnings=[],
                        tables=[
                            TableItem(
                                table_id="supplement_001_table_001",
                                source_id="Table 1",
                                label="Table 1",
                                title_markdown="Table 1",
                                title_plain="Table 1",
                                parts=table.parts,
                                footnotes_markdown=[
                                    "Supplementary Table 1",
                                    "Reagents used",
                                ],
                                footnotes_plain=[
                                    "Supplementary Table 1",
                                    "Reagents used",
                                ],
                                source_path=source.relative_path,
                                source_locator=(
                                    "docx-part=word/document.xml;table=1"
                                ),
                                source_kind="document",
                            )
                        ],
                    )
                ],
                [near_miss],
                [source],
            )

    def test_reviewed_image_only_presentation_table_is_hash_gated(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("tables.pptx"),
            relative_path="papers (private)/00001/supplementary/tables.pptx",
            size=12,
            sha256="a" * 64,
            detected_format=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )
        title_source = SourceFile(
            role="supplement",
            path=Path("legends.docx"),
            relative_path="papers (private)/00001/supplementary/legends.docx",
            size=12,
            sha256="c" * 64,
            detected_format=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        table_id = "supplement_001_slide_001_render"

        def make_supplement() -> SupplementExtraction:
            return SupplementExtraction(
                supplement_id="supplement_001",
                source=source,
                copied_path="supplementary/supplement_001/tables.pptx",
                blocks=[],
                figures=[],
                warnings=[],
                assets=[
                    {
                        "asset_id": table_id,
                        "category": "supplement_slide_render",
                        "output_path": (
                            "supplementary/supplement_001/figures/slide-001.png"
                        ),
                        "presentation_slide_number": 1,
                        "presentation_slide_count": 1,
                    },
                    {
                        "asset_id": "supplement_001_media_001",
                        "category": "supplement_image",
                        "sha256": "b" * 64,
                        "presentation_slide_numbers": [1],
                        "parent_id": "supplement_001",
                    },
                ],
                asset_ids=[table_id, "supplement_001_media_001"],
            )

        spec = {
            "operation": "add_image_table",
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "title_source_path": title_source.relative_path,
            "title_source_sha256": title_source.sha256,
            "slide_number": 1,
            "expected_slide_count": 1,
            "source_media_sha256": "b" * 64,
            "table_id": table_id,
            "label": "Supplementary Table 1",
            "title_plain": "Reviewed table",
            "title_markdown": "Reviewed <em>table</em>",
            "parts": [
                {
                    "rows": [
                        [
                            {"text": "Gene", "header": True},
                            {"text": "Value", "header": True},
                        ],
                        [{"text": "VEGF"}, {"text": "42.9"}],
                    ]
                }
            ],
            "footnotes": [],
            "source_locator": "slide=1;reviewed-image-table",
            "reason": "The publisher supplied the table as one rasterized slide.",
            "evidence": "Exact deck, legend, and embedded-image hashes were reviewed.",
        }

        supplement = make_supplement()
        _apply_supplement_table_overrides(
            [supplement], [spec], [source, title_source]
        )
        self.assertEqual(len(supplement.tables), 1)
        self.assertEqual(supplement.tables[0].table_id, table_id)
        self.assertEqual(supplement.tables[0].source_kind, "image")
        self.assertEqual(supplement.tables[0].parts[0].rows[1][0].text, "VEGF")
        self.assertEqual(supplement.assets[0]["category"], "supplement_table")
        self.assertEqual(supplement.assets[1]["parent_id"], table_id)

        stale = dict(spec, source_media_sha256="d" * 64)
        with self.assertRaisesRegex(ExtractionError, "did not match"):
            _apply_supplement_table_overrides(
                [make_supplement()], [stale], [source, title_source]
            )

    def test_reviewed_supplement_table_cell_repair_is_hash_and_value_gated(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("supplement.doc"),
            relative_path="papers (private)/00001/supplementary/supplement.doc",
            size=12,
            sha256="a" * 64,
            detected_format="application/msword",
        )

        def make_supplement() -> SupplementExtraction:
            return SupplementExtraction(
                supplement_id="supplement_001",
                source=source,
                copied_path="supplementary/supplement_001/supplement.doc",
                blocks=[],
                figures=[],
                warnings=[],
                tables=[
                    TableItem(
                        table_id="supplement_001_table_s5",
                        source_id="Table S5",
                        label="Table S5",
                        title_markdown="Table S5",
                        title_plain="Table S5",
                        parts=[
                            TablePart(
                                part_id="supplement_001_table_s5_part_001",
                                rows=[
                                    [TableCell("Specificity", "Specificity", True)],
                                    [
                                        TableCell(
                                            "^{654}",
                                            "<strong><sup>654</sup></strong>",
                                            False,
                                        )
                                    ],
                                ],
                            )
                        ],
                        footnotes_markdown=[],
                        footnotes_plain=[],
                        source_path=source.relative_path,
                        source_locator="docx-part=word/document.xml;table=5",
                        source_kind="document",
                    )
                ],
            )

        spec = {
            "operation": "repair_cell",
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "table_index": 1,
            "expected_table_id": "supplement_001_table_s5",
            "part_index": 1,
            "row_index": 2,
            "cell_index": 1,
            "expected_text": "^{654}",
            "expected_markdown": "<strong><sup>654</sup></strong>",
            "text": "654",
            "markdown": "<strong>654</strong>",
            "reason": "The rendered source shows an ordinary bold value.",
            "evidence": "Exact source hash and reviewed rendered page.",
        }

        supplement = make_supplement()
        _apply_supplement_table_overrides([supplement], [spec], [source])
        repaired = supplement.tables[0].parts[0].rows[1][0]
        self.assertEqual((repaired.text, repaired.markdown), ("654", "<strong>654</strong>"))

        stale = dict(spec, expected_text="654")
        with self.assertRaisesRegex(ExtractionError, "did not match parsed table cell"):
            _apply_supplement_table_overrides(
                [make_supplement()], [stale], [source]
            )

    def test_source_pinned_text_repair_rejects_stale_snapshot(self) -> None:
        source = SourceFile(
            role="main_html",
            path=Path("article.html"),
            relative_path="papers (private)/00001/html/main.html",
            size=12,
            sha256="a" * 64,
            detected_format="text/html",
        )
        spec = {
            "pattern": "exact source fragment",
            "replacement": "reviewed replacement",
            "expected_matches": 1,
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "reason": "Exact reviewed repair.",
            "evidence": "Exact source locator.",
        }

        _validate_text_repair_sources([spec], [source], source)
        with self.assertRaisesRegex(ExtractionError, "source-pinned text_repairs"):
            _validate_text_repair_sources(
                [dict(spec, source_sha256="b" * 64)], [source], source
            )

    def test_pdf_crop_source_hash_is_checked_when_declared(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("supplement.pdf"),
            relative_path="papers (private)/00001/supplementary/supplement.pdf",
            size=12,
            sha256="a" * 64,
            detected_format="application/pdf",
        )
        spec = {
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
        }

        _validate_crop_sources([spec], [source])
        with self.assertRaisesRegex(ExtractionError, "source_sha256 does not match"):
            _validate_crop_sources(
                [dict(spec, source_sha256="b" * 64)], [source]
            )
        # Existing reviewed crop files without a per-crop digest remain
        # compatible; the extraction-wide source snapshot gate still applies.
        _validate_crop_sources(
            [{"source_path": source.relative_path}], [source]
        )

    def test_supporting_addition_is_source_hash_gated(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("source.jpg"),
            relative_path="papers (private)/00001/supplementary/source.jpg",
            size=3,
            sha256="a" * 64,
            detected_format="image/jpeg",
        )
        article = SimpleNamespace(supporting_information=[])
        spec = {
            "value": "Original data image: full-size source panels.",
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_locator": "full-image; reviewed visually",
            "reason": "Label the otherwise opaque image supplement.",
            "evidence": "The complete image was inspected at original resolution.",
        }

        _apply_supporting_information_additions(article, [spec], [source])

        self.assertEqual(
            article.supporting_information[0].plain_text,
            "Original data image: full-size source panels.",
        )
        stale = dict(spec, source_sha256="b" * 64)
        with self.assertRaisesRegex(ExtractionError, "invalid supporting_information"):
            _apply_supporting_information_additions(
                SimpleNamespace(supporting_information=[]), [stale], [source]
            )

        legacy = {
            "value": "Publisher-listed Table S1 is absent from the archive.",
            "source_path": source.relative_path,
            "source_locator": "publisher supplement index; reviewed",
        }
        legacy_article = SimpleNamespace(supporting_information=[])
        _apply_supporting_information_additions(
            legacy_article, [legacy], [source]
        )
        self.assertEqual(
            legacy_article.supporting_information[0].plain_text,
            legacy["value"],
        )

        partial = dict(legacy, reason="Only one modern evidence field is present.")
        with self.assertRaisesRegex(ExtractionError, "invalid supporting_information"):
            _apply_supporting_information_additions(
                SimpleNamespace(supporting_information=[]), [partial], [source]
            )

    def test_supplement_block_addition_pins_target_and_evidence_sources(self) -> None:
        supplement_source = SourceFile(
            role="supplement",
            path=Path("movie.mp4"),
            relative_path="papers (private)/00001/supplementary/movie.mp4",
            size=3,
            sha256="a" * 64,
            detected_format="video/mp4",
        )
        html_source = SourceFile(
            role="main_html",
            path=Path("main.html"),
            relative_path="papers (private)/00001/html/main.html",
            size=4,
            sha256="b" * 64,
            detected_format="text/html",
        )
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=supplement_source,
            copied_path="supplementary/supplement_001/movie.mp4",
            blocks=[],
            figures=[],
            warnings=[],
        )
        spec = {
            "supplement_path": supplement_source.relative_path,
            "supplement_sha256": supplement_source.sha256,
            "value": "Movie S1. Live imaging of control cells.",
            "source_path": html_source.relative_path,
            "source_sha256": html_source.sha256,
            "source_locator": "supplementary-data;movie=S1",
            "reason": "The label and description live only in publisher HTML.",
            "evidence": "The link target is the preserved movie supplement.",
        }

        _apply_supplement_block_additions(
            [supplement], [spec], [supplement_source, html_source]
        )

        self.assertEqual(len(supplement.blocks), 1)
        self.assertEqual(supplement.blocks[0].kind, "supplement_description")
        self.assertEqual(supplement.blocks[0].plain_text, spec["value"])
        self.assertEqual(supplement.blocks[0].source_path, html_source.relative_path)

        for stale in (
            dict(spec, supplement_sha256="c" * 64),
            dict(spec, source_sha256="c" * 64),
        ):
            with self.assertRaisesRegex(
                ExtractionError, "invalid supplement_block_additions"
            ):
                _apply_supplement_block_additions(
                    [
                        SupplementExtraction(
                            supplement_id="supplement_001",
                            source=supplement_source,
                            copied_path="supplementary/supplement_001/movie.mp4",
                            blocks=[],
                            figures=[],
                            warnings=[],
                        )
                    ],
                    [stale],
                    [supplement_source, html_source],
                )

    def test_override_path_may_use_only_same_record_staging(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Path(directory)
            record_root = repository / "papers (private)" / "00017"
            record_staging = repository / "papers (private)" / "staging" / "00017"
            other_staging = repository / "papers (private)" / "staging" / "00018"
            record_root.mkdir(parents=True)
            record_staging.mkdir(parents=True)
            other_staging.mkdir(parents=True)
            reviewed = record_staging / "source-audit" / "extraction_diagnostic" / "override.yaml"
            reviewed.parent.mkdir(parents=True)
            reviewed.write_text("schema_version: '1.0'\n", encoding="utf-8")
            other = other_staging / "override.yaml"
            other.write_text("schema_version: '1.0'\n", encoding="utf-8")
            outside = repository / "override.yaml"
            outside.write_text("schema_version: '1.0'\n", encoding="utf-8")

            self.assertEqual(
                _resolve_override_path(
                    reviewed.relative_to(repository),
                    repository_root=repository,
                    record_root=record_root,
                    record_staging=record_staging,
                ),
                reviewed.resolve(),
            )
            for disallowed in (other, outside):
                with self.subTest(disallowed=disallowed):
                    with self.assertRaises(ExtractionError):
                        _resolve_override_path(
                            disallowed,
                            repository_root=repository,
                            record_root=record_root,
                            record_staging=record_staging,
                        )

    def test_default_override_uses_working_copy_then_approved_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            record_root = Path(directory) / "papers (private)" / "00017"
            record_root.mkdir(parents=True)
            self.assertIsNone(_default_override_path(record_root))

            diagnostic_override = (
                record_root / "extraction_diagnostic" / "overrides.yaml"
            )
            diagnostic_override.parent.mkdir()
            diagnostic_override.write_text(
                'schema_version: "1.0"\nrecord_id: "00017"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                _default_override_path(record_root), diagnostic_override
            )

            working_override = record_root / "extraction_overrides.yaml"
            working_override.write_text(
                diagnostic_override.read_text(encoding="utf-8"), encoding="utf-8"
            )
            self.assertEqual(_default_override_path(record_root), working_override)

    def test_exact_supplement_figure_caption_duplicates_are_removed_only(self) -> None:
        def block(value: str, number: int) -> ContentBlock:
            return ContentBlock(
                block_id=f"supporting-{number}",
                kind="supporting_information",
                markdown=value,
                plain_text=value,
                source_path="html/article.html",
                source_locator=f"section=Supporting Information;p={number}",
            )

        caption_1 = "Figure S1. First caption."
        caption_2 = "Figure S2. Second caption."
        article = SimpleNamespace(
            supporting_information=[
                block(f"{caption_1} {caption_2}", 1),
                block(caption_1, 2),
                block(f"{caption_2} Additional authored note.", 3),
                block("Table S1. Retained caption.", 4),
            ]
        )
        supplements = [
            SimpleNamespace(
                figures=[
                    SimpleNamespace(caption_plain=caption_1),
                    SimpleNamespace(caption_plain=caption_2),
                ]
            )
        ]

        _remove_redundant_supporting_figure_captions(article, supplements)

        self.assertEqual(
            [item.plain_text for item in article.supporting_information],
            [
                f"{caption_2} Additional authored note.",
                "Table S1. Retained caption.",
            ],
        )

    def test_exact_caption_block_inside_supplement_is_removed_only(self) -> None:
        def block(value: str, number: int) -> ContentBlock:
            return ContentBlock(
                block_id=f"supplement-block-{number}",
                kind="figure_caption",
                markdown=value,
                plain_text=value,
                source_path="supplement.pdf",
                source_locator=f"page=2;block={number}",
            )

        caption = "Supplementary Fig. S1. Exact reviewed caption."
        article = SimpleNamespace(supporting_information=[])
        supplement = SimpleNamespace(
            figures=[SimpleNamespace(caption_plain=caption)],
            blocks=[
                block(caption, 1),
                block(f"{caption} Additional authored note.", 2),
            ],
        )

        _remove_redundant_supporting_figure_captions(article, [supplement])

        self.assertEqual(
            [item.plain_text for item in supplement.blocks],
            [f"{caption} Additional authored note."],
        )

    def test_reviewed_standalone_image_suppresses_only_empty_html_duplicate(
        self,
    ) -> None:
        payload = b"exact-publisher-image"
        source = SourceFile(
            role="supplement",
            path=Path("supplement.jpg"),
            relative_path="papers (private)/00001/supplementary/supplement.jpg",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            detected_format="image/jpeg",
        )

        def figure(
            figure_id: str, caption: str, *, kind: str = "figure"
        ) -> FigureItem:
            return FigureItem(
                figure_id=figure_id,
                source_id=figure_id,
                label=figure_id,
                kind=kind,
                caption_markdown=caption,
                caption_plain=caption,
                source_path="html/main.html",
                source_locator=f"figure={figure_id}",
            )

        article = SimpleNamespace(
            figures=[
                figure("supplement-carousel-copy", ""),
                figure(
                    "supplement-carousel-graphical",
                    "",
                    kind="graphical_abstract",
                ),
                figure("authored-main-reuse", "Different authored main caption."),
                figure("unrelated", "Main Figure 2."),
            ],
            embedded_assets=[
                EmbeddedAsset(
                    asset_id="supplement-carousel-copy",
                    category="figure",
                    label="Figure 7",
                    media_type="image/jpeg",
                    output_path="figures/main/copy.jpg",
                    data=payload,
                    source_path="html/main.html",
                    source_locator="figure=copy/img",
                ),
                EmbeddedAsset(
                    asset_id="supplement-carousel-graphical",
                    category="graphical_abstract",
                    label="Graphical Abstract",
                    media_type="image/jpeg",
                    output_path="figures/main/graphical-copy.jpg",
                    data=payload,
                    source_path="html/main.html",
                    source_locator="figure=graphical-copy/img",
                ),
                EmbeddedAsset(
                    asset_id="authored-main-reuse",
                    category="figure",
                    label="Figure 1",
                    media_type="image/jpeg",
                    output_path="figures/main/reuse.jpg",
                    data=payload,
                    source_path="html/main.html",
                    source_locator="figure=reuse/img",
                ),
                EmbeddedAsset(
                    asset_id="unrelated",
                    category="figure",
                    label="Figure 2",
                    media_type="image/jpeg",
                    output_path="figures/main/unrelated.jpg",
                    data=b"different",
                    source_path="html/main.html",
                    source_locator="figure=unrelated/img",
                ),
            ],
        )
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=source,
            copied_path="supplementary/supplement_001/supplement.jpg",
            blocks=[],
            figures=[figure("supplement_001_figure_s1", "Figure S1. Reviewed.")],
            warnings=[],
        )

        _remove_duplicate_main_figures_promoted_from_supplements(
            article, [supplement]
        )

        self.assertEqual(
            [item.figure_id for item in article.figures],
            ["authored-main-reuse", "unrelated"],
        )
        self.assertEqual(
            [item.asset_id for item in article.embedded_assets],
            ["authored-main-reuse", "unrelated"],
        )

    def test_exact_reviewed_supplement_table_title_block_is_removed_only(self) -> None:
        title = "Table S1. Reviewed measurements."
        supplement = SimpleNamespace(
            tables=[SimpleNamespace(title_plain=title, title_markdown=title)],
            blocks=[
                ContentBlock(
                    block_id="title",
                    kind="subsection_heading",
                    markdown=f"**{title}**",
                    plain_text=title,
                    source_path="table.docx",
                    source_locator="paragraph=1",
                ),
                ContentBlock(
                    block_id="note",
                    kind="paragraph",
                    markdown=f"{title} Additional note.",
                    plain_text=f"{title} Additional note.",
                    source_path="table.docx",
                    source_locator="paragraph=2",
                ),
            ],
        )

        _remove_redundant_supplement_table_titles([supplement])

        self.assertEqual(
            [block.plain_text for block in supplement.blocks],
            [f"{title} Additional note."],
        )

    def test_main_table_override_is_hash_pinned_and_gap_safe(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("article.pdf"),
            relative_path="papers (private)/00001/article.pdf",
            size=12,
            sha256="a" * 64,
            detected_format="application/pdf",
        )
        article = SimpleNamespace(tables=[])
        spec = {
            "number": 1,
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_locator": "page=2;table=1",
            "source_kind": "pdf",
            "title_plain": "Reviewed table",
            "title_markdown": "Reviewed <em>table</em>",
            "reason": "Publisher HTML omitted the body.",
            "evidence": "Reviewed against the exact PDF raster and text.",
            "parts": [
                {
                    "rows": [
                        [
                            {"text": "Compound", "header": True},
                            {"text": "Value", "header": True},
                        ],
                        [{"text": "A"}, {"text": "1"}],
                    ]
                }
            ],
            "footnotes": [
                {"plain_text": "[a] Reviewed.", "markdown": "<sup>a</sup> Reviewed."}
            ],
        }

        _apply_main_table_overrides(article, [spec], [source])
        self.assertEqual(article.tables[0].table_id, "table_001")
        self.assertEqual(article.tables[0].source_kind, "pdf")
        self.assertEqual(article.tables[0].parts[0].rows[1][0].text, "A")

        existing = SimpleNamespace(
            tables=[
                TableItem(
                    table_id="table_001",
                    source_id="publisher-table-t001",
                    label="Table 1",
                    title_markdown="Raster title",
                    title_plain="Raster title",
                    parts=[],
                    footnotes_markdown=[],
                    footnotes_plain=[],
                    source_path=source.relative_path,
                    source_locator="html-anchor=publisher-table-t001",
                    source_kind="image",
                )
            ]
        )
        _apply_main_table_overrides(existing, [spec], [source])
        self.assertEqual(existing.tables[0].source_id, "publisher-table-t001")

        html_source = SourceFile(
            role="main_html",
            path=Path("article.html"),
            relative_path="papers (private)/00001/html/main.html",
            size=12,
            sha256="b" * 64,
            detected_format="text/html",
        )
        html_spec = dict(
            spec,
            source_path=html_source.relative_path,
            source_sha256=html_source.sha256,
            source_kind="html",
            supporting_source_path=source.relative_path,
            supporting_source_sha256=source.sha256,
            supporting_source_locator="PDF page 2, Table 1 header",
        )
        _apply_main_table_overrides(existing, [html_spec], [html_source, source])
        self.assertEqual(existing.tables[0].source_kind, "html")

        stale_support = deepcopy(html_spec)
        stale_support["supporting_source_sha256"] = "c" * 64
        with self.assertRaises(ExtractionError):
            _apply_main_table_overrides(existing, [stale_support], [html_source, source])

        stale = deepcopy(spec)
        stale["source_sha256"] = "b" * 64
        with self.assertRaises(ExtractionError):
            _apply_main_table_overrides(SimpleNamespace(tables=[]), [stale], [source])

        gap = deepcopy(spec)
        gap["number"] = 2
        with self.assertRaises(ExtractionError):
            _apply_main_table_overrides(SimpleNamespace(tables=[]), [gap], [source])

    def test_canonical_author_name_is_preferred_for_rendering(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "00001.yaml"
            path.write_text(
                "title: Synthetic article\n"
                "authors:\n"
                '  - name: "Thomas\u2005G. Example"\n'
                '    canonical_name: "Thomas G. Example"\n'
                "publication_year: 2000\n"
                "journal: Synthetic Journal\n"
                "doi: 10.0000/example\n"
                "document_type: research_article\n",
                encoding="utf-8",
            )

            metadata = load_record_metadata(path, "00001")

        self.assertEqual(metadata.authors, ("Thomas G. Example",))

@unittest.skipUnless(lxml_html is not None, "lxml is required for extraction tests")
class HtmlExtractionTests(unittest.TestCase):
    def test_article_heading_beats_earlier_navigation_heading(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><main>
<div><h1>Journal list menu</h1></div>
<article><div><h1>Authored article title</h1></div>
<section><h2>Results</h2><p>Body text.</p></section></article>
</main></body></html>"""
        )

        self.assertEqual(result.title, "Authored article title")

    def test_nested_references_after_wiley_abbreviations_do_not_add_introduction(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Wiley references</h1>
<section><h3>Abbreviations</h3><div><table><tr><th><li>MGB</li></th>
<td><li>minor groove binder</li></td></tr></table></div>
<section><p>Opening article prose.</p></section>
<section id="article-references-section-1"><div>
<h2><div><span>References</span></div></h2><div><ul>
<li><span>1</span><span>Author, A.</span><span>1999</span>
<span>Reference title.</span><i>Journal</i><span>1</span><span>1</span><span>2</span></li>
</ul></div></div></section></section></article></body></html>"""
        )

        self.assertEqual(
            [(section.heading, len(section.blocks)) for section in result.sections],
            [("Abbreviations", 1), ("Introduction", 1)],
        )

    def test_wiley_literature_cited_and_key_references_are_both_retained(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Wiley references</h1>
<section><h2>Results</h2><p>Body text.</p></section>
<section id="article-references-section-1"><div><h2><div>
<span>Literature Cited</span></div></h2><div><ul>
<li><span>Alpha, A.</span> and <span>Beta, B.</span> <span>2001</span>.
<span>First citation.</span><div><a href="https://doi.org/10.1000/alpha">
10.1000/alpha</a><a aria-label="Google Scholar for first citation">Google Scholar</a></div></li>
</ul></div></div></section>
<section id="article-references-section-2"><div><h2><div>
<span>Key References</span></div></h2><div><ul>
<li><span>Gamma, G. 2002. Complete key citation. <i>Journal</i> 2:3-4.</span>
<div><a aria-label="Web of Science for key citation">Web of Science</a></div></li>
</ul></div></div></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.references), 2)
        self.assertTrue(result.references[0].plain_text.startswith("Alpha, A. and Beta, B."))
        self.assertIn("10.1000/alpha", result.references[0].plain_text)
        self.assertEqual(
            result.references[1].plain_text,
            "Gamma, G. 2002. Complete key citation. Journal 2:3-4.",
        )
        self.assertNotIn("Google Scholar", result.references[0].plain_text)
        self.assertNotIn("Web of Science", result.references[1].plain_text)

    def test_lettered_ordered_list_keeps_authored_markers(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Lettered steps</h1>
<section><h2>Protocol</h2><p>Perform these operations:</p>
<ol type="a"><li>First substep.</li><li>Second substep.</li></ol>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[1]
        self.assertEqual(block.kind, "list")
        self.assertEqual(block.markdown, "a. First substep.\nb. Second substep.")
        self.assertEqual(block.plain_text, "a. First substep.\nb. Second substep.")

    def test_reviewed_equation_marker_preserves_mixed_paragraph_order(self) -> None:
        if lxml_html is None:
            self.skipTest("lxml is not installed")
        paragraph = lxml_html.fromstring(
            '<p>Before <span data-extraction-reviewed-equation="true">'
            '<em>G</em><sub>x</sub> = 1</span> after.</p>'
        )
        containers = _display_equation_containers(paragraph)
        self.assertEqual(len(containers), 1)
        segments = _segmented_inline_content(paragraph, containers)
        self.assertEqual([segment[0] for segment in segments], [
            "prose",
            "equation",
            "prose",
        ])
        self.assertEqual(segments[0][2], "Before")
        self.assertEqual(segments[2][2], "after.")

    def test_emphasized_method_div_preserves_reviewed_equation_and_prose(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Equation article</h1>
<h2>Methods</h2><div><em>Thermodynamic Analysis.</em> Before formula
<div><span data-extraction-reviewed-equation="true"><em>G</em> = −RT ln K (1)</span></div>
after formula.</div></article></body></html>"""
        )

        self.assertEqual(
            [(block.kind, block.plain_text) for block in result.sections[0].blocks],
            [
                ("paragraph", "Thermodynamic Analysis. Before formula"),
                ("equation", "G = −RT ln K (1)"),
                ("paragraph", "after formula."),
            ],
        )

    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "article.html"
            source_path.write_text(source, encoding="utf-8")
            return extract_html(source_path, "html/article.html")

    def test_publisher_exponent_caret_artifact_is_canonicalized(self) -> None:
        result = self.extract(
            "<!doctype html><html><body><article><h1>Caret article</h1>"
            "<h2>Methods</h2><p>Fit: 10⁁((Log EC50 − X) × H.</p>"
            "</article></body></html>"
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.plain_text, "Fit: 10^((Log EC50 − X) × H.")
        self.assertEqual(block.markdown, "Fit: 10^((Log EC50 − X) × H.")

    def test_nucleotide_primes_normalize_before_duplex_slash_and_after_styled_n(self) -> None:
        result = self.extract(
            "<!doctype html><html><body><article><h1>Prime article</h1>"
            "<h2>Methods</h2><p>Duplex 5′-GCAT-3‘/5′-ATGC-3′; "
            "<strong>N</strong>‘TG, N·<strong>N</strong>‘: T·A, and N·N‘ = A·T; "
            "‘Duplex 1’.</p></article></body></html>"
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            "Duplex 5′-GCAT-3′/5′-ATGC-3′; N′TG, N·N′: T·A, and "
            "N·N′ = A·T; ‘Duplex 1’.",
        )
        self.assertEqual(
            block.markdown,
            "Duplex 5′-GCAT-3′/5′-ATGC-3′; <strong>N</strong>′TG, "
            "N·<strong>N</strong>′: T·A, and N·N′ = A·T; ‘Duplex 1’.",
        )

    def test_wiley_unstyled_statistical_comparison_matches_plain_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley comparison boundary</h1>
<section><h2>Results</h2>
<p>Treatment changed expression (<i>P</i>&lt;0.05).</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Treatment changed expression (<em>P</em> &lt;0.05).",
        )
        safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
        self.assertEqual(block.plain_text, "Treatment changed expression (P <0.05).")
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_unstyled_statistical_comparison_matches_plain_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Unstyled comparison boundary</h1>
<section><h2>Results</h2>
<p>Treatment changed expression (P&lt;0.01), unlike MAP&lt;0.01.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Treatment changed expression (P &lt;0.01), unlike MAP&lt;0.01.",
        )
        safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
        self.assertEqual(
            block.plain_text,
            "Treatment changed expression (P <0.01), unlike MAP<0.01.",
        )
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_inline_processing_instruction_is_not_authored_content(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1><?xml version="1.0" encoding="UTF-8"?>Archived article title</h1>
<section><h2>Results</h2><p>Authored text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Archived article title")
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored text.")

    def test_exact_flat_plos_snapshot_restores_semantics_and_tiff_display(self) -> None:
        tiff = (
            "SUkqAAgAAAAKAAABBAABAAAAAgAAAAEBBAABAAAAAQAAAAIBAwADAAAAhgAAAAMB"
            "AwABAAAAAQAAAAYBAwABAAAAAgAAABEBBAABAAAAjAAAABUBAwABAAAAAwAAABYB"
            "BAABAAAAAQAAABcBBAABAAAABgAAABwBAwABAAAAAQAAAAAAAAAIAAgACAAMIjgM"
            "Ijg="
        )
        doi = "10.1371/journal.pone.0123456"
        source = f"""<!doctype html><html><body><div>
<div><div><h1 id="artTitle">Exact flat PLOS article</h1>
<ul id="author-list"><li>Author A</li></ul></div>
<div id="floatTitleTop"><h1>Exact flat PLOS article</h1></div></div>
<div id="artText">
<div><h2>Abstract</h2><div><p>Authored summary.</p></div></div>
<div><p><strong>Citation:</strong> Author A (2024) Exact flat PLOS article.
PLOS ONE 19(2): e0123456. https://doi.org/{doi}</p>
<p><strong>Received:</strong> January 1, 2024; <strong>Accepted:</strong>
January 2, 2024; <strong>Published:</strong> February 3, 2024</p></div>
<div id="section1"><h2>Introduction</h2><p>Opening prose.</p></div>
<div id="section2"><h2>Results</h2><p>See Fig 1.</p>
<a id="pone-0123456-g001" name="pone-0123456-g001"></a><div>
<div><a title="Click for larger image" href="article/figure/image?size=medium&amp;id={doi}.g001">
<img alt="Figure 1." src="data:image/tiff;base64,{tiff}"></a><div></div></div>
<div>Download: <a href="article/figure/image?download&amp;size=large&amp;id={doi}.g001">PNG larger image</a>
<a href="article/figure/image?download&amp;size=original&amp;id={doi}.g001">TIFF original image</a></div>
<div>Fig 1. Dose at 10<sup>−5</sup> M.</div><p><a></a></p>
<p>Panel result with <em>P</em> &lt; .05.</p><p></p>
<p><a href="https://doi.org/{doi}.g001">https://doi.org/{doi}.g001</a></p>
</div>
<a id="pone-0123456-t001" name="pone-0123456-t001"></a><div>
<div><a title="Click for larger image" href="article/figure/image?size=medium&amp;id={doi}.t001">
<img alt="thumbnail" src="data:image/tiff;base64,{tiff}"></a><div></div></div>
<div>Download: <a href="article/figure/image?download&amp;size=large&amp;id={doi}.t001">PNG larger image</a>
<a href="article/figure/image?download&amp;size=original&amp;id={doi}.t001">TIFF original image</a></div>
<div><span>Table 1. </span> Binding at 10<sup>−8</sup> M.</div><p></p>
<p>Fold enrichment has a χ<sup>2</sup> statistic.</p><p></p>
<p><a href="https://doi.org/{doi}.t001">https://doi.org/{doi}.t001</a></p>
</div></div>
<div id="section6"><h2>Supporting information</h2><div></div><div>
<a id="pone.0123456.s001"></a><h3><a href="article/file?type=supplementary&amp;id={doi}.s001">S1 Fig. </a>Supporting diagram.</h3>
<p>Authored supporting caption.</p>
<p><a href="https://doi.org/{doi}.s001">https://doi.org/{doi}.s001</a></p>
<p>(TIF)</p></div><div>
<a id="pone.0123456.s002"></a><h3><a href="article/file?type=supplementary&amp;id={doi}.s002">S2 Dataset. </a></h3>
<p><a href="https://doi.org/{doi}.s002">https://doi.org/{doi}.s002</a></p>
<p>(XLSX)</p></div><div>
<a id="pone.0123456.s003"></a><h3><a href="article/file?type=supplementary&amp;id={doi}.s003">S3 Fig. </a>Second diagram.</h3>
<p>Second authored caption.</p>
<p><a href="https://doi.org/{doi}.s003">https://doi.org/{doi}.s003</a></p>
<p>(TIFF)</p></div><div>
<a id="pone.0123456.s004"></a><h3><a href="article/file?type=supplementary&amp;id={doi}.s004">S4 Raw images. </a></h3>
<p><a href="https://doi.org/{doi}.s004">https://doi.org/{doi}.s004</a></p>
<p>(PDF)</p></div><div>
<a id="pone.0123456.s005"></a><h3><a href="article/file?type=supplementary&amp;id={doi}.s005">S5 Table. </a>Supporting table.</h3>
<p>Authored table caption.</p>
<p><a href="https://doi.org/{doi}.s005">https://doi.org/{doi}.s005</a></p>
<p>(PDF)</p></div></div>
<div><h2>References</h2><ol>
<li id="ref1"><span>1.</span><a id="pone.0123456.ref001"></a>Alpha A. First reference.
<ul><li><a href="https://doi.org/10.1000/first">View Article</a></li>
<li><a href="https://scholar.google.test/first">Google Scholar</a></li></ul></li>
<li id="ref2"><span>2.</span><a id="pone.0123456.ref002"></a>Beta B. Second reference.</li>
<li id="ref3"><span>3.</span><a id="pone.0123456.ref003"></a>Gamma G. Third reference.<ul></ul></li>
</ol></div></div></div></body></html>"""

        result = self.extract(source)

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction", "Results"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored summary."],
        )
        self.assertEqual(len(result.front_matter), 2)
        self.assertTrue(result.front_matter[0].plain_text.startswith("Citation:"))
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "19",
                "issue": "2",
                "article_number": "e0123456",
                "date": "February 3, 2024",
                "first_published": "February 3, 2024",
            },
        )
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Dose at 10^{−5} M. Panel result with P < .05.",
        )
        self.assertIn("10<sup>−5</sup> M", result.figures[0].caption_markdown)
        self.assertEqual(len(result.embedded_assets), 2)
        asset = result.embedded_assets[0]
        self.assertEqual(
            (asset.asset_id, asset.media_type, asset.output_path),
            ("figure_001", "image/png", "figures/main/figure_001.png"),
        )
        self.assertTrue(asset.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_id, "pone-0123456-t001")
        self.assertEqual(result.tables[0].source_kind, "image")
        self.assertEqual(
            result.tables[0].title_plain,
            "Table 1. Binding at 10^{−8} M.",
        )
        self.assertEqual(result.tables[0].parts, [])
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["Fold enrichment has a χ^{2} statistic."],
        )
        self.assertEqual(
            result.tables[0].footnotes_markdown,
            ["Fold enrichment has a χ<sup>2</sup> statistic."],
        )
        table_asset = result.embedded_assets[1]
        self.assertEqual(
            (table_asset.asset_id, table_asset.media_type, table_asset.output_path),
            ("table_001", "image/png", "tables/table_001/source.png"),
        )
        self.assertTrue(table_asset.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            [
                "Figure S1. Supporting diagram. Authored supporting caption.",
                "S2 Dataset.",
                "Figure S3. Second diagram. Second authored caption.",
                "S4 Raw images.",
                "S5 Table. Supporting table. Authored table caption.",
            ],
        )
        self.assertEqual(
            result.supporting_information[0].plain_text,
            result.supporting_information[0].markdown,
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Alpha A. First reference.",
                "2. Beta B. Second reference.",
                "3. Gamma G. Third reference.",
            ],
        )
        self.assertNotIn("Google Scholar", result.references[0].plain_text)

        outer_shell = source.replace("<body><div>", "<body><section><div>", 1).replace(
            "</body></html>", "</section></body></html>", 1
        )
        outer_result = self.extract(outer_shell)
        self.assertEqual(
            [section.heading for section in outer_result.sections],
            ["Abstract", "Introduction", "Results"],
        )
        self.assertEqual(len(outer_result.figures), 1)
        self.assertEqual(len(outer_result.tables), 1)
        self.assertEqual(len(outer_result.references), 3)

        archive_result = self.extract(
            source.replace("S2 Dataset. ", "S2 Appendix. ", 1).replace(
                "(XLSX)", "(GZ)", 1
            )
        )
        self.assertEqual(len(archive_result.tables), 1)
        self.assertEqual(len(archive_result.references), 3)
        self.assertEqual(
            archive_result.supporting_information[1].plain_text,
            "S2 Appendix.",
        )

        legacy_file_result = self.extract(
            source.replace("S2 Dataset. ", "File S1. ", 1).replace(
                "(XLSX)", "(DOCX)", 1
            )
        )
        self.assertEqual(len(legacy_file_result.figures), 1)
        self.assertEqual(len(legacy_file_result.tables), 1)
        self.assertEqual(len(legacy_file_result.references), 3)
        self.assertEqual(legacy_file_result.supporting_information[1].plain_text, "File S1.")
        self.assertEqual(len(legacy_file_result.front_matter), 2)

        png_supplement_result = self.extract(
            source.replace("(TIF)", "(PNG)", 1)
        )
        self.assertEqual(len(png_supplement_result.figures), 1)
        self.assertEqual(len(png_supplement_result.references), 3)
        self.assertEqual(
            png_supplement_result.supporting_information[0].plain_text,
            "Figure S1. Supporting diagram. Authored supporting caption.",
        )

        linked_caption_result = self.extract(
            source.replace(
                "Authored supporting caption.",
                'Authored <a href="https://example.invalid/resource">supporting</a> caption.',
                1,
            )
        )
        self.assertEqual(
            linked_caption_result.supporting_information[0].plain_text,
            "Figure S1. Supporting diagram. Authored supporting caption.",
        )
        self.assertIn(
            "[supporting](https://example.invalid/resource)",
            linked_caption_result.supporting_information[0].markdown,
        )

        supporting_cross_reference_result = self.extract(
            source.replace(
                "S5 Table. </a>Supporting table.",
                'S5 Table. </a>Supporting table from '
                '<a href="#pone.0123456.s001">S1 Fig</a>.',
                1,
            )
        )
        self.assertEqual(len(supporting_cross_reference_result.references), 3)
        self.assertEqual(
            supporting_cross_reference_result.supporting_information[-1].plain_text,
            "S5 Table. Supporting table from S1 Fig. Authored table caption.",
        )

        # Current self-contained PLOS exports can retain the publisher card
        # inside a semantic ``figure`` while article sections remain flat.
        # They also use ``S# File`` labels and keep authored identity details
        # in the author popover.
        semantic_document = lxml_html.fromstring(source)
        flat_anchor = semantic_document.get_element_by_id("pone-0123456-g001")
        flat_card = flat_anchor.getnext()
        semantic_figure = lxml_html.Element("figure", id="figure-1")
        for child in flat_card:
            semantic_figure.append(deepcopy(child))
        semantic_figure.xpath(".//img")[0].set("alt", "thumbnail")
        figure_parent = flat_card.getparent()
        figure_parent.replace(flat_card, semantic_figure)
        figure_parent.remove(flat_anchor)
        table_anchor = semantic_document.get_element_by_id("pone-0123456-t001")
        table_card = table_anchor.getnext()
        semantic_table = lxml_html.Element("figure", id="table-1")
        for child in table_card:
            semantic_table.append(deepcopy(child))
        semantic_table.xpath(".//img")[0].set(
            "src",
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII=",
        )
        table_parent = table_card.getparent()
        table_parent.replace(table_card, semantic_table)
        supporting_heading = semantic_document.xpath(
            './/a[contains(@href, ".s004")][starts-with(normalize-space(), "S4 ")]'
        )[0]
        supporting_heading.text = "S4 File. "
        supporting_cross_reference = lxml_html.Element("a")
        supporting_cross_reference.set("href", "#pone-0123456-g001")
        supporting_cross_reference.text = "Fig 1A"
        supporting_heading.getparent().append(supporting_cross_reference)
        author_item = semantic_document.get_element_by_id("author-list").xpath("./li")[0]
        author_item.clear()
        author_link = lxml_html.Element("a")
        author_link.text = "Author A,"
        author_item.append(author_link)
        author_meta = lxml_html.fragment_fromstring(
            '<div id="author-meta-0">'
            '<p><span></span>Contributed equally to this work with: Author A</p>'
            '<p id="authRoles"><span>Roles</span> Investigation, Writing – review &amp; editing</p>'
            '<p id="authCorresponding-0"><span>* E-mail:</span> author@example.test (AA)</p>'
            '<p id="authCurrentAddress-0">Current address: New Institute</p>'
            '<p id="authAffiliations-0"><span>Affiliations</span> Example Institute</p>'
            '<div><p id="authOrcid-0"><a href="https://orcid.org/0000-0000-0000-0001">'
            'https://orcid.org/0000-0000-0000-0001</a></p></div>'
            '</div>'
        )
        author_item.append(author_meta)
        semantic_result = self.extract(
            lxml_html.tostring(semantic_document, encoding="unicode")
        )
        self.assertEqual(
            [section.heading for section in semantic_result.sections],
            ["Abstract", "Introduction", "Results"],
        )
        self.assertEqual(len(semantic_result.figures), 1)
        self.assertEqual(
            semantic_result.figures[0].caption_plain,
            "Dose at 10^{−5} M. Panel result with P < .05.",
        )
        self.assertEqual(len(semantic_result.tables), 1)
        self.assertEqual(len(semantic_result.references), 3)
        self.assertEqual(
            semantic_result.supporting_information[-2].plain_text,
            "S4 File. Fig 1A",
        )
        self.assertEqual(
            semantic_result.supporting_information[-1].plain_text,
            "S5 Table. Supporting table. Authored table caption.",
        )
        semantic_front_matter = [
            block.plain_text for block in semantic_result.front_matter
        ]
        self.assertIn(
            "Affiliation — Author A: Example Institute", semantic_front_matter
        )
        self.assertIn(
            "Author roles — Author A: Investigation, Writing – review & editing",
            semantic_front_matter,
        )
        self.assertIn(
            "Correspondence: author@example.test (AA)", semantic_front_matter
        )
        self.assertIn(
            "ORCID — Author A: https://orcid.org/0000-0000-0000-0001",
            semantic_front_matter,
        )

        # Earlier self-contained PLOS exports label a semantic image with its
        # authored figure number instead of the generic thumbnail label.
        legacy_semantic_document = deepcopy(semantic_document)
        legacy_semantic_document.xpath("//figure[@id='figure-1']//img")[0].set(
            "alt", "Figure 1."
        )
        legacy_semantic_result = self.extract(
            lxml_html.tostring(legacy_semantic_document, encoding="unicode")
        )
        self.assertEqual(len(legacy_semantic_result.figures), 1)
        self.assertEqual(
            legacy_semantic_result.figures[0].caption_plain,
            "Dose at 10^{−5} M. Panel result with P < .05.",
        )
        self.assertEqual(len(legacy_semantic_result.references), 3)
        self.assertEqual(
            legacy_semantic_result.supporting_information[0].plain_text,
            "Figure S1. Supporting diagram. Authored supporting caption.",
        )

        # Some earlier semantic PLOS cards omit the second empty spacer
        # paragraph immediately before the terminal figure DOI.
        compact_semantic_document = deepcopy(legacy_semantic_document)
        compact_figure = compact_semantic_document.xpath(
            "//figure[@id='figure-1']"
        )[0]
        compact_paragraphs = compact_figure.xpath("./p")
        self.assertEqual("".join(compact_paragraphs[-2].itertext()).strip(), "")
        compact_figure.remove(compact_paragraphs[-2])
        compact_semantic_result = self.extract(
            lxml_html.tostring(compact_semantic_document, encoding="unicode")
        )
        self.assertEqual(len(compact_semantic_result.figures), 1)
        self.assertEqual(
            compact_semantic_result.figures[0].caption_plain,
            "Dose at 10^{−5} M. Panel result with P < .05.",
        )
        self.assertEqual(len(compact_semantic_result.references), 3)
        self.assertEqual(
            compact_semantic_result.supporting_information[0].plain_text,
            "Figure S1. Supporting diagram. Authored supporting caption.",
        )

        # Older PLOS body-fragment archives can omit the surrounding title and
        # author shell while retaining the DOI-bound citation and use the
        # earlier Figure/Table/Text S# labels plus legacy DOC attachments.
        body_only_document = deepcopy(compact_semantic_document)
        body_only_root = body_only_document.get_element_by_id("artText")
        first_support_link = body_only_root.xpath(
            './/h3/a[contains(@href, ".s001")]'
        )[0]
        first_support_link.text = "Figure S1. "
        table_support_link = body_only_root.xpath(
            './/h3/a[contains(@href, ".s005")]'
        )[0]
        table_support_link.text = "Table S5. "
        text_support_link = body_only_root.xpath(
            './/h3/a[contains(@href, ".s004")]'
        )[0]
        text_support_link.text = "Text S4. "
        text_support_entry = text_support_link.getparent().getparent()
        format_paragraph = [
            paragraph
            for paragraph in text_support_entry.xpath("./p")
            if "".join(paragraph.itertext()).strip() == "(PDF)"
        ][0]
        format_paragraph.text = "(DOC)"
        table_download = body_only_root.xpath(
            './/figure[@id="table-1"]/div[normalize-space(text())="Download:"]'
        )[0]
        table_download_links = table_download.xpath("./a")
        download_list = lxml_html.Element("ul")
        for link in table_download_links:
            table_download.remove(link)
            item = lxml_html.Element("li")
            item.append(link)
            download_list.append(item)
        table_download.append(download_list)
        body_only_result = self.extract(
            lxml_html.tostring(body_only_root, encoding="unicode")
        )
        self.assertEqual(body_only_result.title, "Exact flat PLOS article")
        self.assertEqual(len(body_only_result.figures), 1)
        self.assertEqual(len(body_only_result.tables), 1)
        self.assertEqual(len(body_only_result.references), 3)
        body_only_text = [
            block.plain_text
            for section in body_only_result.sections
            for block in section.blocks
        ]
        self.assertNotIn("- PNG larger image\n- TIFF original image", body_only_text)
        self.assertNotIn(f"https://doi.org/{doi}.t001", body_only_text)
        self.assertEqual(
            body_only_result.supporting_information[0].plain_text,
            "Figure S1. Supporting diagram. Authored supporting caption.",
        )
        self.assertTrue(
            any(
                block.plain_text.startswith("Citation:")
                for block in body_only_result.front_matter
            )
        )

        bad_support_reference = lxml_html.tostring(
            legacy_semantic_document, encoding="unicode"
        ).replace("#pone-0123456-g001", "https://example.test/figure", 1)
        bad_support_result = self.extract(bad_support_reference)
        self.assertEqual(bad_support_result.references, [])
        self.assertEqual(bad_support_result.front_matter, [])
        self.assertTrue(
            all(not figure.caption_plain for figure in bad_support_result.figures)
        )

        near_miss = source.replace("TIFF original image", "TIFF source image", 1)
        near_result = self.extract(near_miss)
        self.assertEqual(near_result.figures, [])
        self.assertEqual(near_result.references, [])
        self.assertEqual(near_result.front_matter, [])
        self.assertTrue(
            any(
                block.plain_text.startswith("Citation:")
                for block in near_result.sections[0].blocks
            )
        )

        label_near_miss = source.replace(
            "article/file?type=supplementary", "article/file?type=other", 1
        )
        label_near_result = self.extract(label_near_miss)
        self.assertEqual(label_near_result.figures, [])
        self.assertEqual(label_near_result.references, [])
        self.assertEqual(label_near_result.front_matter, [])

    def test_flat_sciencedirect_snapshot_repairs_only_hash_verified_dialect(self) -> None:
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(pixel).decode("ascii")
        digest = hashlib.sha256(pixel).hexdigest()
        caption = (
            "Fig. 1. Dose 10-5 M in CO2 and 75 cm2. "
            "Data are mean ± SEM (n = 6). *P < 0.05."
        )
        source = f"""<!doctype html><html><body><article>
<h1>Flat ScienceDirect article</h1>
<p><a href="https://doi.org/10.1016/j.example.2026.1">https://doi.org/10.1016/j.example.2026.1</a></p>
<section id="section-1"><h2>Abstract</h2></section>
<section id="section-2"><h2>1. Introduction</h2>
<p>First sentence.1Second paragraph used 10−5 M in 75 cm2 and CO2. Student's t-test used a p value in vitro.</p></section>
<section id="section-3"><h2>2. Results</h2><p>Retained body.Download: Download high-res image (1KB)Download: Download full-size image{caption}</p></section>
<section id="section-4"><h2>Appendix A. Supplementary data</h2><p>Supplementary data are available.Download: Download Word document (1KB)Multimedia component 1. Recommended articles</p></section>
<section id="section-5"><h2>References</h2><p>1A. OneFirst titleJournal, 1 (2000), p. 1Google Scholar2B. TwoSecond titleJournal, 2 (2001), p. 2Google ScholarCited by (0)</p></section>
<section id="archived-figures"><h2>Figures</h2><figure id="figure-1">
<img alt="Figure 1. {caption}" src="data:image/png;base64,{encoded}">
<figcaption>Figure 1. {caption} (publisher asset SHA-256 {digest})</figcaption>
</figure></section></article></body></html>"""

        article = lxml_html.fromstring(source).xpath("//article")[0]
        self.assertIsNotNone(_flat_sciencedirect_snapshot_root(article))
        result = self.extract(source)
        introduction = next(
            section for section in result.sections if section.heading == "1. Introduction"
        )
        self.assertEqual(len(introduction.blocks), 2)
        self.assertEqual(introduction.blocks[0].plain_text, "First sentence.^{1}")
        self.assertEqual(
            introduction.blocks[1].plain_text,
            "Second paragraph used 10^{−5} M in 75 cm^{2} and CO_{2}. "
            "Student's t-test used a p value in vitro.",
        )
        self.assertIn("Student's <em>t</em>-test", introduction.blocks[1].markdown)
        self.assertIn("a <em>p</em> value", introduction.blocks[1].markdown)
        self.assertIn("<em>in vitro</em>", introduction.blocks[1].markdown)
        results = next(section for section in result.sections if section.heading == "2. Results")
        self.assertEqual([block.plain_text for block in results.blocks], ["Retained body."])
        appendix = next(
            section
            for section in result.sections
            if section.heading == "Appendix A. Supplementary data"
        )
        self.assertEqual(
            [block.plain_text for block in appendix.blocks],
            ["Supplementary data are available."],
        )
        self.assertEqual(
            result.figures[0].caption_plain,
            "Fig. 1. Dose 10^{−5} M in CO_{2} and 75 cm^{2}. "
            "Data are mean ± SEM (n = 6). *P < 0.05.",
        )
        self.assertNotIn("publisher asset SHA-256", result.figures[0].caption_plain)
        self.assertIn("(<em>n</em> = 6)", result.figures[0].caption_markdown)
        self.assertIn("<em>P</em> &lt; 0.05", result.figures[0].caption_markdown)

        near_miss = source.replace(digest, "0" + digest[1:], 1)
        near_article = lxml_html.fromstring(near_miss).xpath("//article")[0]
        self.assertIsNone(_flat_sciencedirect_snapshot_root(near_article))
        near_result = self.extract(near_miss)
        near_introduction = next(
            section
            for section in near_result.sections
            if section.heading == "1. Introduction"
        )
        self.assertEqual(len(near_introduction.blocks), 1)
        self.assertIn(
            "publisher asset SHA-256", near_result.figures[0].caption_plain
        )

    def test_exact_mdpi_snapshot_restores_div_prose_and_numbered_figure(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<!doctype html><html><body>
<article id="mdpi-article-pharmaceuticals-16-01526"><div>
<div><span>Open Access</span><span>Article</span></div>
<h1>Exact MDPI article</h1>
<div><span><div>Honoka Obata</div><sup>1</sup>
<a href="mailto:honoka.obata@example.test"></a></span>
<span><div>Atsushi B. Tsuji</div><sup>2,*</sup>
<a href="mailto:tsuji.atsushi@example.test"></a>
<a href="https://orcid.org/0000-0003-2726-288X"></a></span></div>
<div><div><div><div><sup>1</sup></div><div>First institute.</div></div></div></div>
<div><em>Pharmaceuticals</em> <b>2023</b>, <em>16</em>(11), 1526;
<a href="https://doi.org/10.3390/ph16111526">https://doi.org/10.3390/ph16111526</a></div>
<div><span>Submission received: 11 September 2023</span> /
<span>Revised: 24 October 2023</span> / <span>Accepted: 25 October 2023</span> /
<span>Published: 27 October 2023</span></div>
<div><a href="/journal/pharmaceuticals/special_issues/example">Special collection</a></div>
<section id="sec1-pharmaceuticals-16-01526"><h2>1. Introduction</h2>
<div>Authored <sup>191</sup>Pt prose.</div>
<figure id="pharmaceuticals-16-01526-f001"><div><img alt="Figure 1. " src="{pixel}"></div>
<div><b>Figure 1.</b> Authored caption.</div></figure></section>
<div id="html-keywords">Keywords: platinum-191; Auger electron</div>
<section id="html-copyright">© 2023 by the authors.
<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a></section>
<section id="html-references_list"><h2>References</h2><ol><li id="B1-pharmaceuticals-16-01526">One.</li></ol></section>
</div></article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored ^{191}Pt prose.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(result.figures[0].caption_plain, "Figure 1. Authored caption.")
        self.assertEqual(result.embedded_assets[0].asset_id, "figure_001")
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "16",
                "issue": "11",
                "article_number": "1526",
                "date": "27 October 2023",
                "first_published": "27 October 2023",
            },
        )
        front = [block.plain_text for block in result.front_matter]
        self.assertIn("Access: Open access", front)
        self.assertIn("Article type: Article", front)
        self.assertIn("Affiliations: 1. First institute.", front)
        self.assertIn(
            "Correspondence: Atsushi B. Tsuji — tsuji.atsushi@example.test",
            front,
        )
        self.assertIn(
            "Author email: Honoka Obata — honoka.obata@example.test", front
        )
        self.assertIn("ORCID: Atsushi B. Tsuji — 0000-0003-2726-288X", front)
        self.assertIn("Special issue: Special collection", front)
        self.assertIn("Keywords: platinum-191; Auger electron", front)
        self.assertIn("Copyright: © 2023 by the authors. CC BY 4.0", front)

    def test_anonymous_mdpi_snapshot_restores_div_prose_and_numbered_figure(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<!doctype html><html><body><div>
<h1>Anonymous MDPI article</h1>
<div><em>Cancers</em> <b>2022</b>, <em>14</em>(4), 951;
<a href="https://doi.org/10.3390/cancers14040951">https://doi.org/10.3390/cancers14040951</a></div>
<section id="sec1-cancers-14-00951"><h2>1. Introduction</h2>
<div>Authored <sup>3</sup>H prose.</div>
<div id="cancers-14-00951-f001"><div><img alt="Cancers 14 00951 g001" src="{pixel}"></div>
<div><b>Figure 1.</b> Authored caption.</div></div></section>
</div></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored ^{3}H prose.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(result.figures[0].caption_plain, "Figure 1. Authored caption.")
        self.assertEqual(result.embedded_assets[0].asset_id, "figure_001")

    def test_identifier_light_mdpi_snapshot_restores_prose_visuals_and_equation(
        self,
    ) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<!doctype html><html><body><div>
<span itemprop="publisher" content="Multidisciplinary Digital Publishing Institute"></span>
<span itemprop="url" content="https://www.mdpi.com/1422-0067/16/6/12631"></span>
<h1 itemprop="name">Identifier-light MDPI article</h1>
<div><span><div>Ada Example</div><sup>*</sup>
<a href="mailto:ada@example.test"></a></span></div>
<div><div><div><div>Legacy institute.</div></div><div><div><sup>*</sup></div>
<div>Author to whom correspondence should be addressed.</div></div></div></div>
<div><em>Int. J. Mol. Sci.</em> <b>2015</b>, <em>16</em>(6), 12631-12647;
<a href="https://doi.org/10.3390/ijms160612631">https://doi.org/10.3390/ijms160612631</a></div>
<div><section id="1Introduction" type="intro"><h2>1. Introduction</h2>
<div>Authored Py–Im prose with 10<sup>−5</sup> M.</div>
<figure><div><img src="{pixel}"></div><div><b>Scheme 1.</b> Authored scheme.</div></figure>
</section><section id="2Results" type="results"><h2>2. Results</h2>
<div>Equation lead.
<div id="FD1"><div><div><span role="presentation"><math display="block"><mrow>
<mi>R</mi><mi>U</mi><mo>=</mo><mn>1</mn></mrow></math></span></div></div>
<div><label>(1)</label></div></div></div>
<figure><div><img src="{pixel}"></div><div><b>Figure 1.</b> Authored figure.</div></figure>
</section></div>
<section id="html-references_list"><h2>References</h2><ol><li>One.</li></ol></section>
</div></body></html>"""
        )

        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            "Authored Py–Im prose with 10^{−5} M.",
        )
        results = next(section for section in result.sections if section.heading == "2. Results")
        self.assertEqual(
            [block.kind for block in results.blocks], ["paragraph", "equation"]
        )
        self.assertEqual(results.blocks[0].plain_text, "Equation lead.")
        self.assertEqual(results.blocks[1].plain_text, "RU = 1 (1)")
        self.assertEqual(
            [(figure.figure_id, figure.label) for figure in result.figures],
            [("scheme_001", "Scheme 1"), ("figure_001", "Figure 1")],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["scheme_001", "figure_001"],
        )
        self.assertEqual(
            result.bibliographic,
            {"volume": "16", "issue": "6", "pages": "12631-12647"},
        )
        front = [block.plain_text for block in result.front_matter]
        self.assertIn("Affiliations: Legacy institute.", front)
        self.assertIn("Correspondence: Ada Example — ada@example.test", front)

    def test_semantic_identifier_light_mdpi_snapshot_restores_content(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<!doctype html><html><body><article><div>
<span itemprop="publisher" content="Multidisciplinary Digital Publishing Institute"></span>
<span itemprop="url" content="https://www.mdpi.com/1420-3049/18/12/15357"></span>
<h1 itemprop="name">Semantic identifier-light MDPI article</h1>
<div><em>Molecules</em> <b>2013</b>, <em>18</em>(12), 15357-15397;
<a href="https://doi.org/10.3390/molecules181215357">https://doi.org/10.3390/molecules181215357</a></div>
<div><section id="1Introduction" type="intro"><h2>1. Introduction</h2>
<div>Authored Py–Im prose with 10<sup>−5</sup> M.</div>
<figure><div><img src="{pixel}"></div><div><b>Figure 1.</b> Authored figure.</div></figure>
</section></div>
<section id="html-references_list"><h2>References</h2><ol><li>One.</li></ol></section>
</div></article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            "Authored Py–Im prose with 10^{−5} M.",
        )
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain, "Figure 1. Authored figure."
        )
        self.assertEqual(result.embedded_assets[0].asset_id, "figure_001")

    def test_semantic_anonymous_mdpi_snapshot_restores_div_prose(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body><article><div>
<h1>Semantic anonymous MDPI article</h1>
<div><span><div>Ada Example</div><sup>1,*</sup>
<a href="mailto:ada@example.org">ada@example.org</a></span></div>
<div><em>Int. J. Mol. Sci.</em> <b>2021</b>, <em>22</em>(9), 4741;
<a href="https://doi.org/10.3390/ijms22094741">https://doi.org/10.3390/ijms22094741</a></div>
<div>(This article belongs to the Section Molecular Biology)<br></div>
<section id="sec1-ijms-22-04741"><h2>1. Introduction</h2>
<div>Authored TGF-β1 prose with 10<sup>−5</sup> M.</div>
<figure id="ijms-22-04741-f001"><div><img alt="Figure 1." src="{pixel}"></div>
<figcaption><b>Figure 1.</b> Authored caption.</figcaption></figure></section>
</div></article></body>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            "Authored TGF-β1 prose with 10^{−5} M.",
        )
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(result.figures[0].caption_plain, "Figure 1. Authored caption.")
        self.assertEqual(result.embedded_assets[0].asset_id, "figure_001")
        self.assertIn(
            "Author assignments: Ada Example (1,*)",
            [block.plain_text for block in result.front_matter],
        )
        self.assertIn(
            "Section: Molecular Biology",
            [block.plain_text for block in result.front_matter],
        )

    def test_semantic_anonymous_mdpi_missing_raster_stub_restores_prose(self) -> None:
        result = self.extract(
            """<body><article><div>
<h1>Semantic MDPI article with missing raster</h1>
<div><em>Cancers</em> <b>2017</b>, <em>9</em>(3), 22;
<a href="https://doi.org/10.3390/cancers9030022">https://doi.org/10.3390/cancers9030022</a></div>
<section id="sec1-cancers-09-00022"><h2>1. Introduction</h2>
<div>Authored body prose.</div>
<figure id="cancers-09-00022-f001"><div>
<div href="#fig_body_display_cancers-09-00022-f001">
<a href="#fig_body_display_cancers-09-00022-f001"></a></div></div>
<div><b>Figure 1.</b> Authored caption whose archived raster is missing.</div>
</figure></section>
<section id="FigureandTable"><h2>Display objects</h2>
<div id="table_body_display_cancers-09-00022-t001"><table>
<caption><b>Table 1.</b> Exact authored values.<div></div></caption>
<thead><tr><th>Factor</th><th>Value</th></tr></thead>
<tbody><tr><td>FOXA1</td><td>+</td></tr></tbody></table>
<div><div><span>FOXA1: forkhead box A1.</span></div></div></div></section>
</div></article></body>"""
        )

        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored body prose.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Figure 1. Authored caption whose archived raster is missing.",
        )
        self.assertEqual(result.embedded_assets, [])
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].title_plain, "Table 1. Exact authored values.")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Factor", "Value"], ["FOXA1", "+"]],
        )
        self.assertEqual(result.tables[0].footnotes_plain, ["FOXA1: forkhead box A1."])

    def test_semantic_anonymous_mdpi_multipart_figure_restores_content(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body><article><div>
<h1>Semantic anonymous multipart MDPI article</h1>
<div><em>Biomolecules</em> <b>2020</b>, <em>10</em>(4), 544;
<a href="https://doi.org/10.3390/biom10040544">https://doi.org/10.3390/biom10040544</a></div>
<section id="sec1-biomolecules-10-00544"><h2>1. Introduction</h2>
<div>Authored multipart prose.</div>
<figure id="biomolecules-10-00544-f001"><div>
<img alt="Biomolecules 10 00544 g001a" src="{pixel}">
<img alt="Biomolecules 10 00544 g001b" src="{pixel}">
</div><div><b>Figure 1.</b> Complete multipart caption.</div></figure>
</section></div></article></body>"""
        )

        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored multipart prose.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Figure 1. Complete multipart caption.",
        )
        self.assertEqual(len(result.embedded_assets), 1)
        self.assertEqual(result.embedded_assets[0].asset_id, "figure_001")
        self.assertEqual(result.embedded_assets[0].media_type, "image/png")

    def test_exact_mdpi_snapshot_restores_inline_emphasis_only_in_inline_spans(
        self,
    ) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body><article><div>
<h1>MDPI inline emphasis article</h1>
<div><em>Molecules</em> <b>2020</b>, <em>25</em>(12), 2883;
<a href="https://doi.org/10.3390/molecules25122883">https://doi.org/10.3390/molecules25122883</a></div>
<section id="sec1-molecules-25-02883"><h2>1. Introduction</h2>
<div>Student <span>t</span>-test gave <span>p</span> &lt; 0.05 in
<span>Callithrix jacchus</span>.</div>
<figure id="molecules-25-02883-f001"><div><img alt="Figure 1." src="{pixel}"></div>
<div><b>Figure 1.</b> Mean ± SEM (<span>n</span> = 6).</div></figure></section>
<section id="sec2-molecules-25-02883"><h2>2. Results</h2>
<div id="table_body_display_molecules-25-02883-t001">
<div><b>Table 1.</b> Exact authored values.</div>
<table><tr><th>Group</th></tr><tr><td>Control</td></tr></table>
<div><div><span>Values are means ± SEM.</span></div><div></div></div></div></section>
<section id="html-references_list"><h2>References</h2><ol>
<li>Example title. <span>Example Journal</span> <b>2020</b>, <span>12</span>, 1–2.</li>
</ol></section></div></article></body>"""
        )

        self.assertEqual(
            result.sections[0].blocks[0].markdown,
            "Student <em>t</em>-test gave <em>p</em> &lt; 0.05 in "
            "<em>Callithrix jacchus</em>.",
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "<strong>Figure 1.</strong> Mean ± SEM (<em>n</em> = 6).",
        )
        self.assertEqual(
            result.references[0].markdown,
            "Example title. <em>Example Journal</em> <strong>2020</strong>, "
            "<em>12</em>, 1–2.",
        )
        self.assertEqual(
            result.tables[0].footnotes_markdown,
            ["Values are means ± SEM."],
        )

    def test_exact_mdpi_snapshot_extracts_semantic_table_and_note(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body><article><div>
<h1>Semantic MDPI table article</h1>
<div><em>Example Journal</em> <b>2024</b>, <em>7</em>(2), 123;
<a href="https://doi.org/10.3390/example7020123">https://doi.org/10.3390/example7020123</a></div>
<section id="sec1-example-7-00123"><h2>1. Introduction</h2>
<div>Authored introduction.</div>
<figure id="example-7-00123-f001"><div><img alt="Figure 1." src="{pixel}"></div>
<div><b>Figure 1.</b> Authored caption.</div></figure></section>
<section id="sec2-example-7-00123"><h2>2. Results</h2>
<div id="table_body_display_example-7-00123-t001">
<div><b>Table 1.</b> Exact authored values.</div>
<table><thead><tr><th>Group</th><th>Value (mg/L)</th></tr></thead>
<tbody><tr><td><b>Control</b></td><td>4.2 ± 0.3</td></tr></tbody></table>
<div><div><span>Values are means ± SEM.</span></div><div></div></div>
</div></section>
</div></article></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_001")
        self.assertEqual(
            table.source_id, "table_body_display_example-7-00123-t001"
        )
        self.assertEqual(table.label, "Table 1")
        self.assertEqual(table.title_plain, "Table 1. Exact authored values.")
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Group", "Value (mg/L)"], ["Control", "4.2 ± 0.3"]],
        )
        self.assertEqual(table.footnotes_plain, ["Values are means ± SEM."])

    def test_mdpi_table_wrapper_without_authenticated_article_is_ignored(self) -> None:
        result = self.extract(
            """<body><article><h1>Near miss</h1>
<section><h2>Results</h2>
<div id="table_body_display_example-7-00123-t001">
<div><b>Table 1.</b> Layout values.</div>
<table><tr><th>Heading</th></tr><tr><td>Value</td></tr></table>
<div>Layout note.</div></div></section></article></body>"""
        )

        self.assertEqual(result.tables, [])

    def test_semantic_anonymous_mdpi_flat_caption_restores_content(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body><article><div>
<h1>Semantic anonymous MDPI flat-caption article</h1>
<div><em>Molecules</em> <b>2020</b>, <em>25</em>(12), 2883;
<a href="https://doi.org/10.3390/molecules25122883">https://doi.org/10.3390/molecules25122883</a></div>
<section id="sec1-molecules-25-02883"><h2>1. Introduction</h2>
<div>Authored TGF-β1 prose.</div>
<figure id="molecules-25-02883-f001"><div><img alt="Molecules 25 02883 g001" src="{pixel}"></div>
<div><b>Figure 1.</b> Authored flat caption.</div></figure></section>
<section id="html-references_list"><h2>References</h2><ol>
<li id="B1-molecules-25-02883">One reference.</li></ol></section>
<section><table><tbody><tr><td></td><td><div><b>Sample Availability:</b>
Samples are available from the authors.</div></td></tr></tbody></table></section>
<section id="html-copyright">© 2020 by the authors.</section>
</div></article></body>"""
        )

        self.assertEqual(len(result.sections), 2)
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored TGF-β1 prose.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Figure 1. Authored flat caption.",
        )
        self.assertEqual(result.embedded_assets[0].asset_id, "figure_001")
        self.assertEqual(
            [
                (section.heading, [block.plain_text for block in section.blocks])
                for section in result.sections
            ],
            [
                ("1. Introduction", ["Authored TGF-β1 prose."]),
                (
                    "Sample Availability",
                    ["Samples are available from the authors."],
                ),
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.references],
            ["One reference."],
        )
        self.assertEqual(result.tables, [])

    def test_semantic_anonymous_mdpi_flat_caption_extra_child_is_not_normalized(
        self,
    ) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body><article><div>
<h1>Semantic anonymous MDPI flat-caption near miss</h1>
<div><em>Molecules</em> <b>2020</b>, <em>25</em>(12), 2883;
<a href="https://doi.org/10.3390/molecules25122883">https://doi.org/10.3390/molecules25122883</a></div>
<section id="sec1-molecules-25-02883"><h2>1. Introduction</h2>
<div>Anonymous layout prose must remain untouched.</div>
<figure id="molecules-25-02883-f001"><div><img alt="Molecules 25 02883 g001" src="{pixel}"></div>
<div><b>Figure 1.</b> Flat caption.</div><span>Extra child.</span></figure>
</section></div></article></body>"""
        )

        self.assertEqual(result.sections[0].blocks, [])

    def test_semantic_anonymous_mdpi_near_miss_without_doi_is_not_normalized(self) -> None:
        result = self.extract(
            """<body><article><div><h1>Semantic near miss</h1>
<section id="sec1-ijms-22-04741"><h2>1. Introduction</h2>
<div>Anonymous layout div.</div>
<figure id="ijms-22-04741-f001"><div><img alt="Figure 1."></div>
<figcaption><b>Figure 1.</b> Caption.</figcaption></figure></section>
</div></article></body>"""
        )

        self.assertEqual(result.sections[0].blocks, [])

    def test_anonymous_mdpi_near_miss_without_doi_is_not_normalized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div>
<h1>Anonymous near miss</h1>
<section id="sec1-cancers-14-00951"><h2>1. Introduction</h2>
<div>Anonymous layout div.</div>
<div id="cancers-14-00951-f001"><div><img alt="Cancers 14 00951 g001"></div>
<div><b>Figure 1.</b> Caption.</div></div></section>
</div></body></html>"""
        )

        self.assertEqual(result.sections[0].blocks, [])
        self.assertEqual(result.figures, [])

    def test_large_embedded_image_attribute_is_preserved_by_exact_parser_gate(self) -> None:
        tiny_gif = base64.b64decode(
            "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        )
        oversized_gif = tiny_gif + (b"\0" * 7_600_000)
        source = (
            "<!doctype html><html><body><article><h1>Large image</h1>"
            '<figure><h2>Graphical Abstract</h2><img src="data:image/gif;base64,'
            + base64.b64encode(oversized_gif).decode("ascii")
            + '"></figure></article></body></html>'
        )

        self.assertTrue(_requires_huge_html_parser(source))
        result = self.extract(source)
        self.assertEqual(len(result.embedded_assets), 1)
        self.assertEqual(result.embedded_assets[0].asset_id, "graphical_abstract")
        self.assertEqual(result.embedded_assets[0].data, oversized_gif)

        self.assertFalse(
            _requires_huge_html_parser(
                '<img src="data:application/octet-stream;base64,AAAA">'
            )
        )
        self.assertFalse(
            _requires_huge_html_parser(
                '<div src="data:image/gif;base64,AAAA"></div>'
            )
        )

    def test_exact_beilstein_snapshot_preserves_authored_semantics(self) -> None:
        svg = base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2">'
            b'<path d="M0 0h2v2H0z"/></svg>'
        ).decode("ascii")
        source = f"""<!doctype html><html><body><div>
<div><h1>Exact Beilstein article</h1><ol><li><sup>1</sup>
<a href="mailto:ada@example.org">mail</a></li></ol></div>
<div id="author1" role="tooltip"><div><div><div><span>Ada Example</span></div></div></div>
<div>Example Institute</div><div><a href="https://orcid.org/0000-0001-2345-6789">
https://orcid.org/0000-0001-2345-6789</a></div></div>
<div><div><div><sup>1</sup><span>Example Institute</span></div>
<div><span>Associate Editor: K. Editor</span><br>
<span><i>Beilstein J. Org. Chem.</i> <b>2024,</b> <i>20,</i> 10–20.</span>
<a href="https://doi.org/10.3762/bjoc.20.2">https://doi.org/10.3762/bjoc.20.2</a><br>
<strong>Received </strong><span>01 Jan 2024</span>,
<strong>Accepted </strong><span>02 Jan 2024</span>,
<strong>Published </strong><span>03 Jan 2024</span></div></div>
<div><div>Full Research Paper</div></div></div>
<div id="articleContent">
<div><h2>Abstract</h2><p>Authored abstract.</p></div>
<div><h2>Introduction</h2><p>Opening 5´-TTGTC-3´ prose.</p>
<figure id="scheme-1"><figcaption>
<p><strong>Scheme 1:</strong> Complete authored caption.</p>
<div id="caption-modal">Scheme 1: Truncated modal duplicate…</div></figcaption>
<img src="data:image/svg+xml;base64,{svg}"></figure>
<a id="T1"></a><figure><figcaption><p><strong>Table 1:</strong> Binding data.</p></figcaption>
<div><table><tr><th>Sequence</th><th>Value</th></tr>
<tr><td>AA<u>TT</u><b>G</b> D1‘</td><td>1.0</td></tr></table></div></figure>
<p><sup>a</sup><i>c</i> = 1.0 μM. <sup>b</sup>Authored condition.</p></div>
<div id="supporting-info"><h2>Supporting Information</h2><table>
<tr><td><strong>Supporting Information File 1:</strong> Experimental details.</td></tr>
<tr><td><a href="/bjoc/content/supplementary/example-S1.pdf">Download</a></td></tr>
</table></div>
<div id="references"><h2>References</h2><ol>
<li id="R1">One, A. <i>Journal</i> <b>2020,</b> 1–2.<br>Return to citation in text:
[<a href="#link1">1</a>]</li>
<li id="R2">Two, B. <i>Journal</i> <b>2021,</b> 3–4.<br>
https://example.org/authored<br>Return to citation in text:
[<a href="#link2">2</a>]</li></ol></div>
</div></div></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.bibliographic["volume"], "20")
        self.assertEqual(result.bibliographic["pages"], "10–20")
        self.assertEqual(result.bibliographic["date"], "03 Jan 2024")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(
            result.sections[1].blocks[0].plain_text,
            "Opening 5′-TTGTC-3′ prose.",
        )
        self.assertEqual(result.figures[0].figure_id, "scheme_001")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Scheme 1: Complete authored caption.",
        )
        self.assertNotIn("modal", result.figures[0].caption_plain)
        self.assertEqual(len(result.embedded_assets), 1)
        self.assertEqual(result.embedded_assets[0].media_type, "image/svg+xml")
        self.assertEqual(result.embedded_assets[0].output_path, "figures/main/scheme_001.svg")
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_id, "T1")
        self.assertEqual(
            result.tables[0].parts[0].rows[1][0].markdown,
            "AA<u>TT</u><strong>G</strong> D1′",
        )
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["[a] c = 1.0 μM.", "[b] Authored condition."],
        )
        self.assertEqual(
            result.tables[0].footnotes_markdown,
            ["[a] <em>c</em> = 1.0 μM.", "[b] Authored condition."],
        )
        self.assertEqual(
            block_markup_to_safe_html(
                result.tables[0].parts[0].rows[1][0].markdown,
                kind="table_cell",
            ),
            "AA<u>TT</u><strong>G</strong> D1′",
        )
        self.assertEqual(len(result.references), 2)
        self.assertNotIn("Return to citation", result.references[0].plain_text)
        self.assertIn("https://example.org/authored", result.references[1].plain_text)
        self.assertNotIn("Return to citation", result.references[1].plain_text)
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Supporting Information File 1: Experimental details."],
        )
        self.assertTrue(
            any(
                block.plain_text
                == "Corresponding author: Ada Example — ada@example.org"
                for block in result.front_matter
            )
        )
        self.assertTrue(
            any("0000-0001-2345-6789" in block.plain_text for block in result.front_matter)
        )
        self.assertEqual(result.warnings, [])

        unsafe_svg = base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            b'<image href="https://example.org/external.png"/></svg>'
        ).decode("ascii")
        unsafe_result = self.extract(source.replace(svg, unsafe_svg))
        self.assertEqual(unsafe_result.embedded_assets, [])
        self.assertEqual(unsafe_result.warnings[0]["code"], "invalid_embedded_figure_image")

    def test_embedded_svg_accepts_only_inert_inkscape_metadata(self) -> None:
        svg = b"""<svg xmlns="http://www.w3.org/2000/svg"
          xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
          xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
          viewBox="0 0 2 2" inkscape:version="1.2">
          <sodipodi:namedview inkscape:deskcolor="#fff">
            <inkscape:page x="0" y="0" width="2" height="2"/>
          </sodipodi:namedview>
          <g inkscape:groupmode="layer" inkscape:label="Layer 1">
            <path d="M0 0h2v2H0z"/>
          </g></svg>"""

        def article(payload: bytes) -> str:
            encoded = base64.b64encode(payload).decode("ascii")
            return (
                "<!doctype html><html><body><article><h1>SVG metadata</h1>"
                '<figure><h2>Graphical Abstract</h2><img '
                f'src="data:image/svg+xml;base64,{encoded}"></figure>'
                "</article></body></html>"
            )

        result = self.extract(article(svg))
        self.assertEqual(len(result.embedded_assets), 1)
        self.assertEqual(result.embedded_assets[0].media_type, "image/svg+xml")

        unsafe = svg.replace(b"inkscape:page", b"inkscape:script")
        self.assertEqual(self.extract(article(unsafe)).embedded_assets, [])

    def test_mdpi_like_markup_without_matching_doi_is_not_normalized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<article id="mdpi-article-pharmaceuticals-16-01526"><div>
<h1>Near miss</h1>
<section id="sec1-pharmaceuticals-16-01526"><h2>1. Introduction</h2>
<div>Anonymous layout div.</div>
<figure id="pharmaceuticals-16-01526-f001"><div><img alt="Figure 1. "></div>
<div><b>Figure 1.</b> Caption.</div></figure></section>
</div></article></body></html>"""
        )

        self.assertEqual(result.sections[0].blocks, [])
        self.assertEqual(result.figures[0].kind, "graphical_abstract")
        self.assertEqual(result.bibliographic, {})
        self.assertEqual(result.front_matter, [])

    def test_old_acs_title_author_note_marker_is_not_title_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a href="#bi9912847AF2">†</a></h1>
<section><h2>Abstract</h2><p>Text.</p></section>
<div id="bi9912847AF2">Author note.</div>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title")

    def test_acs_funding_title_note_marker_is_not_title_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1 id="aria123">Scientific title<a href="#fs1">†</a></h1>
<div><div>Funding</div><div><strong>Funding Statement(s): </strong>
<div><sup>†</sup> Supported by the authors' grant.</div></div></div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title")

    def test_acs_fs_title_marker_without_funding_evidence_is_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a href="#fs1">†</a></h1>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title†")

    def test_silverchair_title_access_badge_is_not_authored_title_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1 id="aria123">Scientific <em>MYCN</em> title
<span><i title="Free"><span>Free</span></i></span></h1>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific MYCN title")

    def test_nonmatching_title_status_markup_is_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title <span><i title="Dataset"><span>Free</span></i></span></h1>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title Free")

    def test_wiley_title_author_note_marker_is_not_title_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a id="link_chem123-note-1001"
 aria-label="Scrollable Link" href="#chem123-note-1001">**</a></h1>
<div id="chem123-note-1001"><sup>†</sup><div><p>Defined abbreviation.</p></div></div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title")

    def test_legacy_wiley_nss_title_note_marker_is_not_title_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a id="link_nss" aria-label="Scrollable Link"
 href="#nss"><sup>†</sup></a></h1>
<div id="nss" tabindex="0"><sup>†</sup><div>
<p>CBI: Defined abbreviation.</p></div></div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title")

    def test_legacy_wiley_nss_funding_note_is_recovered_as_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a id="link_nss" aria-label="Scrollable Link"
 href="#nss"><sup>†</sup></a></h1>
<div id="nss" tabindex="0"><sup>†</sup><div>
<p>Support was provided by the National Institutes of Health. A.B. is grateful for a fellowship.</p>
</div></div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title")
        self.assertIn(
            (
                "Funding (†): Support was provided by the National Institutes "
                "of Health. A.B. is grateful for a fellowship."
            ),
            [block.plain_text for block in result.front_matter],
        )

    def test_wiley_like_authored_title_marker_without_note_target_is_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a id="link_topic-note-1001"
 aria-label="Scrollable Link" href="#topic-note-1001">**</a></h1>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title**")

    def test_non_acs_title_link_and_authored_marker_are_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific <a href="#topic">title</a> †</h1>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title †")

    def test_old_acs_redundant_citation_parentheses_are_collapsed(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS citations</h1><section><h2>Results</h2>
<p>Evidence (<em> (<a ref-data-modal-source-id="b1">1</a>, <a ref-data-modal-source-id="b2">2</a>−<a ref-data-modal-source-id="b4">4</a>)</em>).</p>
<p>Authored <em>(nested words)</em> and (<em>(3)</em>) stay unchanged.</p>
</section></article></body></html>"""
        )

        first, second = result.sections[0].blocks
        self.assertEqual(first.plain_text, "Evidence (1, 2−4).")
        self.assertEqual(first.markdown, "Evidence (<em>1, 2−4</em>).")
        self.assertEqual(
            second.plain_text,
            "Authored (nested words) and ((3)) stay unchanged.",
        )

    def test_old_acs_split_citation_fragment_uses_outer_parenthesis(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS split citations</h1><section><h2>Results</h2>
<p>Evidence (<em>5</em>, <em> (<a ref-data-modal-source-id="b9 b10 b11 b12">9−12</a>)</em>.</p>
<p>Authored (<em>words</em>, <em> (<a ref-data-modal-source-id="b2">2</a>)</em>.</p>
</section></article></body></html>"""
        )

        first, second = result.sections[0].blocks
        self.assertEqual(first.plain_text, "Evidence (5, 9−12).")
        self.assertEqual(first.markdown, "Evidence (<em>5</em>, <em>9−12)</em>.")
        self.assertEqual(second.plain_text, "Authored (words, (2).")

    def test_utf8_inline_notation_citations_and_terminal_linkouts(self) -> None:
        result = self.extract(
            """<!doctype html>
<html><body><article>
  <div><a>Volume 12, Issue 3</a><span> pp. 101-109</span></div>
  <div><span>First published: </span><span>07 June 2020</span></div>
  <h1>β-Lactam formation in H₂O</h1>
  <section>
    <h2>Results</h2>
    <p>Rate <i>k</i><sub>obs</sub> was 10<sup>−3</sup> s<sup>−1</sup>;
       see <a href="#bib1">1</a>.</p>
    <p>A complete sentence.<a href="#fig1">1</a></p>
    <p>Previously described.<span><a href="#bib1">1</a></span><a href="#fig1">1</a>, <a href="#fig2">2</a></p>
  </section>
  <figure id="fig1"><figcaption><p><strong>Figure 1.</strong> Synthetic caption.</p></figcaption></figure>
  <section id="article-references">
    <h2>References</h2>
    <ol><li id="bib1"><span>1.</span> Example, α study.
      <div><span>10.1234/example</span></div></li></ol>
  </section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "β-Lactam formation in H₂O")
        self.assertEqual(len(result.sections), 1)
        paragraphs = result.sections[0].blocks
        self.assertIn("<em>k</em><sub>obs</sub>", paragraphs[0].markdown)
        self.assertIn("10<sup>−3</sup> s<sup>−1</sup>", paragraphs[0].markdown)
        self.assertIn("see [1].", paragraphs[0].markdown)
        self.assertEqual(paragraphs[1].markdown, "A complete sentence.")
        self.assertEqual(paragraphs[2].markdown, "Previously described.[1]")
        self.assertEqual(len(result.references), 1)
        self.assertIn("α study", result.references[0].plain_text)
        self.assertIn(
            "https://doi.org/10.1234/example",
            result.references[0].markdown,
        )
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "12",
                "issue": "3",
                "pages": "101-109",
                "first_published": "07 June 2020",
            },
        )

    def test_wiley_reference_does_not_duplicate_visible_doi_linkout(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley DOI deduplication</h1>
<section id="article-references"><h2>References</h2><ol>
<li><span>1</span> Example A. Visible DOI. doi:<a
 href="https://doi.org/10.1234/EXAMPLE">10.1234/example</a>
 <div><span>10.1234/EXAMPLE</span><a href="#">Linkout controls</a></div>
</li></ol></section></article></body></html>"""
        )

        reference = result.references[0]
        self.assertEqual(reference.plain_text.count("10.1234/example"), 1)
        self.assertNotIn("DOI: https://doi.org/", reference.plain_text)
        self.assertNotIn("Linkout controls", reference.plain_text)

    def test_wiley_reference_ignores_damaged_hidden_doi_when_visible_doi_exists(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley damaged hidden DOI</h1>
<section id="article-references"><h2>References</h2><ol>
<li><span>1</span> Example A. Visible DOI. doi:<a
 href="https://doi.org/10.1016/s0959-440x(03)00081-2">10.1016/s0959-440x(03)00081-2</a>
 <div><span>10.1016/s0959?440x(03)00081?2</span><a href="#">Linkout controls</a></div>
</li></ol></section></article></body></html>"""
        )

        reference = result.references[0]
        self.assertIn("doi:10.1016/s0959-440x(03)00081-2", reference.plain_text)
        self.assertNotIn("s0959%3F440x", reference.plain_text)
        self.assertNotIn("DOI: https://doi.org/", reference.plain_text)

    def test_wiley_reference_keeps_hidden_only_doi(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley hidden-only DOI</h1>
<section id="article-references"><h2>References</h2><ol>
<li><span>1</span> Example A. Citation without a visible identifier.
 <div><span>10.1234/hidden-only</span><a href="#">Linkout controls</a></div>
</li></ol></section></article></body></html>"""
        )

        reference = result.references[0]
        self.assertTrue(
            reference.plain_text.endswith(
                "DOI: https://doi.org/10.1234/hidden-only"
            )
        )
        self.assertNotIn("Linkout controls", reference.plain_text)

    def test_structured_wiley_supporting_table_keeps_only_authored_description(self) -> None:
        source = """<!doctype html><html><body><article><h1>Legacy Wiley</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="d13876412e373"><div><table><thead><tr>
<th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id="cas14912-sup-0001"><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1111%2Fcas.14912&amp;file=cas14912-sup-0001-AppendixS1.pdf">cas14912-sup-0001-AppendixS1.pdf</a>PDF document, 5.8 MB</td>
<td headers="article-description">Appendix S1</td></tr></tbody></table>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Appendix S1"],
        )
        self.assertEqual(
            result.supporting_information[0].source_locator,
            "/html/body/article/section/div/div/div/table/tbody/tr/td[2]",
        )

        near_miss = self.extract(source.replace("Description</th>", "Details</th>", 1))
        self.assertEqual(
            [block.plain_text for block in near_miss.supporting_information],
            [
                "Please note: The publisher is not responsible for the content or "
                "functionality of any supporting information supplied by the authors. "
                "Any queries (other than missing content) should be directed to the "
                "corresponding author for the article."
            ],
        )

        spoof = self.extract(
            """<!doctype html><html><body><article><h1>Reserved attribute</h1>
<section><h2>Results</h2><p data-pip-litdb-normalized-kind="wiley-supporting-description"
data-pip-litdb-source-locator="/spoofed/source">Authored prose.</p></section>
</article></body></html>"""
        )
        self.assertNotEqual(spoof.sections[0].blocks[0].source_locator, "/spoofed/source")

    def test_structured_wiley_supporting_table_removes_service_notice_variant(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Legacy Wiley variant</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="d257043269e284"><div>
<p>As a service to our authors and readers, this journal provides supporting information supplied by the authors. Such materials are peer reviewed and may be re-organized for online delivery, but are not copy-edited or typeset. Technical support issues arising from supporting information (other than missing files) should be addressed to the authors.</p>
<table><thead><tr><th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id=""><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1002%2Fchem.202004421&amp;file=chem202004421-sup-0001-misc_information.pdf">chem202004421-sup-0001-misc_information.pdf</a>2.1 MB</td>
<td headers="article-description">Supplementary</td></tr></tbody></table>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Supplementary"],
        )
        self.assertEqual(
            result.supporting_information[0].source_locator,
            "/html/body/article/section/div/div/div/table/tbody/tr/td[2]",
        )

    def test_structured_wiley_legacy_supinf_online_notice_keeps_description(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Legacy Wiley supinf</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="jmr2273-supinf-0001"><div>
<p>Supporting information may be found in the online version of this paper</p>
<table><thead><tr><th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id="jmr2273-supitem-0001"><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1002%2Fjmr.2273&amp;file=jmr_2273_supplementary+material.doc">jmr_2273_supplementary material.doc</a>Word document, 525 KB</td>
<td headers="article-description">Supporting Information</td></tr></tbody></table>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Supporting Information"],
        )
        self.assertEqual(
            result.supporting_information[0].source_locator,
            "/html/body/article/section/div/div/div/table/tbody/tr/td[2]",
        )

    def test_structured_wiley_additional_online_notice_is_retained(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Biopolymers SI</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="d236197686e379"><div>
<p>Additional Supporting Information may be found in the online version of this article.</p>
<table><thead><tr><th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id=""><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1002%2Fbip.22205&amp;file=supplementary.doc">supplementary.doc</a>Word document, 34 KB</td>
<td headers="article-description">Supplementary Information</td></tr></tbody></table>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            [
                "Additional Supporting Information may be found in the online "
                "version of this article.",
                "Supplementary Information",
            ],
        )
        self.assertNotIn(
            "publisher is not responsible",
            " ".join(
                block.plain_text for block in result.supporting_information
            ).casefold(),
        )

    def test_structured_sciencedirect_supporting_table_keeps_descriptions(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Legacy ScienceDirect</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="feb4s2211546314000278-s0120"><div><table><thead><tr><th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id="article-sup-m0005"><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1016%2Fj.fob.2014.03.004&amp;file=article-sup-m0005.doc">article-sup-m0005.doc</a>application/doc, 29.5 KB</td>
<td headers="article-description"><p>Supplementary Table 1. List of primers.</p></td></tr>
<tr id="article-sup-m0010"><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1016%2Fj.fob.2014.03.004&amp;file=article-sup-m0010.doc">article-sup-m0010.doc</a>application/doc, 31 KB</td>
<td headers="article-description"><p>Supplementary Table 2. List of oligonucleotides.</p></td></tr>
</tbody></table><p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            [
                "Supplementary Table 1. List of primers.",
                "Supplementary Table 2. List of oligonucleotides.",
            ],
        )

    def test_structured_wiley_supporting_table_keeps_paragraph_descriptions(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Legacy Wiley paragraphs</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="d705972e510"><div><table><thead><tr>
<th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id=""><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1111%2Fcas.12610&amp;file=cas12610-sup-0001.doc">cas12610-sup-0001.doc</a>Word document, 3.6 MB</td>
<td headers="article-description"><p><b>Doc. S1.</b> Supplementary methods.</p>
<p><b>Fig. S1.</b> <i>In vivo</i> binding.</p></td></tr></tbody></table>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Doc. S1. Supplementary methods.", "Fig. S1. In vivo binding."],
        )
        self.assertEqual(
            [block.source_locator for block in result.supporting_information],
            [
                "/html/body/article/section/div/div/div/table/tbody/tr/td[2]/p[1]",
                "/html/body/article/section/div/div/div/table/tbody/tr/td[2]/p[2]",
            ],
        )
        self.assertIn("<em>In vivo</em>", result.supporting_information[1].markdown)

    def test_structured_wiley_supporting_table_keeps_inline_description(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Legacy Wiley inline description</h1>
<section><div><h2><a><div id="support-information-section"><span>Supporting Information</span></div></a></h2>
<div id="d23279747e463"><div><table><thead><tr>
<th id="article-filename" scope="col">Filename</th>
<th id="article-description" scope="col">Description</th></tr></thead><tbody>
<tr id=""><td headers="article-filename"><a href="/action/downloadSupplement?doi=10.1111%2Fcas.12493&amp;file=cas12493-sup-0001-FigureS1.tif">cas12493-sup-0001-FigureS1.tif</a>image/tif, 1.2 MB</td>
<td headers="article-description"><b>Fig. S1.</b> Distribution at 5 μM with a 50 μm scale bar.</td></tr></tbody></table>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p>
</div></div></div></section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Fig. S1. Distribution at 5 μM with a 50 μm scale bar."],
        )
        self.assertEqual(
            result.supporting_information[0].source_locator,
            "/html/body/article/section/div/div/div/table/tbody/tr/td[2]",
        )
        self.assertIn("<strong>Fig. S1.</strong>", result.supporting_information[0].markdown)

    def test_empty_wiley_citing_literature_service_section_is_not_content(self) -> None:
        source = """<!doctype html><html><body><article><h1>Wiley article</h1>
<section><h2>Discussion</h2><p>Authored discussion.</p></section>
<div><section id="cited-by"><div><div><h2><div tabindex="0" role="button"
aria-controls="cited-by__content" aria-expanded="false"><span
id="citedby-section">Citing Literature</span></div></h2><div
id="cited-by__content"><div></div></div></div></div></section></div>
<section><h2>Appendix</h2><p>Authored mention of Citing Literature.</p></section>
</article></body></html>"""

        result = self.extract(source)

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Discussion", "Appendix"],
        )
        self.assertEqual(
            result.sections[1].blocks[0].plain_text,
            "Authored mention of Citing Literature.",
        )

        direct_wrapper = self.extract(
            """<!doctype html><html><body><article><h1>Wiley article</h1>
<section><h2>Discussion</h2><p>Authored discussion.</p></section>
<div><section id="cited-by"><div><h2><div tabindex="0" role="button"
aria-controls="cited-by__content" aria-expanded="false"><span
id="citedby-section">Citing Literature</span></div></h2><div
id="cited-by__content"><div></div></div></div></section></div>
<section><h2>Appendix</h2><p>Authored appendix.</p></section>
</article></body></html>"""
        )
        self.assertEqual(
            [section.heading for section in direct_wrapper.sections],
            ["Discussion", "Appendix"],
        )

    def test_wiley_small_cap_molar_unit_spans_render_uppercase(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley molarity units</h1><section><h2>Methods</h2>
<p>Samples used 50&#8197;n<span>m</span>, 5&#8197;m<span>m</span>,
40&#8197;μ<span>m</span>, 0.05&#8197;µ<span>m</span>, 1.0-m<span>m</span>,
and 0.1&#8201;<span>m</span> NaOH.
A path was 5 m long, wavelength was 450 nm, and variable <span>m</span> remained.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            "Samples used 50 nM, 5 mM, 40 μM, 0.05 µM, 1.0-mM, and 0.1 M NaOH. "
            "A path was 5 m long, wavelength was 450 nm, and variable m remained.",
        )
        self.assertEqual(block.markdown, block.plain_text)

    def test_flat_wiley_snapshot_removes_only_exact_ui_and_recovers_references(self) -> None:
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(pixel).decode("ascii")
        digest = hashlib.sha256(pixel).hexdigest()
        caption = "Authored caption."
        toc = (
            "1 Introduction2 Materials and Methods3 Results4 Discussion5 Conclusion "
            "Author Contributions Acknowledgments Conflicts of InterestData Availability "
            "StatementSupporting InformationReferencesCiting LiteraturePDF"
        )
        modal = (
            "Give accessShare full text accessClose modalShare full-text accessPlease review "
            "our Terms and Conditions of Use and check box below to share full-text version "
            "of article.I have read and accept the Wiley Online Library Terms and Conditions "
            "of UseShareable LinkUse the link below to share a full-text version of this "
            "article with your friends and colleagues. Learn more.Copy URL Share a linkShare "
            "onEmailFacebookxLinkedInRedditWechatBluesky QR Code"
        )
        flattened_tables = """
<p>TABLE 1. IC50 values.</p><p>Cell lines</p><p>IC50 value (nM)</p>
<p>CCC-002</p><p>Olaparib</p><p>AZD6738</p><p>KU-60019</p><p>SN-38</p>
<p>CHP-134</p><p>0.3</p><p>1.2 × 103</p><p>8.8 × 102</p><p>5.5 × 103</p><p>1.2</p>
<p>TABLE 2. CI index.</p><p>Cell lines</p><p>CCC-002 + olaparib</p>
<p>CCC-002 + AZD6738</p><p>CCC-002 + KU-60019</p><p>CCC-002 + SN-38</p>
<p>CI at ED50</p><p>CI at ED75</p><p>CI at ED50</p><p>CI at ED75</p>
<p>CI at ED50</p><p>CI at ED75</p><p>CI at ED50</p><p>CI at ED75</p>
<p>CHP-134</p><p>0.27</p><p>0.29</p><p>0.75</p><p>0.67</p><p>0.43</p><p>0.34</p><p>3.23</p><p>14.52</p>"""
        source = f"""<!doctype html><html><body><article>
<h1>Flat Wiley article</h1><p><a href="https://doi.org/10.1002/test.12345">https://doi.org/10.1002/test.12345</a></p>
<section id="section-1"><h2>ABSTRACT</h2><p>{toc}</p><p>CITE</p>
<p>ToolsRequest permissionAdd to favoritesTrack citation</p><p>ShareShare</p>
<p>{modal}</p><p>ABSTRACT</p><p>Authored abstract.</p></section>
<section id="section-2"><h2>Abbreviations</h2><p>ATM</p><p>ataxia telangiectasia mutated</p>
<p>IC50</p><p>half-maximal inhibitory concentration</p></section>
<section id="section-3"><h2>1 Introduction</h2><p>Authored MYCN body used 1 × 106 cells in 75 mm3.</p>
<p>FIGURE 1Open in figure viewerPowerPoint</p><p>{caption}</p>{flattened_tables}</section>
<section id="section-4"><h2>Supporting Information</h2>
<p>Filename Description</p><p>example.pdfPDF document, 1.0 KB</p>
<p>Figure S1. One. Figure S2. Two. Figure S3. Three.</p>
<p>example.pptxPowerPoint 2007 presentation, 2.0 KB</p><p>Table S1. Caption.</p>
<p>Please note: The publisher is not responsible for the content or functionality of any supporting information supplied by the authors. Any queries (other than missing content) should be directed to the corresponding author for the article.</p></section>
<section id="section-5"><h2>References</h2>
<p>1Alpha A. One, https://doi.org/10.1234/one. 10.1234/oneCAS PubMed Google Scholar</p>
<p>2Beta B. Two. 10.1234/twoPubMed Web of Science® Google Scholar</p>
<p>Citing Literature</p></section>
<section id="archived-figures"><h2>Figures</h2>
<figure id="figure-1"><img alt="Figure 1. FIGURE 1Open in figure viewerPowerPoint {caption}" src="data:image/png;base64,{encoded}">
<figcaption>Figure 1. FIGURE 1Open in figure viewerPowerPoint {caption} (publisher asset SHA-256 {digest})</figcaption></figure>
</section></article></body></html>"""

        self.assertIsNotNone(_flat_wiley_snapshot_root(lxml_html.fromstring(source).xpath("//article")[0]))

        result = self.extract(source)

        abstract = result.sections[0]
        self.assertEqual([block.plain_text for block in abstract.blocks], ["Authored abstract."])
        abbreviations = result.sections[1]
        self.assertEqual(len(abbreviations.blocks), 1)
        self.assertEqual(abbreviations.blocks[0].kind, "list")
        self.assertEqual(
            abbreviations.blocks[0].plain_text,
            "- ATM: ataxia telangiectasia mutated\n"
            "- IC_{50}: half-maximal inhibitory concentration",
        )
        introduction = result.sections[2]
        self.assertEqual(
            [block.plain_text for block in introduction.blocks],
            ["Authored MYCN body used 1 × 10^{6} cells in 75 mm^{3}."],
        )
        self.assertIn("<em>MYCN</em>", introduction.blocks[0].markdown)
        self.assertIn("10<sup>6</sup>", introduction.blocks[0].markdown)
        self.assertEqual(result.figures[0].caption_plain, caption)
        self.assertEqual([table.table_id for table in result.tables], ["table_001", "table_002"])
        self.assertEqual(result.tables[0].parts[0].rows[0][0].rowspan, 2)
        self.assertEqual(result.tables[0].parts[0].rows[0][1].colspan, 5)
        self.assertEqual(
            result.tables[0].parts[0].rows[2][2].text,
            "1.2 × 10^{3}",
        )
        self.assertEqual(result.tables[1].parts[0].rows[0][1].colspan, 2)
        self.assertEqual(result.tables[0].title_plain, "TABLE 1. IC_{50} values.")
        self.assertIn("IC<sub>50</sub>", result.tables[0].title_markdown)
        self.assertEqual(
            result.tables[1].parts[0].rows[1][0].text,
            "CI at ED_{50}",
        )
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            [
                "Figure S1. One. Figure S2. Two. Figure S3. Three.",
                "Table S1. Caption.",
            ],
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Alpha A. One, https://doi.org/10.1234/one.",
                "2. Beta B. Two. 10.1234/two",
            ],
        )

        near_miss = self.extract(source.replace("2Beta B.", "3Beta B.", 1))
        self.assertEqual(near_miss.references, [])
        self.assertIn("CITE", [block.plain_text for block in near_miss.sections[0].blocks])
        self.assertIn("Open in figure viewer", near_miss.figures[0].caption_plain)
        self.assertEqual(
            near_miss.sections[2].blocks[0].plain_text,
            "Authored MYCN body used 1 × 106 cells in 75 mm3.",
        )

        table_near_miss = self.extract(
            source.replace("CI at ED75", "CI at ED 75", 1)
        )
        self.assertEqual(table_near_miss.tables, [])
        self.assertIn(
            "TABLE 1. IC_{50} values.",
            [
                block.plain_text
                for section in table_near_miss.sections
                for block in section.blocks
            ],
        )

        current_toc = (
            "1 Introduction2 Materials and Methods3 Results4 Discussion Author Contributions "
            "Acknowledgments Disclosure Ethics Statement Conflicts of InterestSupporting "
            "InformationReferencesCiting LiteraturePDF"
        )
        doi_1111 = source.replace(
            "https://doi.org/10.1002/test.12345",
            "https://doi.org/10.1111/test.12345",
        ).replace(toc, current_toc)
        current = self.extract(doi_1111)
        self.assertEqual(
            [block.plain_text for block in current.sections[0].blocks],
            ["Authored abstract."],
        )
        self.assertEqual(len(current.references), 2)
        self.assertEqual(current.figures[0].caption_plain, caption)

    def test_flat_acs_rolling_paragraph_references_are_exact_gated(self) -> None:
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(pixel).decode("ascii")
        digest = hashlib.sha256(pixel).hexdigest()
        source = f"""<!doctype html><html><body><article>
<h1>Flat ACS article</h1><p><a href="https://doi.org/10.1021/example.1">https://doi.org/10.1021/example.1</a></p>
<section id="section-1"><h2>Abstract</h2><p>Abstract.</p></section>
<section id="section-2"><h2>Visual Abstract</h2><p>Subjects</p></section>
<section id="section-3"><h2>Introduction</h2><p>Body.</p></section>
<section id="section-4"><h2>Supporting Information</h2><p>Files.</p></section>
<section id="section-5"><h2>References</h2>
<p>1.Alpha, A. First study,</p>
<p>https://doi.org/10.1000/oneDownload PDF.CrossrefSearch ADS Google Scholar2.Beta, B. Second study,</p>
<p>https://doi.org/10.1000/twoDownload PDF.Google Scholar</p>
<p>© 2024 The Authors. Published by American Chemical Society This publication is licensed under a Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International License.</p>
</section>
<section id="archived-figures"><h2>Figures</h2><figure>
<img alt="Figure 1." src="data:image/png;base64,{encoded}">
<figcaption>Figure 1. Caption. (publisher asset SHA-256 {digest})</figcaption>
</figure></section></article></body></html>"""

        article = lxml_html.fromstring(source).xpath("//article")[0]
        self.assertIsNotNone(_flat_acs_snapshot_root(article))
        result = self.extract(source)
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Alpha, A. First study DOI: https://doi.org/10.1000/one",
                "2. Beta, B. Second study DOI: https://doi.org/10.1000/two",
            ],
        )
        self.assertEqual(result.figures[0].caption_plain, "Figure 1. Caption.")
        self.assertEqual(result.figures[0].caption_markdown, "Figure 1. Caption.")
        self.assertNotIn("publisher asset SHA-256", result.figures[0].caption_plain)

        rich_source = source.replace(
            "Figure 1. Caption. (publisher asset SHA-256",
            "Figure 1. Caption <em>K</em><sub>d</sub>. (publisher asset SHA-256",
            1,
        )
        rich_result = self.extract(rich_source)
        self.assertEqual(
            rich_result.figures[0].caption_plain, "Figure 1. Caption K_{d}."
        )
        self.assertEqual(
            rich_result.figures[0].caption_markdown,
            "Figure 1. Caption <em>K</em><sub>d</sub>.",
        )

        near_miss = source.replace("Scholar2.Beta", "Scholar3.Beta", 1)
        near_miss_article = lxml_html.fromstring(near_miss).xpath("//article")[0]
        self.assertIsNone(_flat_acs_snapshot_root(near_miss_article))
        near_miss_result = self.extract(near_miss)
        self.assertEqual(near_miss_result.references, [])
        self.assertIn(
            "publisher asset SHA-256", near_miss_result.figures[0].caption_plain
        )

    def test_aacr_flat_div_references_exclude_page_navigation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section>
<h1>AACR flat references</h1>
<nav><ul><li>Previous Article</li><li>Share</li><li>Export citation</li></ul></nav>
<h2 id="8733021">References</h2>
<div><div id="8733021-content"><div>
  <div xmlns:helper="urn:XsltStringHelper"><div><div><span>1</span><div>
    Alpha A. First study. <div><em>Journal One</em></div>
    <div>2007</div>;<div>6</div>:<div>346</div>–54.
    <div><a>Crossref</a></div><div><a>Download PDF</a></div><div><a>Search ADS</a></div>
    <div><a>OpenURL</a></div><div id="libkey-nomad-10-1000-example-0"><div><div>
      <a href="https://nomad.example/fallback"><div><div>Access Options</div></div></a>
    </div></div></div><div><a>Google Scholar</a></div>
  </div></div></div></div>
  <div xmlns:helper="urn:XsltStringHelper"><div><div><span>2</span><div>
    Beta B. Second study. <div>Journal Two</div>
    <div>2008</div>;<div>7</div>:<div>10</div>–20. 10.1000/source-literal.
  </div></div></div></div>
  <div xmlns:helper="urn:XsltStringHelper"><div><div><span>3</span><div>
    Gamma G. Third study.
  </div></div></div></div>
</div></div></div>
</section></body></html>"""
        )

        self.assertEqual(len(result.references), 3)
        self.assertEqual(
            [reference.block_id for reference in result.references],
            ["reference-001", "reference-002", "reference-003"],
        )
        self.assertEqual(
            result.references[0].plain_text,
            "1. Alpha A. First study. Journal One 2007;6:346–54.",
        )
        self.assertEqual(
            result.references[1].plain_text,
            "2. Beta B. Second study. Journal Two 2008;7:10–20. "
            "10.1000/source-literal.",
        )
        combined = "\n".join(
            reference.plain_text for reference in result.references
        )
        self.assertNotIn("Previous Article", combined)
        self.assertNotIn("Share", combined)
        self.assertNotIn("Export citation", combined)
        self.assertNotIn("Crossref", combined)
        self.assertNotIn("Download PDF", combined)
        self.assertNotIn("Search ADS", combined)
        self.assertNotIn("OpenURL", combined)
        self.assertNotIn("Access Options", combined)
        self.assertNotIn("Google Scholar", combined)
        self.assertNotIn("https://doi.org/", result.references[1].markdown)
        for reference in result.references:
            safe_html = block_markup_to_safe_html(
                reference.markdown, kind="reference"
            )
            self.assertTrue(
                rich_text_matches_plain(reference.plain_text, safe_html)
            )
            self.assertEqual(
                plain_text_from_safe_html(safe_html), reference.plain_text
            )

    def test_legacy_acs_reference_doi_anchor_keeps_word_boundary(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section>
<h1>Legacy ACS DOI boundary</h1>
<h2 id="flat">References</h2>
<div id="flat-content"><div>
  <div><div><div><span>1.</span><div>
    Example A. <em>J. Am. Chem. Soc.</em> <strong>1992</strong>, 114,
    5911-5919.<div><a href="https://doi.org/10.1000/example">
      https://doi.org/10.1000/example</a></div>
  </div></div></div></div>
</div></div>
</section></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertIn("5911-5919. https://doi.org/", reference.plain_text)
        self.assertIn("5911-5919. https://doi.org/", reference.markdown)
        safe_html = block_markup_to_safe_html(
            reference.markdown, kind="reference"
        )
        self.assertEqual(
            plain_text_from_safe_html(safe_html), reference.plain_text
        )
        self.assertTrue(
            rich_text_matches_plain(reference.plain_text, safe_html)
        )

    def test_legacy_acs_composite_reference_keeps_all_subentries(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section>
<h1>Legacy ACS composite bibliography</h1>
<h2 id="flat">References</h2>
<div id="flat-content"><div>
  <div><div><div><span>1.</span>
    <div><span>(a)</span><span>Alpha A.</span> First study.</div><div>(b) Beta B. Second study.</div>
    <div><span>(c)</span><span>Gamma G.</span> Third study.
      <div><a>Crossref</a></div><div><a>Google Scholar</a></div></div>
  </div></div></div>
</div></div>
</section></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertEqual(
            reference.plain_text,
            "1. (a) Alpha A. First study. (b) Beta B. Second study. "
            "(c) Gamma G. Third study.",
        )
        self.assertNotIn("Crossref", reference.plain_text)
        self.assertNotIn("Google Scholar", reference.plain_text)
        safe_html = block_markup_to_safe_html(
            reference.markdown, kind="reference"
        )
        self.assertEqual(
            plain_text_from_safe_html(safe_html), reference.plain_text
        )
        self.assertTrue(
            rich_text_matches_plain(reference.plain_text, safe_html)
        )

    def test_aacr_issue_date_and_compact_citation_are_bibliographic(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div id="issueInfo-IssueInfo_Article"><div>
  <div><span>Volume 6, Issue 1</span></div>
  <div>1 January 2007</div>
</div></div>
<div><em>Mol Cancer Ther</em> (2007) 6 (1): 346–354.</div>
<article><h1>AACR bibliography</h1></article>
</body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "date": "1 January 2007",
                "volume": "6",
                "issue": "1",
                "pages": "346–354",
            },
        )

    def test_aacr_getcitation_panel_is_bibliographic_without_issue_widget(self) -> None:
        source = """<!doctype html><html><body>
<h1>AACR Silverchair archive</h1>
<div id="getCitation"><div>Citation</div><p>
Alexis A. Kurmis; Example title. <em>Cancer Res</em>
1 May 2017; 77 (9): 2207–2212.
<a href="https://doi.org/10.1158/0008-5472.CAN-16-2503">
https://doi.org/10.1158/0008-5472.CAN-16-2503</a></p></div>
</body></html>"""

        result = self.extract(source)

        self.assertEqual(
            result.bibliographic,
            {
                "date": "1 May 2017",
                "volume": "77",
                "issue": "9",
                "pages": "2207–2212",
            },
        )
        wrong_publisher = self.extract(
            source.replace("https://doi.org/10.1158/", "https://example.test/")
        )
        malformed_pages = self.extract(source.replace("2207–2212", "e2207"))
        self.assertEqual(wrong_publisher.bibliographic, {})
        self.assertEqual(malformed_pages.bibliographic, {})

    def test_aacr_title_container_compact_citation_is_bibliographic(self) -> None:
        source = """<!doctype html><html><body><div>
<div><span>Cell Death and Survival</span><span>|</span><span>March 14 2016</span></div>
<div>
<h1>Legacy AACR article</h1>
<div><div><em>Mol Cancer Res</em> (2016) 14 (3): 253–266.</div></div>
<div><a href="https://doi.org/10.1158/1541-7786.MCR-15-0361">
https://doi.org/10.1158/1541-7786.MCR-15-0361</a></div>
</div></div></body></html>"""

        result = self.extract(source)

        self.assertEqual(
            result.bibliographic,
            {
                "date": "March 14 2016",
                "volume": "14",
                "issue": "3",
                "pages": "253–266",
            },
        )
        wrong_publisher = self.extract(
            source.replace("https://doi.org/10.1158/", "https://example.test/")
        )
        malformed_pages = self.extract(source.replace("253–266", "e253"))
        self.assertEqual(wrong_publisher.bibliographic, {})
        self.assertEqual(malformed_pages.bibliographic, {})

    def test_acs_article_banner_citation_and_dates_are_bibliographic(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div id="acs-article-3743301">
<div><div><h1>Archived ACS article</h1>
  <div>J. Phys. Chem. Lett. (2025) 16 (31): 7875–7882.</div>
  <div><span>Published Online:</span><span>July 28, 2025</span></div>
  <div><span>Published in Issue:</span><span>August 07, 2025</span></div>
</div></div></div></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "16",
                "issue": "31",
                "pages": "7875–7882",
                "first_published": "July 28, 2025",
                "date": "August 07, 2025",
            },
        )

    def test_unlabeled_acs_title_container_citation_is_bibliographic(self) -> None:
        source = """<body><div><div><div><div><span>Article</span></div>
<div><h1>Archived ACS title</h1><div><span>Received:</span>
<span>January 22, 2021</span><span>Published Online:</span>
<span>May 05, 2021</span><span>Published in Issue:</span>
<span>May 13, 2021</span></div><div>
<div><em>J. Med. Chem.</em> (2021) 64 (9): 6021–6036.</div>
<div><a href="https://doi.org/10.1021/acs.jmedchem.1c00120">
https://doi.org/10.1021/acs.jmedchem.1c00120</a></div>
</div></div></div></div></div></body>"""

        result = self.extract(source)

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "64",
                "issue": "9",
                "pages": "6021–6036",
                "first_published": "May 05, 2021",
                "date": "May 13, 2021",
            },
        )
        near_miss = self.extract(
            source.replace("https://doi.org/10.1021/", "https://example.test/")
        )
        self.assertEqual(near_miss.bibliographic, {})

    def test_acs_article_banner_date_precedes_conflicting_history_date(self) -> None:
        result = self.extract(
            """<body><div><div><div><div><span>Article</span><span>|</span>
<span>May 06, 2009</span></div><div><h1>Archived ACS date conflict</h1>
<div><span>Published Online:</span><span>May 21, 2009</span>
<span>Published in Issue:</span><span>June 23, 2009</span></div>
<div><div><em>Biochemistry</em> (2009) 48 (24): 5679–5688.</div>
<div><a href="https://doi.org/10.1021/bi900242t">
https://doi.org/10.1021/bi900242t</a></div></div>
</div></div></div></div></body>"""
        )

        self.assertEqual(result.bibliographic["first_published"], "May 06, 2009")
        self.assertEqual(result.bibliographic["date"], "June 23, 2009")

    def test_aacr_bibliographic_grammar_is_marker_gated_and_strict(self) -> None:
        without_marker = self.extract(
            """<!doctype html><html><body>
<div><em>Mol Cancer Ther</em> (2007) 6 (1): 346–354.</div>
<article><h1>Non-AACR bibliography</h1></article>
</body></html>"""
        )
        malformed_aacr = self.extract(
            """<!doctype html><html><body>
<div id="issueInfo-IssueInfo_Article"><div>Published 1 January 2007</div></div>
<div><em>Mol Cancer Ther</em> (2007) 6 (1): e346.</div>
<article><h1>Malformed AACR bibliography</h1></article>
</body></html>"""
        )

        self.assertEqual(without_marker.bibliographic, {})
        self.assertEqual(malformed_aacr.bibliographic, {})

    def test_aacr_flat_div_references_reject_noncontiguous_labels(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Malformed AACR target</h1>
<section><h2 id="flat">References</h2>
  <div id="flat-content"><div>
    <div><div><div><span>1</span><div>Flat first reference.</div></div></div></div>
    <div><div><div><span>3</span><div>Flat noncontiguous reference.</div></div></div></div>
  </div></div>
  <ol><li><span>1.</span> Fallback list reference.</li></ol>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            "1. Fallback list reference.",
        )

    def test_oup_silverchair_flat_div_references_exclude_linkout_controls(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>OUP article</h1><h2>REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><p><span><span></span></span></p>
<div>Author A.</div> <div>First authored citation</div>. <div>Journal A.</div> <div>2020</div>.
<p></p><div><p><a>Google Scholar</a></p><div><a>OpenURL Placeholder Text</a></div><p><a>WorldCat</a></p></div>
</div></div></div></div>
<div content-id="B2"><div><span><a name="jumplink-B2" aria-label="jumplink-B2"></a></span></div>
<div><div id="ref-auto-B2"><span>2.</span><div><p><span><span></span></span></p>
<div>Author B.</div> <div>Second authored citation</div>. <div>Journal B.</div> <div>2021</div>.
<p></p><div><p><a>Google Scholar</a></p><div><a href="http://dx.doi.org/10.1000/test">Crossref</a></div><div><a>Search ADS</a></div><p><a>WorldCat</a></p></div>
</div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Author A. First authored citation. Journal A. 2020.",
                (
                    "2. Author B. Second authored citation. Journal B. 2021. "
                    "DOI: https://doi.org/10.1000/test"
                ),
            ],
        )
        self.assertNotIn("Google Scholar", result.references[0].plain_text)
        self.assertNotIn("Crossref", result.references[1].plain_text)

    def test_oup_silverchair_anonymous_modal_figures_and_library_controls(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div>
<h1>Older OUP article</h1><h2 id="100">Abstract</h2>
<section aria-label="Main abstract"><div><div>Aims</div><p>Summary.</p></div></section>
<div><h4>Gift article access</h4><p>As a benefit of your subscription, you can share temporary access to restricted articles.</p></div>
<h2 id="101">1. Introduction</h2><p>Body.</p>
<div swap-content-for-modal="true"><div><img src="{pixel}" alt="Panel A."><div>
<div id="label-12345">Figure 1</div><div><p>Authored caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.gif">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></div>
<h2 id="102">References</h2><div>
<div content-id="CVN355C1"><div><span><a name="jumplink-CVN355C1" aria-label="jumplink-CVN355C1"></a></span></div>
<div><div id="ref-auto-CVN355C1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>Find in my library</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Aims: Summary.")
        self.assertEqual(
            result.sections[0].blocks[0].markdown,
            "<strong>Aims:</strong> Summary.",
        )
        self.assertEqual(result.figures[0].figure_id, "figure_001")
        self.assertEqual(result.figures[0].caption_plain, "Authored caption.")
        self.assertEqual(len(result.embedded_assets), 1)
        self.assertNotIn(
            "Gift article access", [section.heading for section in result.sections]
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Author A. Citation."],
        )

    def test_oup_toolbar_free_references_and_unheaded_back_matter(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div id="ContentColumn">
<div><h1>Legacy NAR article</h1></div>
<div><h3>Cite</h3><p>Publisher interface prose.</p></div>
<div><h2 id="100">Abstract</h2>
<section aria-label="Main abstract"><p>Summary.</p></section>
<h2 id="101">INTRODUCTION</h2><p>Body.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Authored caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<p>This work was supported by Grant 1. We thank A. Researcher.</p>
<p><em>Conflict of interest statement.</em> None declared.</p>
<h2 id="102">REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1</span><div><p>Author, A., Writer, B., Scientist, C.</p><div>2006</div>First title <div>Journal X</div>. <div>12</div><div>34</div>–39<p></p></div></div></div></div>
<div content-id="B2"><div><span><a name="jumplink-B2" aria-label="jumplink-B2"></a></span></div>
<div><div id="ref-auto-B2"><span>2</span><div><p>Author, B., Jr, Writer, C.</p><div>Journal Y</div>. in press<p></p></div></div></div></div>
</div></div></div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "INTRODUCTION", "ACKNOWLEDGEMENTS"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[-1].blocks],
            [
                "This work was supported by Grant 1. We thank A. Researcher.",
                "Conflict of interest statement. None declared.",
            ],
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Author, A., Writer, B. and Scientist, C. (2006) First title. Journal X., 12, 34–39.",
                "2. Author, B., Jr and Writer, C. Journal Y., in press.",
            ],
        )
        self.assertEqual(
            result.references[0].markdown,
            (
                "1. Author, A., Writer, B. and Scientist, C. (2006) First title. <em>Journal X.</em>, "
                "<strong>12</strong>, 34–39."
            ),
        )
        self.assertNotIn(
            "Publisher interface prose.",
            [block.plain_text for section in result.sections for block in section.blocks],
        )

    def test_oup_toolbar_free_references_preserve_inline_and_proceedings(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy OUP references</h1><h2>REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1</span><div><p>Author, A.</p><div>2005</div>CO<sub>2</sub> binding title <div>Journal X</div>. <div>12</div><div>34</div>–39<p></p></div></div></div></div>
<div content-id="B2"><div><span><a name="jumplink-B2" aria-label="jumplink-B2"></a></span></div>
<div><div id="ref-auto-B2"><span>2</span><div><p>Author, B.</p><div>1987</div>Length-dependent study <em>Proceedings Venue</em>, State University Press pp. <div>198</div>–199<p></p></div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Author, A. (2005) CO_{2} binding title. Journal X., 12, 34–39.",
                (
                    "2. Author, B. (1987) Length-dependent study. Proceedings "
                    "Venue, State University Press pp. 198–199."
                ),
            ],
        )
        self.assertIn("CO<sub>2</sub> binding title", result.references[0].markdown)
        self.assertIn("<em>Proceedings Venue</em>", result.references[1].markdown)

    def test_oup_silverchair_reference_toolbar_allows_orphan_nbsp_marker(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>OUP article</h1><h2>REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a>Â</div>
</div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Author A. Citation."],
        )

    def test_oup_silverchair_reference_toolbar_allows_no_worldcat_link(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>OUP article</h1><h2>REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Dissertation.</div>
<div><a>Google Scholar</a><a>Crossref</a><a>Search ADS</a></div>
</div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Author A. Dissertation."],
        )

    def test_oup_silverchair_reference_toolbar_allows_google_scholar_only(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>OUP article</h1><h2>REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Biacore AB. BIAevaluation Software Handbook.</div>
<div><a href="https://scholar.google.com/scholar_lookup?title=BIAevaluation%20Software%20Handbook">Google Scholar</a></div>
</div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Biacore AB. BIAevaluation Software Handbook."],
        )

    def test_oup_silverchair_reference_ids_allow_one_publisher_prefix(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>OUP article</h1><h2>REFERENCES</h2><div>
<div content-id="gkr970-B1"><div><span><a name="jumplink-gkr970-B1" aria-label="jumplink-gkr970-B1"></a></span></div>
<div><div id="ref-auto-gkr970-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>Crossref</a><a>Search ADS</a></div>
</div></div></div></div>
<div content-id="gkr970-B2"><div><span><a name="jumplink-gkr970-B2" aria-label="jumplink-gkr970-B2"></a></span></div>
<div><div id="ref-auto-gkr970-B2"><span>2.</span><div><div>Author B. Citation.</div>
<div><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Author A. Citation.", "2. Author B. Citation."],
        )

    def test_oup_silverchair_unidentified_accessible_table_modal_is_structured(
        self,
    ) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div><h1>OUP table article</h1></div>
<div><h2 id="100">Abstract</h2><section aria-label="Main abstract"><p>Summary.</p></section>
<h2 id="101">INTRODUCTION</h2><p>Body.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<div swap-content-for-modal="true"><div>
<div id="gkr970-T1"><span id="label-81189">Table 1.</span><div>
<a role="button" target="_blank" href="/view-large/83644359" aria-describedby="label-81189">Open in new tab</a></div>
<div id="caption-81189"><p>Authored table title</p></div></div>
<div><table role="table" aria-labelledby=" label-gkr970-T1" aria-describedby=" caption-gkr970-T1">
<thead><tr><th rowspan="2">Group<span aria-hidden="true">. </span></th><th>Value</th></tr></thead>
<tbody><tr><td>A</td><td>4.2 ± 0.3</td></tr></tbody></table></div>
<div><table aria-hidden="true"><thead><tr><th rowspan="2">Group</th><th>Value</th></tr></thead>
<tbody><tr><td>A</td><td>4.2 ± 0.3</td></tr></tbody></table></div>
<div><span id="fn-gkr970-TF1"></span><div content-id="gkr970-TF1"><span><p>Mean and standard deviation.</p></span></div></div>
</div></div>
<h2 id="102">REFERENCES</h2><div>
<div content-id="gkr970-B1"><div><span><a name="jumplink-gkr970-B1" aria-label="jumplink-gkr970-B1"></a></span></div>
<div><div id="ref-auto-gkr970-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_001")
        self.assertEqual(table.source_id, "gkr970-T1")
        self.assertEqual(table.title_plain, "Table 1. Authored table title")
        self.assertEqual(table.footnotes_plain, ["Mean and standard deviation."])
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Group", "Value"], ["A", "4.2 ± 0.3"]],
        )
        self.assertEqual(
            [
                block.plain_text
                for section in result.sections
                for block in section.blocks
            ],
            ["Summary.", "Body."],
        )

    def test_oup_silverchair_tbl_accessible_table_modal_is_structured(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div><h1>Older OUP table article</h1></div>
<div><h2 id="100">Abstract</h2><section aria-label="Main abstract"><p>Summary.</p></section>
<h2 id="101">INTRODUCTION</h2><p>Body.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<div swap-content-for-modal="true"><div>
<div id="TBL1"><span id="label-81189">Table 1</span><div>
<a role="button" target="_blank" href="/view-large/83644359" aria-describedby="label-81189">Open in new tab</a></div>
<div id="caption-81189"><p>Authored table title</p></div></div>
<div><table role="table" aria-labelledby=" label-TBL1" aria-describedby=" caption-TBL1">
<thead><tr><th>Group</th><th>Value</th><th>Left</th><th>Right</th></tr></thead>
<tbody><tr><td>A</td><td>4.2 <span id="jumplink-TF1-1"></span><a reveal-id="TF1-1">a</a><sup>,</sup><span id="jumplink-TF1-2"></span><a reveal-id="TF1-2">b</a></td><td>X</td><td>Y</td></tr>
<tr><td>Group</td><td>Value</td><td>Second left</td><td>Second right</td></tr></tbody></table></div>
<div><table aria-hidden="true"><thead><tr><th>Group</th><th>Value</th><th>Left</th><th>Right</th></tr></thead>
<tbody><tr><td>A</td><td>4.2 <span id="jumplink-TF1-1"></span><a reveal-id="TF1-1">a</a><sup>,</sup><span id="jumplink-TF1-2"></span><a reveal-id="TF1-2">b</a></td><td>X</td><td>Y</td></tr>
<tr><td>Group</td><td>Value</td><td>Second left</td><td>Second right</td></tr></tbody></table></div>
<div><span id="fn-"></span><div content-id=""><span><p>ND, not determined.</p></span></div>
<span id="fn-TF1-1"></span><div content-id="TF1-1"><span><p><sup>a</sup>First marked note.</p></span></div>
<span id="fn-TF1-2"></span><div content-id="TF1-2"><span><p><sup>b,c</sup>Combined marked note.</p></span></div></div>
</div></div>
<h2 id="102">REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "TBL1")
        self.assertEqual(table.title_plain, "Table 1 Authored table title")
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] First marked note.",
                "[b,c] Combined marked note.",
                "ND, not determined.",
            ],
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [
                ["Group", "Value", "Left", "Right"],
                ["A", "4.2 ^{a,b}", "X", "Y"],
                ["Group", "Value", "Second left", "Second right"],
            ],
        )
        self.assertTrue(all(cell.header for cell in table.parts[0].rows[-1]))
        self.assertEqual(
            [
                block.plain_text
                for section in result.sections
                for block in section.blocks
            ],
            ["Summary.", "Body."],
        )

    def test_oup_silverchair_legacy_tb_table_accepts_direct_note_sequence(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div><h1>Legacy OUP table notes</h1></div>
<div data-extraction-oup-legacy-modal="true"><h2 id="100">Abstract</h2><section aria-label="Main abstract"><p>Summary.</p></section>
<h2 id="101">RESULTS</h2><p>Body.</p>
<div swap-content-for-modal="true"><div>
<div id="GKG206TB1"><span id="label-25544"><strong>Table 1.</strong></span><div>
<a role="button" target="_blank" href="/view-large/38162430" aria-describedby="label-25544">Open in new tab</a></div>
<div id="caption-25544"><p>Binding constants</p></div></div>
<div><table role="table" aria-labelledby=" label-GKG206TB1" aria-describedby=" caption-GKG206TB1"><tbody>
<tr><td>Molecule</td><td><em>K</em><sub>i</sub><sup>a</sup></td><td>Linker<sup>b</sup></td></tr>
<tr><td>PA1</td><td>2</td><td>Two</td></tr></tbody></table></div><div></div>
<div><p>NA, not applicable.</p><p><sup>a</sup><em>K</em><sub>i</sub> is the inhibition constant.</p>
<p><sup>b</sup>Number of repeats.</p></div></div></div>
<h2 id="102">REFERENCES</h2><div></div></div></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "GKG206TB1")
        self.assertEqual(table.title_plain, "Table 1. Binding constants")
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] K_{i} is the inhibition constant.",
                "[b] Number of repeats.",
                "NA, not applicable.",
            ],
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Molecule", "K_{i}^{a}", "Linker^{b}"], ["PA1", "2", "Two"]],
        )
        self.assertTrue(all(cell.header for cell in table.parts[0].rows[0]))
        self.assertTrue(all(not cell.header for cell in table.parts[0].rows[1]))

    def test_oup_silverchair_legacy_image_table_modal_is_preserved(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div><h1>OUP raster-table article</h1></div>
<div><h2 id="100">Abstract</h2><section aria-label="Main abstract"><p>Summary.</p></section>
<h2 id="101">INTRODUCTION</h2><p>Body.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<div swap-content-for-modal="true"><div>
<div id="T1"><span id="label-81189"><strong>Table 1.</strong></span><div>
<a role="button" target="_blank" href="/view-large/83644359" aria-describedby="label-81189">Open in new tab</a></div>
<div id="caption-81189"><p>Authored raster table</p></div></div>
<div><table role="table" aria-labelledby=" label-T1" aria-describedby=" caption-T1">
<tbody><tr><td><img src="{pixel}" alt="graphic"></td></tr></tbody></table></div>
<div><table aria-hidden="true"><tbody><tr><td><inline-graphic href="articlei1.jpeg"></inline-graphic></td></tr></tbody></table></div>
<div><span id="fn-"></span><div content-id=""><span><p>Unmarked note.</p></span></div>
<span id="fn-TF1"></span><div content-id="TF1"><span><p><sup>a</sup>Marked note.</p></span></div></div>
</div></div>
<h2 id="102">REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_001")
        self.assertEqual(table.source_id, "T1")
        self.assertEqual(table.source_kind, "image")
        self.assertEqual(table.title_plain, "Table 1. Authored raster table")
        self.assertEqual(table.footnotes_plain, ["[a] Marked note.", "Unmarked note."])
        table_assets = [
            asset for asset in result.embedded_assets if asset.category == "table"
        ]
        self.assertEqual(len(table_assets), 1)
        self.assertEqual(table_assets[0].parent_table_id, None)
        self.assertEqual(table_assets[0].output_path, "tables/table_001/source.gif")
        self.assertEqual(
            [
                block.plain_text
                for section in result.sections
                for block in section.blocks
            ],
            ["Summary.", "Body."],
        )

    def test_oup_silverchair_flat_div_references_require_contiguous_ids(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Malformed OUP article</h1><h2>REFERENCES</h2><div>
<div content-id="B2"><div><span><a name="jumplink-B2" aria-label="jumplink-B2"></a></span></div>
<div><div id="ref-auto-B2"><span>2.</span><div><div>Must not be accepted.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div>
</div></article></body></html>"""
        )

        self.assertEqual(result.references, [])

    def test_oup_silverchair_body_scope_and_front_matter_are_recovered(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div>
<div><h1>Scoped OUP article</h1>
<span id="author-flyout-123"><div>
  <div><div>Alex Author</div></div><div><div>Institute A</div></div>
  <div><div content-id="cor1">Email: alex@example.test</div></div>
  <div>Search for other works by this author on:</div><div><a>Oxford Academic</a></div>
</div></span>
<div>Journal X, Volume 2, Issue 3, 01 May 2020, Pages 4–9,
  <a href="https://doi.org/10.1000/oup-test">https://doi.org/10.1000/oup-test</a></div>
<div><div>Received:</div><div>01 January 2020</div></div>
<div><div>Revision received:</div><div>02 February 2020</div></div>
<div><div>Accepted:</div><div>03 March 2020</div></div>
<div><div>Published:</div><div>04 April 2020</div></div>
</div>
<div><h3>Cite</h3><p>Publisher interface prose.</p></div>
<div><h2 id="100">Abstract</h2><section aria-label="Main abstract"><p>Authored abstract.</p></section>
<div><div><a>alpha</a>, <a>beta</a></div><div>Issue Section: Research</div></div>
<h2 id="101">INTRODUCTION</h2><p>Authored introduction.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Authored caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<h2 id="102">REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "INTRODUCTION"],
        )
        self.assertNotIn(
            "Publisher interface prose.",
            [block.plain_text for section in result.sections for block in section.blocks],
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Keywords: alpha; beta",
                "Affiliation: Institute A",
                "Correspondence: Alex Author — Email: alex@example.test",
                (
                    "Citation details: Journal X, Volume 2, Issue 3, 01 May 2020, "
                    "Pages 4–9, https://doi.org/10.1000/oup-test"
                ),
                (
                    "Publication history: Received: 01 January 2020; Revision "
                    "received: 02 February 2020; Accepted: 03 March 2020; "
                    "Published: 04 April 2020"
                ),
            ],
        )

    def test_oup_no_affiliation_header_and_single_acknowledgement_are_recovered(
        self,
    ) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div>
<div><h1>OUP header without affiliations</h1>
<span id="author-flyout-123"><div><div>Karen Author</div>
<div><div content-id="COR1">*To whom correspondence should be addressed. Email: moses@example.test</div></div>
<div>Search for other works by this author on:</div><div>Oxford Academic</div><div>PubMed</div><div>Google Scholar</div>
</div></span>
<div>Journal X, Volume 2, Issue 3, 01 May 2020, Pages 4–9,
  <a href="https://doi.org/10.1000/oup-test">https://doi.org/10.1000/oup-test</a></div>
<div><div>Received:</div><div>01 January 2020</div></div>
<div><div>Revision received:</div><div>02 February 2020</div></div>
<div><div>Accepted:</div><div>03 March 2020</div></div>
<div><div>Published:</div><div>04 April 2020</div></div>
</div>
<div><h2 id="100">Abstract</h2><section aria-label="Main abstract"><p>Summary.</p></section>
<h2 id="101">INTRODUCTION</h2><p>Body.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<div content-id=""><span><div><p>Correspondence may also be addressed to W. Writer. Tel: +1 555 0100; Fax: +1 555 0101; Email: writer@example.test</p></div></span></div>
<p>The Open Access publication charges for this article were waived by Oxford University Press.</p>
<h2 id="102">REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></div></body></html>"""
        )

        self.assertIn(
            "Correspondence: To whom correspondence should be addressed. "
            "Email: moses@example.test",
            [block.plain_text for block in result.front_matter],
        )
        self.assertNotIn(
            "Karen Author — To whom correspondence should be addressed.",
            " ".join(block.plain_text for block in result.front_matter),
        )
        self.assertIn(
            "Additional correspondence: Correspondence may also be addressed "
            "to W. Writer. Tel: +1 555 0100; Fax: +1 555 0101; Email: "
            "writer@example.test",
            [block.plain_text for block in result.front_matter],
        )
        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "INTRODUCTION", "ACKNOWLEDGEMENTS"],
        )
        self.assertEqual(
            result.sections[-1].blocks[0].plain_text,
            "The Open Access publication charges for this article were waived "
            "by Oxford University Press.",
        )

    def test_oup_silverchair_author_note_and_acknowledgments_are_structured(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        author_note = (
            "The authors wish it to be known that, in their opinion, the first "
            "two authors should be regarded as Joint First Authors."
        )
        result = self.extract(
            f"""<!doctype html><html><body><div>
<div><h1>OUP back matter</h1>
<span id="author-flyout-123"><div>
  <div><div>Alex Author</div></div><div><div><span>1</span>Institute A</div></div>
  <div><div>†</div><p>The authors wish it to be known that, in their opinion, the first two authors should be regarded as Joint First Authors.</p></div>
  <div>Search for other works by this author on:</div><div><a>Oxford Academic</a></div>
</div></span>
<span id="author-flyout-124"><div>
  <div><div>Bailey Author</div></div><div><div><span>1</span>Institute A</div></div>
  <div><div>†</div><p>The authors wish it to be known that, in their opinion, the first two authors should be regarded as Joint First Authors.</p></div>
  <div>Search for other works by this author on:</div><div><a>Oxford Academic</a></div>
</div></span>
<div>Journal X, Volume 2, Issue 3, 01 May 2020, Pages 4–9,
  <a href="https://doi.org/10.1000/oup-test">https://doi.org/10.1000/oup-test</a></div>
<div><div>Received:</div><div>01 January 2020</div></div>
<div><div>Revision received:</div><div>02 February 2020</div></div>
<div><div>Accepted:</div><div>03 March 2020</div></div>
<div><div>Published:</div><div>04 April 2020</div></div>
</div>
<div><h2 id="100">Abstract</h2><p>Authored abstract.</p>
<h2 id="101">INTRODUCTION</h2><p>Authored introduction.</p>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A."><div><div id="label-12345">Figure 1.</div>
<div><p>Authored caption.</p></div><div>
<a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a>
</div></div></div></figure>
<h2 id="102">DISCUSSION</h2><p>Discussion prose.</p>
<p>We thank the laboratory for assistance.</p>
<h2 id="103">FUNDING</h2><p>Funding prose.</p><div>{author_note}</div>
<h2 id="104">REFERENCES</h2><div>
<div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div>
<div><div id="ref-auto-B1"><span>1.</span><div><div>Author A. Citation.</div>
<div><a>Google Scholar</a><a>OpenURL Placeholder Text</a><a>WorldCat</a></div>
</div></div></div></div></div>
</div></div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "INTRODUCTION", "DISCUSSION", "ACKNOWLEDGMENTS", "FUNDING"],
        )
        self.assertEqual(
            result.sections[3].blocks[0].plain_text,
            "We thank the laboratory for assistance.",
        )
        self.assertEqual(
            [
                block.plain_text
                for block in result.front_matter
                if block.plain_text.startswith("Author note: ")
            ],
            [f"Author note: {author_note}"],
        )
        self.assertEqual(result.front_matter[0].plain_text, "Affiliation: 1 Institute A")
        self.assertNotIn(
            author_note,
            [block.plain_text for section in result.sections for block in section.blocks],
        )

    def test_nested_main_abstract_scope_does_not_become_introduction(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div id="acs-article-123">
<h1>Nested abstract article</h1>
<h2 id="100">Abstract</h2><div id="100-content">
  <section aria-label="Main abstract"><section><p>Authored abstract.</p></section></section>
</div>
<h2 id="101">Introduction</h2><div id="101-content"><p>Authored introduction.</p></div>
</div></body></html>"""
        )

        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Abstract", ["Authored abstract."]),
                ("Introduction", ["Authored introduction."]),
            ],
        )

    def test_acs_numeric_aria_abstract_does_not_absorb_unheaded_body(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section><div>
<h1>Archived ACS article</h1>
<a href="https://doi.org/10.1021/example">https://doi.org/10.1021/example</a>
<h2 id="100">Abstract</h2><div>
  <div id="100-content"><section aria-label="Main abstract">
    <p>Authored abstract only.</p>
  </section></div>
  <div><span>Subjects</span></div>
  <div id="101-content"><p>First unheaded body paragraph.</p></div>
  <div id="102-content"><p>Second unheaded body paragraph.</p></div>
</div>
<div><p>Body paragraph after an authored table.</p></div>
<h2 id="103">Experimental Procedures</h2><div id="103-content"><p>Method details.</p></div>
</div></section></body></html>"""
        )

        self.assertEqual(
            [
                (section.heading, [block.plain_text for block in section.blocks])
                for section in result.sections
            ],
            [
                ("Abstract", ["Authored abstract only."]),
                (
                    "Main text",
                    [
                        "First unheaded body paragraph.",
                        "Second unheaded body paragraph.",
                        "Body paragraph after an authored table.",
                    ],
                ),
                ("Experimental Procedures", ["Method details."]),
            ],
        )

    def test_old_acs_split_div_figure_caption_is_extracted(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS figure dialect</h1><section><h2>Results</h2>
<figure id="fig1"><div>
  <div>Figure 1.</div>
  <div><p>Cleavage at 5′-TGGT-3′ by <strong>7R</strong>.</p></div>
</div></figure>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Cleavage at 5′-TGGT-3′ by 7R.",
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "Cleavage at 5′-TGGT-3′ by <strong>7R</strong>.",
        )

    def test_old_acs_split_div_scheme_caption_keeps_local_footnote(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS scheme dialect</h1><section><h2>Results</h2>
<figure id="scheme-1"><div>
  <div>Scheme 1.</div>
  <div><div>Convergent ligation of fragments <strong>8</strong> and
    <strong>13</strong>.<sup><a reveal-id="sch1-fn1">a</a></sup></div></div>
</div><div id="sch1-fn1"><span>a</span>
  <p>For coupling conditions, see the Supporting Information.</p>
</div></figure>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Scheme 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Convergent ligation of fragments 8 and 13.^{a} For coupling "
            "conditions, see the Supporting Information.",
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "Convergent ligation of fragments <strong>8</strong> and "
            "<strong>13</strong>.<sup>a</sup>\n\nFor coupling conditions, "
            "see the Supporting Information.",
        )

    def test_oup_silverchair_modal_figure_caption_excludes_controls(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>OUP figure dialect</h1><section><h2>Results</h2>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="{pixel}" alt="Panel A and panel B."><div>
<div id="label-12345">Figure 1.</div>
<div><p>(<strong>A</strong>) Authored β caption.</p></div>
<div><a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a>
<a aria-describedby="label-12345" href="/DownloadFile/DownloadImage.aspx?image=example">Download slide</a></div>
</div></div></figure></section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(result.figures[0].caption_plain, "(A) Authored β caption.")
        self.assertEqual(
            result.figures[0].caption_markdown,
            "(<strong>A</strong>) Authored β caption.",
        )
        self.assertNotIn("Open in new tab", result.figures[0].caption_plain)

    def test_oup_silverchair_modal_figure_requires_exact_toolbar(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Near-miss OUP figure</h1><section><h2>Results</h2>
<figure swap-content-for-modal="true" id="figure-1"><div>
<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Panel A."><div>
<div id="label-12345">Figure 1.</div><div><p>Must not be accepted.</p></div>
<div><a aria-describedby="label-12345" href="/view-large/figure/12345/example.jpg">Open in new tab</a></div>
</div></div></figure></section></article></body></html>"""
        )

        self.assertEqual(result.figures[0].caption_plain, "")

    def test_sanitized_acs_record_media_tables_abstract_and_references(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><div id="article-record">
<h1>Sanitized ACS record</h1>
<div><div><div id="100-content"><div><h2>Visual Abstract</h2>
  <section aria-label="Main abstract"><p></p><div><a><img src="{pixel}"
    alt="Graphic. Refer to the image caption for details."></a></div></section>
</div></div></div></div>
<h2 id="200">Abstract</h2><div><div id="200-content">
  <section aria-label="Main abstract"><p>Authored summary.</p></section>
</div></div>
<h2 id="201">Introduction</h2><div>
  <div id="202-content"><p>Opening DNA-
binding and Im-
Py prose with <em>K</em>
<sub>a</sub>.</p></div>
  <div>Relationship before equation.
    <div><span id="ufd00001"><em>K</em><sub>a</sub> = 2 × 10<sup>9</sup> (1)</span></div>
    Relationship after equation.
  </div>
  <div id="300-content"><a id="300"></a><div>
    <div>Figure 1</div><div><a><img src="{pixel}"
      alt="Figure 1. Refer to the image caption for details."></a></div>
    <div><div>Figure 1.</div><div><p>Authored β caption.</p></div></div>
  </div></div>
  <div id="400-content"><a id="400"></a><div>
    <div id="ja000t00001"><span>Table 1.</span><div>Measured K<sub>a</sub><sup>a</sup></div></div>
    <div><table><caption><img src="{pixel}"
      alt="Graphic. Refer to the image caption for details."></caption>
      <thead><tr><th>Agent</th><th>K<sub>a</sub></th></tr></thead>
      <tbody><tr><td>1</td><td>2.0 × 10<sup>9</sup><em><sup>b</sup></em></td></tr></tbody></table></div>
    <div><p><em><sup>a</sup></em> Authored table note.<em><sup>b</sup></em>
      Rate <em>K</em><sub>eq</sub> was calculated.<em><sup>c</sup></em>
      Not determined.</p></div>
    <div><a aria-label="View large Table 1.">View Large</a></div>
  </div></div>
</div>
<h2 id="500">References</h2><div>
  <div><div><span>1.</span><div>Alpha A. <em>Journal</em> 2007.
    <div><a>Crossref</a></div></div>
    <div>(b) Beta B. <em>Other Journal</em> 2008.
    <div><a>Google Scholar</a></div></div></div></div>
</div>
</div></body></html>"""
        )

        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Abstract", ["Authored summary."]),
                (
                    "Introduction",
                    [
                        "Opening DNA-binding and Im-Py prose with K_{a}.",
                        "Relationship before equation.",
                        "K_{a} = 2 × 10^{9} (1)",
                        "Relationship after equation.",
                    ],
                ),
            ],
        )
        self.assertEqual(result.sections[1].blocks[2].kind, "equation")
        self.assertEqual(
            [(figure.figure_id, figure.label, figure.caption_plain)
             for figure in result.figures],
            [
                ("graphical_abstract", "Graphical Abstract", ""),
                ("figure_001", "Figure 1", "Authored β caption."),
            ],
        )
        table = result.tables[0]
        self.assertEqual(table.source_kind, "image")
        self.assertEqual(table.title_plain, "Table 1. Measured K_{a}^{a}")
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Agent", "K_{a}"], ["1", "2.0 × 10^{9} ^{b}"]],
        )
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] Authored table note.",
                "[b] Rate K_{eq} was calculated.",
                "[c] Not determined.",
            ],
        )
        self.assertEqual(
            result.references[0].plain_text,
            "1. Alpha A. Journal 2007. (b) Beta B. Other Journal 2008.",
        )
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [
                ("graphical_abstract", "figures/main/graphical_abstract.gif"),
                ("figure_001", "figures/main/figure_001.gif"),
                ("table_001", "tables/table_001/source.gif"),
            ],
        )

    def test_acs_article_source_fragment_restores_main_text_table_and_references(
        self,
    ) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        result = self.extract(
            f"""<body><div id="article-source"><div><div>
<div><h1 id="aria123">Legacy ACS communication</h1>
<a href="https://doi.org/10.1021/example">https://doi.org/10.1021/example</a>
</div></div></div>
<div id="100-content"><figure id="visual-abstract"><h2>Visual Abstract</h2>
<section aria-label="Main abstract"><p></p><div><a><img src="{pixel}"
alt="Graphic. Refer to the image caption for details."></a></div><p></p></section>
</figure></div>
<h2 id="101">Abstract</h2><div>
  <div id="101-content"><section aria-label="Main abstract"><p>Authored abstract.</p></section></div>
  <div id="102-content"><p>First unheaded body paragraph.</p></div>
  <div id="103-content"><figure id="figure-1"><img src="{pixel}"
    alt="Figure 1. Refer to the image caption for details."><figcaption>Figure 1. Authored caption.</figcaption></figure></div>
</div>
<div><div id="104-content"><p>Second unheaded body paragraph.</p></div></div>
<div><div id="105-content"><figure content-id="tbl1 " id="table-1">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Authored values<a reveal-id="tbl1-fn1">a</a></div></div></div>
  <div><table role="presentation" aria-labelledby="label-tbl1" aria-describedby="caption-tbl1"><tbody><tr><td><a><img
    src="{pixel}" alt="Graphic. Refer to the image caption for details."
    path-from-xml="example_0001.tif"></a></td></tr></tbody></table></div>
  <div></div>
  <div><div id="tbl1-fn1" content-id="tbl1-fn1"><span><span rel="nofollow">a</span></span><p>Authored note.</p></div></div>
  <div><a href="/view-large/105" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div><div id="106-content"><p>Final body paragraph.</p></div></div>
<h2 id="si1">Supporting Information</h2><div><p>Supporting description.</p></div>
<h2 id="107">Acknowledgments</h2><div><p>Thanks.</p></div>
<h2 id="108">References</h2><ol>
  <li>Alpha A. Journal 2001.Crossref OpenURL Google Scholar</li>
  <li>(a)Beta B. Journal 2002.Crossref Download PDF OpenURL Google Scholar</li>
  <li>(b) Gamma C. Journal 2003.Crossref Article Link OpenURL Google Scholar</li>
</ol><div>Copyright © 2009 American Chemical Society</div>
</div></body>"""
        )

        self.assertEqual(
            [
                (section.heading, [block.plain_text for block in section.blocks])
                for section in result.sections
            ],
            [
                ("Abstract", ["Authored abstract."]),
                (
                    "Main text",
                    [
                        "First unheaded body paragraph.",
                        "Second unheaded body paragraph.",
                        "Final body paragraph.",
                    ],
                ),
                ("Acknowledgments", ["Thanks."]),
            ],
        )
        self.assertEqual(
            [(figure.figure_id, figure.label) for figure in result.figures],
            [("graphical_abstract", "Graphical Abstract"), ("figure_001", "Figure 1")],
        )
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_kind, "image")
        self.assertEqual(
            [block.plain_text for block in result.references],
            [
                "1. Alpha A. Journal 2001.",
                "2. (a) Beta B. Journal 2002. (b) Gamma C. Journal 2003.",
            ],
        )

    def test_acs_article_source_accepts_semantic_table_with_empty_hidden_rendering(
        self,
    ) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        result = self.extract(
            f"""<body><div id="article-source"><div><div>
<div><h1 id="aria123">Legacy ACS semantic article</h1>
<a href="https://doi.org/10.1021/example">https://doi.org/10.1021/example</a>
</div></div></div>
<div id="100-content"><figure id="visual-abstract"><h2>Visual Abstract</h2>
<section aria-label="Main abstract"><p></p><div><a><img src="{pixel}"
alt="Graphic. Refer to the image caption for details."></a></div><p></p></section>
</figure></div>
<h2 id="101">Abstract</h2><div><div id="101-content">
<section aria-label="Main abstract"><p>Authored abstract.</p></section></div>
<div id="102-content"><p>Unheaded introduction.</p>
<figure id="figure-1"><img src="{pixel}"
alt="Figure 1. Refer to the image caption for details.">
<figcaption>Figure 1. Authored caption.</figcaption></figure></div></div>
<h2 id="102">Results</h2>
<div id="103-content"><a id="103"></a><div content-id="tbl1 sec2.2">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1">Authored values</div></div>
  <div><table role="table" aria-labelledby="label-tbl1"
      aria-describedby="caption-tbl1"><thead><tr><th>Agent</th><th>Value</th></tr></thead>
    <tbody><tr><td>A</td><td>75</td></tr></tbody></table></div>
  <div></div>
  <div><div id="t1fn1" content-id="t1fn1"><span><span>a</span></span>
    <p>Authored note.</p></div></div>
  <div><a href="/view-large/103" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div>
<h2 id="si1">Supporting Information</h2><div><p>Supporting description.</p></div>
<h2 id="104">Acknowledgments</h2><div><p>Thanks.</p></div>
<h2 id="105">References</h2><ol>
  <li>Alpha A. Journal 2001.Crossref OpenURL Google Scholar</li>
  <li>Beta B. Journal 2002.Crossref Download PDF OpenURL Google Scholar</li>
</ol></div></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_kind, "html")
        self.assertEqual(result.tables[0].title_plain, "Table 1. Authored values")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Agent", "Value"], ["A", "75"]],
        )
        self.assertEqual(
            [block.plain_text for block in result.references],
            ["1. Alpha A. Journal 2001.", "2. Beta B. Journal 2002."],
        )
        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Main text", "Results", "Acknowledgments"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored abstract."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded introduction."],
        )

        near_miss = self.extract(
            f"""<!doctype html><html><body><article><h1>Near miss</h1>
<section><h2>Results</h2><div id="103-content"><div content-id="tbl1 sec2.2">
<div id="tbl1"><span id="label-tbl1">Table 1.</span><div id="caption-tbl1">Values</div></div>
<div><table role="table" aria-labelledby="label-tbl1" aria-describedby="caption-tbl1">
<tr><th>A</th></tr></table></div><div>unverified hidden content</div>
<div><a href="/view-large/103" target="_blank" rel="nofollow"
aria-label="View large Table 1.">View Large</a></div>
</div></div></section></article></body></html>"""
        )
        self.assertEqual(near_miss.tables, [])

    def test_acs_raster_figure_tables_are_not_graphical_abstracts(self) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        source = f"""<!doctype html><html><body><main>
<h1>ACS raster table export</h1><section><h2>Results</h2>
<div id="101-content"><figure id="table-1">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Summary of T<sub>m</sub> Measurements<a reveal-id="tbl1-fn1">a</a></div></div></div>
  <div id="gr1"><a><img src="{pixel}"
    alt="Graphic. Refer to the image caption for details."
    path-from-xml="article_0001.tif"></a></div>
  <div content-id="tbl1"></div>
  <div><div id="tbl1-fn1" content-id="tbl1-fn1"><span><span rel="nofollow">a</span></span>
    <p>Quadruplicate measurements.</p></div></div>
  <div><a href="/view-large/101" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div>
<div id="102-content"><figure id="table-2">
  <div id="tbl2"><span id="label-tbl2">Table 2.</span>
    <div id="caption-tbl2"><div>Summary of SPR Assays<sup>a</sup></div></div></div>
  <div id="gr2"><a><img src="{pixel}"
    alt="Graphic. Refer to the image caption for details."
    path-from-xml="article_0002.tif"></a></div>
  <div content-id="tbl2"></div>
  <div><div id="t2fn1" content-id="t2fn1"><span><span rel="nofollow">a</span></span>
    <p>K<sub>D</sub> (reverse)/K<sub>D</sub> (forward).</p></div></div>
  <div><a href="/view-large/102" target="_blank" rel="nofollow"
    aria-label="View large Table 2.">View Large</a></div>
</figure></div>
</section></main></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(
            [
                (
                    table.table_id,
                    table.source_kind,
                    table.title_plain,
                    table.footnotes_plain,
                )
                for table in result.tables
            ],
            [
                (
                    "table_001",
                    "image",
                    "Table 1. Summary of T_{m} Measurements^{a}",
                    ["[a] Quadruplicate measurements."],
                ),
                (
                    "table_002",
                    "image",
                    "Table 2. Summary of SPR Assays^{a}",
                    ["[a] K_{D} (reverse)/K_{D} (forward)."],
                ),
            ],
        )
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [
                ("table_001", "tables/table_001/source.png"),
                ("table_002", "tables/table_002/source.png"),
            ],
        )

        near_miss = self.extract(source.replace('content-id="tbl1"', 'content-id="other"'))
        self.assertEqual([table.table_id for table in near_miss.tables], ["table_002"])
        self.assertEqual(
            [figure.figure_id for figure in near_miss.figures],
            ["graphical_abstract"],
        )

    def test_acs_presentation_raster_table_is_not_a_graphical_abstract(self) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        source = f"""<!doctype html><html><body><main>
<h1>ACS presentation raster table export</h1><section><h2>Results</h2>
<div id="101-content"><figure content-id="tbl1 sec2.2" id="table-1">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div><em>T</em><sub>m</sub> values<a reveal-id="tbl1-fn1">a</a></div></div></div>
  <div><table role="presentation" aria-labelledby="label-tbl1" aria-describedby="caption-tbl1"><tbody><tr><td>
    <div id="gr5"><a><img src="{pixel}" alt="Graphic. Refer to the image caption for details."
      path-from-xml="article_0003.tif"></a></div>&#160;
  </td></tr></tbody></table></div>
  <div></div>
  <div><div id="tbl1-fn1" content-id="tbl1-fn1"><span><span rel="nofollow">a</span></span>
    <p>Reviewed measurements.</p></div></div>
  <div><a href="/view-large/101" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div></section></main></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].table_id, "table_001")
        self.assertEqual(result.tables[0].source_kind, "image")
        self.assertEqual(result.tables[0].footnotes_plain, ["[a] Reviewed measurements."])
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [("table_001", "tables/table_001/source.png")],
        )

        near_miss = self.extract(source.replace('role="presentation"', 'role="grid"'))
        self.assertEqual(near_miss.tables, [])
        self.assertEqual([item.figure_id for item in near_miss.figures], ["graphical_abstract"])

    def test_acs_source_prefixed_presentation_raster_table_is_structured(self) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        source = f"""<!doctype html><html><body><main>
<h1>ACS source-prefixed raster table export</h1><section><h2>Results</h2>
<div id="219322524-content"><figure content-id="ja064369lt00001 d7e293" id="table-1">
  <div id="ja064369lt00001"><span id="label-ja064369lt00001">Table 1.</span>
    <div id="caption-ja064369lt00001"><p>Binding constants (<em>K</em><sub>D</sub>)</p></div></div>
  <div><table role="presentation" aria-labelledby=" label-ja064369lt00001"
      aria-describedby=" caption-ja064369lt00001"><tbody><tr><td>
    <div id="ja064369l_0005.tif"><a><img src="{pixel}"
      alt="Graphic. Refer to the image caption for details."
      path-from-xml="ja064369l_0005.tif"></a></div>&#160;
  </td></tr></tbody></table></div>
  <div></div>
  <div><p><em><sup>a</sup></em> The number in parentheses is the standard error.</p></div>
  <div><a href="/view-large/219322524" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div></section></main></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].table_id, "table_001")
        self.assertEqual(result.tables[0].source_kind, "image")
        self.assertEqual(
            result.tables[0].title_plain,
            "Table 1. Binding constants (K_{D})",
        )
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["[a] The number in parentheses is the standard error."],
        )
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [("table_001", "tables/table_001/source.png")],
        )

        near_miss = self.extract(source.replace("d7e293", "section-3.5"))
        self.assertEqual(near_miss.tables, [])
        self.assertEqual(
            [item.figure_id for item in near_miss.figures],
            ["graphical_abstract"],
        )

        legacy_without_content_id = self.extract(
            source.replace(
                ' content-id="ja064369lt00001 d7e293"',
                "",
            )
        )
        self.assertEqual(legacy_without_content_id.figures, [])
        self.assertEqual(
            [item.table_id for item in legacy_without_content_id.tables],
            ["table_001"],
        )
        self.assertEqual(
            [asset.output_path for asset in legacy_without_content_id.embedded_assets],
            ["tables/table_001/source.png"],
        )

        source_without_notes = self.extract(
            source.replace(
                "  <div><p><em><sup>a</sup></em> The number in parentheses is the standard error.</p></div>\n",
                "",
            )
        )
        self.assertEqual(source_without_notes.figures, [])
        self.assertEqual(
            [item.table_id for item in source_without_notes.tables],
            ["table_001"],
        )
        self.assertEqual(source_without_notes.tables[0].footnotes_plain, [])
        self.assertEqual(
            [asset.output_path for asset in source_without_notes.embedded_assets],
            ["tables/table_001/source.png"],
        )

        multiple_source_notes = self.extract(
            source.replace(
                "<em><sup>a</sup></em> The number in parentheses is the standard error.",
                "<em><sup>a</sup></em> First note."
                "<em><sup>b</sup></em> Second note."
                "<em><sup>c</sup></em> Third note.",
            )
        )
        self.assertEqual(multiple_source_notes.figures, [])
        self.assertEqual(
            [item.table_id for item in multiple_source_notes.tables],
            ["table_001"],
        )

        legacy_near_miss = self.extract(
            source.replace(
                ' content-id="ja064369lt00001 d7e293"',
                "",
            ).replace(
                'aria-describedby=" caption-ja064369lt00001"',
                'aria-describedby=" caption-other"',
            )
        )
        self.assertEqual(legacy_near_miss.tables, [])
        self.assertEqual(
            [item.figure_id for item in legacy_near_miss.figures],
            ["graphical_abstract"],
        )

    def test_acs_omitted_formula_placeholder_preserves_surrounding_prose(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>Legacy ACS formula omission</h1>
<a href="https://doi.org/10.1021/ja000000a">https://doi.org/10.1021/ja000000a</a>
<h2 id="101">Experimental Section</h2><div id="101-content">
  <div><strong>Correlation Analysis. </strong>Before formula.
    <div content-id="ja000000ae00001"><div id="jumplink-ja000000ae00001">
      <div id="ja000000a_0008.gif"><a><span>[Image omitted: Formula. Refer to the image caption for details.]</span></a></div>
    </div></div>
    After formula.</div>
</div></article></body></html>"""

        document = lxml_html.fromstring(source)
        host = document.xpath('//div[@id="101-content"]/div')[0]
        containers = _display_equation_containers(host)
        self.assertEqual(len(containers), 1)
        segments = _segmented_inline_content(host, containers)
        self.assertEqual(
            [segment[0] for segment in segments],
            ["prose", "equation", "prose"],
        )
        self.assertEqual(segments[0][2], "Correlation Analysis. Before formula.")
        self.assertEqual(segments[2][2], "After formula.")

        near_miss = lxml_html.fromstring(
            source.replace("Refer to the image caption", "See the caption")
        )
        near_miss_host = near_miss.xpath('//div[@id="101-content"]/div')[0]
        self.assertEqual(_display_equation_containers(near_miss_host), [])

    def test_acs_unnumbered_raster_display_preserves_surrounding_prose(self) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        source = f"""<!doctype html><html><body><article>
<h1>Legacy ACS raster display</h1>
<a href="https://doi.org/10.1021/ja000000a">10.1021/ja000000a</a>
<h2>Discussion</h2><div id="123-content"><div>Before display.
<div reveal-group-id="[parent-legacy-section-id]"><span id="viewTranscriptId_"></span><div>
<a href="/view-large/figure/123/ja000000a_0007.tif" target="_blank" rel="nofollow" aria-describedby="sr-fig-viewer-action"><img src="{pixel}" alt="Figure. Refer to the image caption for details." path-from-xml="ja000000a_0007.tif"></a>
<div><a section="123" role="button" href="/view-large/figure/123/ja000000a_0007.tif" path-from-xml="ja000000a_0007.tif" target="_blank" rel="nofollow" aria-label="View large figure">View Large</a><a section="123" href="//example.invalid/download" role="button" path-from-xml="ja000000a_0007.tif" target="_blank" rel="nofollow" aria-label="Download slide for figure">Download to Slide</a></div>
</div></div>After display.</div></div></article></body></html>"""

        document = lxml_html.fromstring(source)
        host = document.xpath('//div[@id="123-content"]/div')[0]
        containers = _display_equation_containers(host)
        self.assertEqual(len(containers), 1)
        segments = _segmented_inline_content(host, containers)
        self.assertEqual(
            [(segment[0], segment[2]) for segment in segments],
            [
                ("prose", "Before display."),
                ("equation", ""),
                ("prose", "After display."),
            ],
        )

        near_miss = lxml_html.fromstring(source.replace("Figure. Refer", "Graphic. Refer"))
        near_miss_host = near_miss.xpath('//div[@id="123-content"]/div')[0]
        self.assertEqual(_display_equation_containers(near_miss_host), [])

    def test_attribute_light_acs_raster_title_runs_and_empty_table_controls(
        self,
    ) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        source = f"""<!doctype html><html><body><main>
<h1>Attribute-light ACS tables</h1>
<div id="101-content"><a id="101"></a><figure id="table-1">
  <img src="{pixel}" alt="Table 1">
  <figcaption><strong>Table 1.</strong> Stability of polyamides
    <strong>1</strong> and <strong>2</strong>
    <p><sup>a</sup> Measurements were replicated.</p>
  </figcaption>
</figure></div>
<div id="102-content"><a id="102"></a><div>
  <div id="tbl2"><span id="label-tbl2">Table 2.</span>
    <div id="caption-tbl2">Binding values</div></div>
  <div><table><thead><tr><th>Agent</th><th>Value</th></tr></thead>
    <tbody><tr><td>1</td><td>2.0</td></tr></tbody></table></div>
  <div></div>
</div></div>
</main></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(
            [
                (
                    table.table_id,
                    table.source_kind,
                    table.title_plain,
                    table.footnotes_plain,
                )
                for table in result.tables
            ],
            [
                (
                    "table_001",
                    "image",
                    "Table 1. Stability of polyamides 1 and 2",
                    ["[a] Measurements were replicated."],
                ),
                ("table_002", "html", "Table 2. Binding values", []),
            ],
        )
        self.assertEqual(
            result.tables[0].title_markdown,
            "Table 1. Stability of polyamides <strong>1</strong> and <strong>2</strong>",
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[1].parts[0].rows],
            [["Agent", "Value"], ["1", "2.0"]],
        )
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [("table_001", "tables/table_001/source.png")],
        )

    def test_attribute_light_acs_caption_tails_and_raster_table_without_note(
        self,
    ) -> None:
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        source = f"""<!doctype html><html><body><main>
<h1>Attribute-light ACS figures and tables</h1>
<div id="100-content"><a id="100"></a><figure id="figure-1">
  <img src="{pixel}" alt="Figure 1">
  <figcaption><strong>Figure 1.</strong> Complete authored β caption in PO<sub>4</sub>0.</figcaption>
</figure></div>
<div id="101-content"><a id="101"></a><figure id="table-1">
  <img src="{pixel}" alt="Table 1">
  <figcaption><strong>Table 1.</strong> Binding Affinities of Six-Ring PI Polyamides</figcaption>
</figure></div>
</main></body></html>"""

        result = self.extract(source)

        self.assertEqual(
            [
                (figure.figure_id, figure.label, figure.caption_plain)
                for figure in result.figures
            ],
            [("figure_001", "Figure 1", "Complete authored β caption in PO_{4}0.")],
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "Complete authored β caption in PO<sub>4</sub>0.",
        )
        self.assertEqual(
            [
                (
                    table.table_id,
                    table.source_kind,
                    table.title_plain,
                    table.footnotes_plain,
                )
                for table in result.tables
            ],
            [
                (
                    "table_001",
                    "image",
                    "Table 1. Binding Affinities of Six-Ring PI Polyamides",
                    [],
                )
            ],
        )
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [
                ("figure_001", "figures/main/figure_001.png"),
                ("table_001", "tables/table_001/source.png"),
            ],
        )

    def test_acs_incomplete_accessibility_table_stub_requires_override_rows(
        self,
    ) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS incomplete accessibility tables</h1><section><h2>Results</h2>
<div id="210151667-content"><figure content-id="tbl1 sec3.5" id="table-1">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>dsDNA Sequences<a reveal-id="tbl1-fn1">a</a></div></div>
  </div>
  <div id="GRAPHIC-d170e833-autogenerated"><a></a></div>
  <div content-id="tbl1"></div>
  <div><div id="tbl1-fn1" content-id="tbl1-fn1"><span><span rel="nofollow">a</span></span>
    <p>Mismatch positions are underlined.</p></div></div>
  <div><a href="/view-large/210151667" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div>
<div id="210151689-content"><figure content-id="tbl2 sec3.5" id="table-2">
  <div id="tbl2"><span id="label-tbl2">Table 2.</span>
    <div id="caption-tbl2"><div>UV–Vis Melting Temperatures</div></div>
  </div>
  <div id="GRAPHIC-d170e849-autogenerated"><a></a></div>
  <div content-id="tbl2"></div>
  <div><a href="/view-large/210151689" target="_blank" rel="nofollow"
    aria-label="View large Table 2.">View Large</a></div>
</figure></div>
</section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(
            [
                (
                    table.table_id,
                    table.source_id,
                    table.source_kind,
                    table.title_plain,
                    table.footnotes_plain,
                    [part.rows for part in table.parts],
                )
                for table in result.tables
            ],
            [
                (
                    "table_001",
                    "table-1",
                    "html",
                    "Table 1. dsDNA Sequences^{a}",
                    ["[a] Mismatch positions are underlined."],
                    [[]],
                ),
                (
                    "table_002",
                    "table-2",
                    "html",
                    "Table 2. UV–Vis Melting Temperatures",
                    [],
                    [[]],
                ),
            ],
        )
        self.assertEqual(result.embedded_assets, [])

        near_miss = self.extract(
            source.replace(
                'content-id="tbl1 sec3.5"',
                'content-id="tbl1 discussion"',
                1,
            )
        )
        self.assertEqual([table.table_id for table in near_miss.tables], ["table_002"])
        self.assertEqual(
            [figure.figure_id for figure in near_miss.figures],
            ["graphical_abstract"],
        )

    def test_attribute_light_acs_tables_and_display_mathml_are_structured(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<!doctype html><html><body><article id="acs-article-980556">
<header><h1>Attribute-light ACS article</h1>
<p>Journal of Exact Chemistry (2014) 136 (32): 11546–11554. Published July 18, 2014. <a href="https://doi.org/10.1021/ja506058e">https://doi.org/10.1021/ja506058e</a></p></header>
<h2 id="900">Results</h2><div id="900-content"><div>
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Semantic T<sub>m</sub> values<sup>a</sup></div></div>
  </div>
  <div><table><thead><tr><th>Agent</th><th>T<sub>m</sub></th></tr></thead>
    <tbody><tr><td>1</td><td>61.0 °C</td></tr></tbody></table></div>
  <div><div id="tbl1-fn1"><span><span>a</span></span>
    <p>Authored note.</p></div></div>
</div></div>
<h2 id="901">SPR</h2><div id="901-content">
  <figure id="ja506058e-table-2"><img src="{pixel}"
    alt="Table 2. Results of SPR measurement.">
    <figcaption id="caption-tbl2"><strong>Table 2.</strong>
      <div>Results of SPR Measurement</div></figcaption>
  </figure>
</div>
<h2 id="902">Methods</h2><div id="902-content"><div>Prose before.
  <div id="jumplink-ueq1"><div><mjx-container>
    <mjx-math aria-hidden="true">visual-only duplicate</mjx-math>
    <mjx-assistive-mml><math><mi>χ</mi><msup><mi></mi><mn>2</mn></msup>
      <mo>=</mo><mfrac><mi>x</mi><mi>n</mi></mfrac></math></mjx-assistive-mml>
  </mjx-container></div></div>
  Prose after.</div></div>
</article></body></html>"""
        )

        self.assertEqual(result.figures, [])
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "136",
                "issue": "32",
                "pages": "11546–11554",
                "date": "July 18, 2014",
                "first_published": "July 18, 2014",
            },
        )
        self.assertEqual(
            [
                (table.table_id, table.source_kind, table.title_plain)
                for table in result.tables
            ],
            [
                ("table_001", "html", "Table 1. Semantic T_{m} values^{a}"),
                ("table_002", "image", "Table 2. Results of SPR Measurement"),
            ],
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Agent", "T_{m}"], ["1", "61.0 °C"]],
        )
        self.assertEqual(result.tables[0].footnotes_plain, ["[a] Authored note."])
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [("table_002", "tables/table_002/source.png")],
        )
        blocks = result.sections[2].blocks
        self.assertEqual(
            [(block.kind, block.plain_text) for block in blocks],
            [
                ("paragraph", "Prose before."),
                ("equation", "χ^{2} = (x)/(n)"),
                ("paragraph", "Prose after."),
            ],
        )

    def test_bold_lead_div_preserves_prose_and_consecutive_display_equations(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley mixed equation div</h1><section><h2>Experimental Section</h2>
<div><strong>General</strong>: Authored method prose.
  <div><span id="ufd9001">ε = 9900 × (sum number of Py and Im)</span></div>
  <div><span id="ufd9002">Abs = εcl</span></div>
</div><p>Following method.</p></section></article></body></html>"""
        )

        blocks = result.sections[0].blocks
        self.assertEqual(
            [(block.kind, block.plain_text) for block in blocks],
            [
                ("paragraph", "General: Authored method prose."),
                ("equation", "ε = 9900 × (sum number of Py and Im)"),
                ("equation", "Abs = εcl"),
                ("paragraph", "Following method."),
            ],
        )
        self.assertEqual(
            blocks[0].markdown,
            "<strong>General</strong>: Authored method prose.",
        )

    def test_numeric_acs_tables_with_stripped_controls_are_structured(self) -> None:
        pixel = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        source = f"""<!doctype html><html><body><article>
<h1>Numeric ACS table snapshot</h1><section><h2>Results</h2>
<div id="100-content"><a id="100"></a><div>
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Measured values<sup>a</sup></div></div></div>
  <div><table><thead><tr><th>Agent</th><th>Value</th></tr></thead>
    <tbody><tr><td>1</td><td>2.0</td></tr></tbody></table></div>
  <div><div id="t1fn1"><span><span>a</span></span><p>Authored note.</p></div></div>
  <div></div>
</div></div>
<div id="101-content"><a id="101"></a><figure id="table-2">
  <img src="{pixel}" alt="Table 2">
  <figcaption><strong>Table 2.</strong> Image-only values
    <p><sup>a</sup> Authored image-table note.</p></figcaption>
</figure></div>
</section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(
            [(table.table_id, table.source_kind) for table in result.tables],
            [("table_001", "html"), ("table_002", "image")],
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Agent", "Value"], ["1", "2.0"]],
        )
        self.assertEqual(result.tables[0].footnotes_plain, ["[a] Authored note."])
        self.assertEqual(
            result.tables[1].footnotes_plain,
            ["[a] Authored image-table note."],
        )
        self.assertEqual(
            result.tables[0].title_markdown,
            "Table 1. Measured values<sup>a</sup>",
        )

        identified_wrapper = self.extract(
            source.replace(
                '<div id="100-content"><a id="100"></a><div>',
                '<div id="100-content"><a id="100"></a><div id="table-1">',
                1,
            )
        )
        self.assertEqual(
            [(table.table_id, table.source_kind) for table in identified_wrapper.tables],
            [("table_001", "html"), ("table_002", "image")],
        )
        self.assertEqual(
            identified_wrapper.tables[0].footnotes_plain,
            ["[a] Authored note."],
        )

        flattened_marker = self.extract(
            source.replace("Measured values<sup>a</sup>", "Measured valuesa", 1)
        )
        self.assertEqual(
            flattened_marker.tables[0].title_markdown,
            "Table 1. Measured values<sup>a</sup>",
        )
        self.assertEqual(
            flattened_marker.tables[0].title_plain,
            "Table 1. Measured values^{a}",
        )
        self.assertEqual(
            [(asset.asset_id, asset.output_path) for asset in result.embedded_assets],
            [("table_002", "tables/table_002/source.png")],
        )

        near_miss = self.extract(source.replace('<a id="101"></a>', "", 1))
        self.assertEqual(
            [table.table_id for table in near_miss.tables],
            ["table_001"],
        )

    def test_current_acs_source_prefixed_table_with_unmarked_note_is_structured(
        self,
    ) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS source-prefixed table</h1><section><h2>Results</h2><p>Lead prose.</p>
<div id="221-content"><a id="221" scrollto-destination="221"></a>
<div content-id="ja075247bt00001 d7e241">
  <div id="ja075247bt00001"><span id="label-ja075247bt00001">Table 1.</span>
    <div id="caption-ja075247bt00001"><p>Measured values<sup>a</sup></p></div></div>
  <div><table role="presentation" aria-labelledby=" label-ja075247bt00001"
    aria-describedby=" caption-ja075247bt00001"><tbody>
    <tr><td>Agent</td><td>Value<sup>b</sup></td></tr>
    <tr><td>1</td><td>2.0</td></tr></tbody></table></div>
  <div></div>
  <div><p><em>a</em> First note. <em>b</em> Second note.</p></div>
  <div><a href="/view-large/221" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div><p>Trailing prose.</p></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_id, "ja075247bt00001")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Agent", "Value^{b}"], ["1", "2.0"]],
        )
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["a First note. b Second note."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose.", "Trailing prose."],
        )

        near_miss = self.extract(
            source.replace('scrollto-destination="221"', 'scrollto-destination="222"')
        )
        self.assertEqual(near_miss.tables, [])

    def test_current_acs_source_prefixed_table_without_note_is_structured(
        self,
    ) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS source-prefixed table without note</h1><section><h2>Results</h2>
<p>Lead prose.</p><div id="221-content">
<a id="221" scrollto-destination="221"></a>
<div content-id="bi701053at00001 d7e507">
  <div id="bi701053at00001"><span id="label-bi701053at00001">Table 1.</span>
    <div id="caption-bi701053at00001"><p>Measured values</p></div></div>
  <div><table role="table" aria-labelledby=" label-bi701053at00001"
    aria-describedby=" caption-bi701053at00001"><tbody>
    <tr><td></td><td colspan="2">Measured</td></tr>
    <tr><td>Agent</td><td>Value</td><td>Error</td></tr>
    <tr><td>1</td><td>2.0</td><td>0.1</td></tr></tbody></table></div>
  <div></div>
  <div><a href="/view-large/221" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div><p>Trailing prose.</p></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_id, "bi701053at00001")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["", "Measured"], ["Agent", "Value", "Error"], ["1", "2.0", "0.1"]],
        )
        self.assertEqual(
            [[cell.header for cell in row] for row in result.tables[0].parts[0].rows],
            [[True, True], [True, True, True], [False, False, False]],
        )
        self.assertEqual(result.tables[0].footnotes_plain, [])
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose.", "Trailing prose."],
        )

        near_miss = self.extract(
            source.replace('aria-describedby=" caption-bi701053at00001"', '')
        )
        self.assertEqual(near_miss.tables, [])

    def test_current_acs_fragmented_group_header_table_is_structured(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS fragmented semantic table</h1><section><h2>Results</h2>
<p>Lead prose.</p><div id="221-content">
<a id="221" scrollto-destination="221"></a>
<div content-id="bi047872ot00001 d7e378">
  <div id="bi047872ot00001"><span id="label-bi047872ot00001">Table 1.</span>
    <div id="caption-bi047872ot00001"><p>Binding affinity</p></div></div>
  <div><table role="table" aria-labelledby=" label-bi047872ot00001"
    aria-describedby=" caption-bi047872ot00001"><tbody>
    <tr><td></td><td colspan="2">Measured affinity</td></tr></tbody></table></div>
  <div></div>
  <div><table role="presentation" aria-labelledby=" label-bi047872ot00001"
    aria-describedby=" caption-bi047872ot00001"><tbody>
    <tr><td>Agent</td><td>K<sub>d</sub></td><td>K<sub>a</sub></td>
      <td>Delta G</td></tr>
    <tr><td>none</td><td>7 +/- 1</td><td>1.4 x 10<sup>8</sup></td>
      <td>-11.04</td></tr></tbody></table></div>
  <div></div>
  <div><p><em>a</em> Determined by the authored assay.</p></div>
  <div><a href="/view-large/221" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div><p>Trailing prose.</p></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "bi047872ot00001")
        self.assertEqual(table.title_plain, "Table 1. Binding affinity")
        self.assertEqual(len(table.parts), 1)
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [
                ["", "Measured affinity"],
                ["Agent", "K_{d}", "K_{a}", "Delta G"],
                ["none", "7 +/- 1", "1.4 x 10^{8}", "-11.04"],
            ],
        )
        self.assertEqual(
            [[cell.header for cell in row] for row in table.parts[0].rows],
            [[True, True], [True, True, True, True], [False, False, False, False]],
        )
        self.assertEqual(
            table.footnotes_plain,
            ["a Determined by the authored assay."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose.", "Trailing prose."],
        )

        near_miss = self.extract(
            source.replace('role="presentation"', 'role="grid"', 1)
        )
        self.assertEqual(near_miss.tables, [])

    def test_current_acs_split_accessibility_table_is_two_structured_parts(
        self,
    ) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS split semantic table</h1><section><h2>Results</h2><p>Lead.</p>
<div id="221-content"><a id="221" scrollto-destination="221"></a>
<div content-id="bi061245ct00001 d7e415">
  <div id="bi061245ct00001"><span id="label-bi061245ct00001">Table 1.</span></div>
  <div><table role="table" aria-labelledby="label-bi061245ct00001"><tbody>
    <tr><td colspan="3">(A) First experiment</td></tr></tbody></table></div><div></div>
  <div><table role="table" aria-labelledby="label-bi061245ct00001"><tbody>
    <tr><th></th><th colspan="2">Measured</th></tr></tbody></table></div><div></div>
  <div><table role="table" aria-labelledby="label-bi061245ct00001"><tbody>
    <tr><th>Agent</th><th>Value</th><th>Error</th></tr>
    <tr><td>1</td><td>2.0</td><td>0.1</td></tr></tbody></table></div><div></div>
  <div><table role="presentation" aria-labelledby="label-bi061245ct00001"><tbody>
    <tr><td>(B) Second experiment</td></tr></tbody></table></div><div></div>
  <div><table role="table" aria-labelledby="label-bi061245ct00001"><tbody>
    <tr><th>Agent</th><th>Value</th></tr>
    <tr><td>2</td><td>3.0</td></tr></tbody></table></div><div></div>
  <div><p><em>a</em> Reviewed note.</p></div>
  <div><a href="/view-large/221" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div><p>Trailing.</p></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "bi061245ct00001")
        self.assertEqual(table.title_plain, "Table 1.")
        self.assertEqual(len(table.parts), 2)
        self.assertEqual(table.parts[0].rows[0][0].text, "(A) First experiment")
        self.assertEqual(table.parts[0].rows[0][0].colspan, 3)
        self.assertTrue(table.parts[0].rows[0][0].header)
        self.assertEqual(table.parts[1].rows[0][0].text, "(B) Second experiment")
        self.assertEqual(table.parts[1].rows[0][0].colspan, 2)
        self.assertEqual(table.footnotes_plain, ["a Reviewed note."])
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead.", "Trailing."],
        )

        near_miss = self.extract(
            source.replace('role="presentation"', 'role="table"', 1)
        )
        self.assertEqual(near_miss.tables, [])

    def test_current_acs_section_fragment_table_is_one_structured_part(
        self,
    ) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS section-fragment table</h1><section><h2>Results</h2><p>Lead.</p>
<div id="221-content"><a id="221" scrollto-destination="221"></a>
<div content-id="ja0373622t00001 d7e318">
  <div id="ja0373622t00001"><span id="label-ja0373622t00001">Table 1.</span>
    <div id="caption-ja0373622t00001"><p>Energy and rmsd</p></div></div>
  <div><table role="presentation" aria-labelledby="label-ja0373622t00001"
    aria-describedby="caption-ja0373622t00001"><tbody>
    <tr><td>Molecular Mechanics Energy (kcal)</td></tr></tbody></table></div><div></div>
  <div><table role="presentation" aria-labelledby="label-ja0373622t00001"
    aria-describedby="caption-ja0373622t00001"><tbody>
    <tr><td></td><td></td></tr><tr><td>EAmber</td><td>-6224.6 +/- 6.8</td></tr>
    <tr><td>Eviol</td><td>17.4 +/- 1.8</td></tr></tbody></table></div><div></div>
  <div><table role="presentation" aria-labelledby="label-ja0373622t00001"
    aria-describedby="caption-ja0373622t00001"><tbody>
    <tr><td>Average Pairwise rmsd (A)</td></tr></tbody></table></div><div></div>
  <div><table role="presentation" aria-labelledby="label-ja0373622t00001"
    aria-describedby="caption-ja0373622t00001"><tbody>
    <tr><td></td><td></td></tr><tr><td>DNA</td><td>1.99</td></tr>
    <tr><td>ligand + DNA</td><td>1.92</td></tr></tbody></table></div><div></div>
  <div><a href="/view-large/221" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div><p>Trailing.</p></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "ja0373622t00001")
        self.assertEqual(table.title_plain, "Table 1. Energy and rmsd")
        self.assertEqual(len(table.parts), 1)
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [
                ["Molecular Mechanics Energy (kcal)"],
                ["EAmber", "-6224.6 +/- 6.8"],
                ["Eviol", "17.4 +/- 1.8"],
                ["Average Pairwise rmsd (A)"],
                ["DNA", "1.99"],
                ["ligand + DNA", "1.92"],
            ],
        )
        self.assertEqual(table.parts[0].rows[0][0].colspan, 2)
        self.assertTrue(table.parts[0].rows[0][0].header)
        self.assertEqual(table.parts[0].rows[3][0].colspan, 2)
        self.assertTrue(table.parts[0].rows[3][0].header)
        self.assertFalse(table.parts[0].rows[1][0].header)
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead.", "Trailing."],
        )

        near_miss = self.extract(source.replace(
            '<tr><td></td><td></td></tr>',
            '<tr><td>unexpected</td><td></td></tr>',
            1,
        ))
        self.assertEqual(near_miss.tables, [])

    def test_direct_text_div_preserves_prose_and_consecutive_display_equations(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley direct-text equation div</h1><section><h2>Experimental Section</h2>
<div>Authored method prose before the formulas.
  <div id="disp-0001"><span id="ufd9001">ε = 9900 × (sum number of Py and Im)</span></div>
  <div id="disp-0002"><span id="ufd9002">Abs = εcl</span></div>
</div><p>Following method.</p></section></article></body></html>"""
        )

        self.assertEqual(
            [(block.kind, block.plain_text) for block in result.sections[0].blocks],
            [
                ("paragraph", "Authored method prose before the formulas."),
                ("equation", "ε = 9900 × (sum number of Py and Im)"),
                ("equation", "Abs = εcl"),
                ("paragraph", "Following method."),
            ],
        )

    def test_legacy_acs_gif_equations_preserve_surrounding_prose(self) -> None:
        pixel = (
            "data:image/gif;base64,"
            "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        )
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>Legacy ACS formula snapshot</h1>
<a href="https://doi.org/10.1021/jm000001a">10.1021/jm000001a</a>
<h2 id="100">Methods</h2><div id="100-content"><div>
Before formula.<div><div id="jumplink-ueq1"><div id="jm-2011-00001a_m001.gif">
<a><img src="{pixel}" alt="Displayed formula"></a></div></div></div>
Between formulas.<div><div id="jumplink-ueq2"><div id="jm-2011-00001a_m002.gif">
<a><img src="{pixel}" alt="Displayed formula"></a></div></div></div>
After formula.</div></div></article></body></html>"""
        )

        blocks = result.sections[0].blocks
        self.assertEqual(
            [(block.kind, block.plain_text) for block in blocks],
            [
                ("paragraph", "Before formula."),
                ("paragraph", "Between formulas."),
                ("paragraph", "After formula."),
            ],
        )

    def test_numeric_heading_content_sibling_scopes_nested_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>ACS article</h1><main>
<h2 id="100">Abstract</h2><div><div id="100-content">
  <section><p>Authored abstract text.</p></section></div></div>
<h2 id="101">Introduction</h2><div id="101-content">
  <p>Authored introduction text.</p></div>
</main></article></body></html>"""
        )

        self.assertEqual(
            [
                (
                    section.section_id,
                    section.heading,
                    [block.plain_text for block in section.blocks],
                )
                for section in result.sections
            ],
            [
                ("section-abstract", "Abstract", ["Authored abstract text."]),
                (
                    "section-introduction",
                    "Introduction",
                    ["Authored introduction text."],
                ),
            ],
        )

    def test_modern_acs_display_mathml_keeps_aligned_rows_as_one_equation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div id="article-record">
<h1>Modern ACS equation dialect</h1>
<h2 id="201">Experimental Section</h2><div id="201-content">
  <p>Relationship before equation.</p>
  <div id="jumplink-ueq1"><span></span><div>
    <mjx-container display="true">
      <mjx-math aria-hidden="true">visual-only duplicate</mjx-math>
      <mjx-assistive-mml display="block"><math display="block">
        <mtable>
          <mtr><mtd><mrow><mi>ε</mi><mo>=</mo><mn>9900</mn><mo>×</mo>
            <mrow><mo>(</mo><mi>sum number of Py and Im</mi><mo>)</mo></mrow>
          </mrow></mtd></mtr>
          <mtr><mtd><mrow><mi>Abs</mi><mo>=</mo><mi>ε</mi><mi>cl</mi></mrow></mtd></mtr>
        </mtable>
      </math></mjx-assistive-mml>
    </mjx-container>
  </div></div>
  <div id="jumplink-ueqX"><math display="block"><mi>near miss</mi></math></div>
  <div id="jumplink-ueq2"><math display="inline"><mi>inline near miss</mi></math></div>
</div></div></body></html>"""
        )

        equations = [
            block
            for section in result.sections
            for block in section.blocks
            if block.kind == "equation"
        ]
        self.assertEqual(len(equations), 1)
        self.assertEqual(
            equations[0].plain_text,
            "ε = 9900 × (sum number of Py and Im)\nAbs = εcl",
        )
        self.assertEqual(
            equations[0].markdown,
            "ε = 9900 × (sum number of Py and Im)  \nAbs = εcl",
        )

    def test_current_acs_uneq_display_mathml_keeps_mixed_prose(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div id="article-record">
<h1>Current ACS unnumbered equation</h1>
<h2 id="201">Experimental Section</h2><div id="201-content">
<div>Prose before.<div content-id="uneq1"><div id="jumplink-uneq1"><div>
<mjx-container display="true"><mjx-math aria-hidden="true">visual duplicate</mjx-math>
<mjx-assistive-mml display="block"><math display="block">
<mtable><mtr><mtd><mi>ε</mi><mo>=</mo><mn>9900</mn></mtd></mtr>
<mtr><mtd><mi>Abs</mi><mo>=</mo><mi>ε</mi><mi>cl</mi></mtd></mtr></mtable>
</math></mjx-assistive-mml></mjx-container></div></div></div>Prose after.</div>
</div></div></body></html>"""
        )

        self.assertEqual(
            [(block.kind, block.plain_text) for block in result.sections[0].blocks],
            [
                ("paragraph", "Prose before."),
                ("equation", "ε = 9900\nAbs = εcl"),
                ("paragraph", "Prose after."),
            ],
        )

    def test_current_acs_numbered_display_mathml_preserves_equation_label(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div id="article-record">
<h1>Current ACS numbered equation</h1>
<h2 id="201">Results</h2><div id="201-content">
<div content-id="eq1"><div id="jumplink-eq1"><span></span><div>
<mjx-container display="true"><mjx-math aria-hidden="true">visual duplicate</mjx-math>
<mjx-assistive-mml display="block"><math display="block">
<mi>r</mi><mo>=</mo><mfrac><mi>x</mi><mi>σ</mi></mfrac>
</math></mjx-assistive-mml></mjx-container></div></div><span>(1)</span></div>
</div></div></body></html>"""
        )

        equations = [
            block
            for section in result.sections
            for block in section.blocks
            if block.kind == "equation"
        ]
        self.assertEqual(len(equations), 1)
        self.assertEqual(equations[0].plain_text, "r = (x)/(σ) (1)")
        self.assertEqual(
            [
                (block.kind, block.plain_text)
                for section in result.sections
                for block in section.blocks
            ],
            [("equation", "r = (x)/(σ) (1)")],
        )

    def test_oup_blank_jumplink_display_mathml_is_equation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>OUP equation</h1>
<h2>Methods</h2><p>Relationship before equation.</p>
<div content-id=""><div id="jumplink-"><div>
<mjx-container display="true"><mjx-math aria-hidden="true">visual duplicate</mjx-math>
<mjx-assistive-mml display="block"><math display="block">
<mi>Z</mi><mo>=</mo><mfrac><mi>x</mi><mi>σ</mi></mfrac>
</math></mjx-assistive-mml></mjx-container>
</div></div></div><p>Relationship after equation.</p>
</article></body></html>"""
        )

        self.assertEqual(
            [(block.kind, block.plain_text) for block in result.sections[0].blocks],
            [
                ("paragraph", "Relationship before equation."),
                ("equation", "Z = (x)/(σ)"),
                ("paragraph", "Relationship after equation."),
            ],
        )

    def test_oup_blank_jumplink_inside_mixed_prose_is_not_duplicated(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>OUP mixed equation</h1>
<h2>Methods</h2><div>Relationship before equation.
<div content-id=""><div id="jumplink-"><div>
<mjx-container display="true"><mjx-math aria-hidden="true">visual duplicate</mjx-math>
<mjx-assistive-mml display="block"><math display="block">
<mi>Z</mi><mo>=</mo><mfrac><mi>x</mi><mi>σ</mi></mfrac>
</math></mjx-assistive-mml></mjx-container>
</div></div></div>Relationship after equation.</div>
</article></body></html>"""
        )

        self.assertEqual(
            [(block.kind, block.plain_text) for block in result.sections[0].blocks],
            [
                ("paragraph", "Relationship before equation."),
                ("equation", "Z = (x)/(σ)"),
                ("paragraph", "Relationship after equation."),
            ],
        )

    def test_anonymous_acs_body_fragment_requires_complete_exact_signature(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        source = f"""<body>
<header><h1>Anonymous ACS body fragment</h1>
  <p>Alpha Author; Beta Author</p><p>Example Institute</p></header>
<div id="100-content"><div><h2>Visual Abstract</h2><section>
  <p></p><div id="tgr1"><img src="{pixel}"
    alt="Graphic. Refer to the image caption for details."></div><p></p>
</section></div></div>
<h2 id="200">Abstract</h2><div><div id="200-content"><section>
  <p>Authored summary.</p></section></div></div>
<h2 id="201">Introduction</h2><div>
  <div id="202-content"><p>Opening DNA-
binding prose.</p></div>
  <div id="300-content"><div reveal-group-id="sec1">
    <span id="viewTranscriptId_"></span><div>Figure 1</div>
    <div><a href="/large-figure" rel="nofollow"><img src="{pixel}"
      alt="Figure 1. Refer to the image caption for details."></a>
      <div><a href="/large-figure" rel="nofollow">View Large</a></div></div>
    <div><div>Figure 1.</div><div><p>Authored β caption.</p></div></div>
  </div></div>
  <div id="400-content"><div content-id="tbl1 sec2.1">
    <div id="tbl1"><span id="label-tbl1">Table 1.</span>
      <div id="caption-tbl1"><div>Raster measurements</div></div></div>
    <div><table><tbody><tr><td><div id="gr5"><img src="{pixel}"
      alt="Graphic. Refer to the image caption for details."></div></td></tr></tbody></table></div>
    <div><div id="tbl1-fn1" content-id="tbl1-fn1"><span>a</span>
      <p>Authored note.</p></div></div>
    <div><a href="/large-table" rel="nofollow">View Large</a></div>
  </div></div>
</div>
<h2 id="500">References</h2><div><div>
  <div><span>1.</span><div>Alpha A. <em>Journal</em> 2008.</div></div>
</div></div>
</body>"""

        result = self.extract(source)

        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Abstract", ["Authored summary."]),
                ("Introduction", ["Opening DNA-binding prose."]),
            ],
        )
        self.assertEqual(
            [(figure.figure_id, figure.caption_plain) for figure in result.figures],
            [
                ("graphical_abstract", ""),
                ("figure_001", "Authored β caption."),
            ],
        )
        self.assertEqual(
            [(table.table_id, table.source_kind) for table in result.tables],
            [("table_001", "image")],
        )
        self.assertEqual(result.references[0].plain_text, "1. Alpha A. Journal 2008.")

        near_miss = self.extract(
            source.replace('content-id="tbl1 sec2.1"', 'content-id="tbl1 unrelated"')
        )
        self.assertEqual(near_miss.figures, [])
        self.assertEqual(near_miss.tables, [])

    def test_acs_scheme_div_only_caption_body_is_preserved(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>ACS div-only Scheme caption</h1><section><h2>Results</h2>
<figure id="scheme-1"><span id="viewTranscriptId_"></span><div>Scheme 1</div>
  <div><a href="/large-scheme" rel="nofollow"><img src="{pixel}"
    alt="Scheme 1. Refer to the image caption for details."></a>
    <div><a href="/large-scheme" rel="nofollow">View Large</a></div></div>
  <div><div>Scheme 1.</div><div>
    <div>Synthesis of the Mtt-Protected Pyrrole and Imidazole Monomers</div>
  </div></div>
</figure></section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "scheme_001")
        self.assertEqual(result.figures[0].label, "Scheme 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Synthesis of the Mtt-Protected Pyrrole and Imidazole Monomers",
        )

        control_near_miss = self.extract(
            f"""<!doctype html><html><body><article>
<h1>ACS div-only Scheme caption near miss</h1><section><h2>Results</h2>
<figure id="scheme-1"><div>Scheme 1</div><div><img src="{pixel}"
  alt="Scheme 1. Refer to the image caption for details."></div>
<div><div>Scheme 1.</div><div><div><a href="/viewer">View Large</a></div></div></div>
</figure></section></article></body></html>"""
        )
        self.assertEqual(control_near_miss.figures[0].caption_plain, "")

    def test_acs_scheme_div_only_caption_keeps_unlinked_exact_footnote(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>ACS div-only Scheme note</h1><section><h2>Results</h2>
<figure id="scheme-1"><span id="viewTranscriptId_"></span><div>Scheme 1</div>
  <div><a href="/large-scheme" rel="nofollow"><img src="{pixel}"
    alt="Scheme 1. Refer to the image caption for details."></a></div>
  <div><div>Scheme 1.</div><div><div>Synthetic route to 16S.</div></div></div>
  <div id="sch1-fn1" content-id="sch1-fn1"><span><span rel="nofollow">*</span></span>
    <p>SPPS = solid-phase peptide synthesis.</p></div>
</figure></section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Scheme 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Synthetic route to 16S. * SPPS = solid-phase peptide synthesis.",
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "Synthetic route to 16S.\n\n\\* SPPS = solid-phase peptide synthesis.",
        )

    def test_acs_sibling_semantic_table_binds_title_notes_and_rows(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS semantic table</h1><section><h2>Results</h2><p>Lead prose.</p>
<div id="900-content"><div>
  <div id="tbl2"><span id="label-tbl2">Table 2.</span>
    <div id="caption-tbl2"><div><em>T</em><sub>m</sub> values<span>a</span></div></div>
  </div>
  <div><table role="table" aria-labelledby=" label-tbl2">
    <thead><tr><th rowspan="2">Agent</th><th colspan="2">Result</th></tr>
      <tr><th><em>K</em><sub>D</sub></th><th>Δ<em>T</em><sub>m</sub></th></tr></thead>
    <tbody><tr><td><strong>1</strong></td><td>8 (±0.2)<span>b</span> × 10<sup>−9</sup></td>
      <td>2.7 ± 0.1</td></tr></tbody>
  </table></div>
  <div><div id="t2fn1"><span><span>a</span></span>
    <p>Values are means; <em>n</em> = 3.</p></div>
    <div id="t2fn2"><span><span>b</span></span>
    <p>Standard deviation is given in parentheses.</p></div></div>
</div></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_002")
        self.assertEqual(table.source_id, "tbl2")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.title_plain, "Table 2. T_{m} values^{a}")
        self.assertEqual(
            table.title_markdown,
            "Table 2. <em>T</em><sub>m</sub> values<sup>a</sup>",
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [
                ["Agent", "Result"],
                ["K_{D}", "ΔT_{m}"],
                ["1", "8 (±0.2)^{b} × 10^{−9}", "2.7 ± 0.1"],
            ],
        )
        self.assertEqual(
            table.parts[0].rows[2][1].markdown,
            "8 (±0.2)<sup>b</sup> × 10<sup>−9</sup>",
        )
        self.assertEqual(table.parts[0].rows[0][0].rowspan, 2)
        self.assertEqual(table.parts[0].rows[0][1].colspan, 2)
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] Values are means; n = 3.",
                "[b] Standard deviation is given in parentheses.",
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose."],
        )

        near_miss = self.extract(
            source.replace(
                'aria-labelledby=" label-tbl2"',
                'aria-labelledby=" label-other"',
            )
        )
        self.assertEqual(near_miss.tables, [])

        reordered_note_ids = source.replace('id="t2fn1"', 'id="t2fnX"').replace(
            'id="t2fn2"', 'id="t2fn1"'
        ).replace('id="t2fnX"', 'id="t2fn2"')
        self.assertEqual(len(self.extract(reordered_note_ids).tables), 1)

    def test_acs_semantic_table_uses_targeted_note_label(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>ACS note target</h1>
<section><h2>Results</h2><div id="900-content"><div content-id="tbl1 sec1.1">
<div id="tbl1"><span id="label-tbl1">Table 1.</span>
<div id="caption-tbl1"><div>Measurements</div></div></div>
<div><table role="table" aria-labelledby=" label-tbl1" aria-describedby=" caption-tbl1">
<thead><tr><th>Dose<a reveal-id="t1fn2">a</a></th></tr></thead>
<tbody><tr><td>5</td></tr></tbody></table></div><div></div>
<div><div id="t1fn1"><span><span>a</span></span><p>First note.</p></div>
<div id="t1fn2"><span><span>b</span></span><p>Targeted note.</p></div></div>
<div><a href="/view-large/900" target="_blank" rel="nofollow"
aria-label="View large Table 1.">View Large</a></div>
</div></div></section></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].parts[0].rows[0][0].text, "Dose^{b}")

    def test_acs_accessible_table_ignores_exact_hidden_duplicate(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS accessible table</h1><section><h2>Results</h2><p>Lead prose.</p>
<div id="185067599-content"><div content-id="tbl1 ">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Tested conditions<a reveal-id="t1fn1">a</a></div></div>
  </div>
  <div><table role="table" aria-labelledby=" label-tbl1"
      aria-describedby=" caption-tbl1"><thead><tr><th></th><th>Yield (%)</th></tr></thead>
    <tbody><tr><td>A</td><td>&lt;5</td></tr><tr><td>B</td><td>75</td></tr></tbody>
  </table></div>
  <div><table aria-hidden="true"><thead><tr><th></th><th>Yield (%)</th></tr></thead>
    <tbody><tr><td>A</td><td>&lt;5</td></tr><tr><td>B</td><td>75</td></tr></tbody>
  </table></div>
  <div><div id="t1fn1" content-id="t1fn1"><span><span rel="nofollow">a</span></span>
    <p>Authored reaction conditions.</p></div></div>
  <div><a href="/view-large/185067599" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_001")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.title_plain, "Table 1. Tested conditions^{a}")
        self.assertEqual(
            table.title_markdown,
            "Table 1. Tested conditions<sup>a</sup>",
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["", "Yield (%)"], ["A", "<5"], ["B", "75"]],
        )
        self.assertEqual(
            table.footnotes_plain,
            ["[a] Authored reaction conditions."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose."],
        )

        near_miss = self.extract(
            source.replace("<td>B</td><td>75</td></tr></tbody>\n  </table></div>",
                           "<td>B</td><td>74</td></tr></tbody>\n  </table></div>", 1)
        )
        self.assertEqual(near_miss.tables, [])

    def test_acs_accessible_table_accepts_hidden_jats_break_duplicate(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS accessible line-break table</h1><section><h2>Results</h2>
<div id="185067599-content"><div content-id="tbl1 ">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Tested conditions</div></div>
  </div>
  <div><table role="table" aria-labelledby=" label-tbl1"
      aria-describedby=" caption-tbl1"><thead><tr><th>Entry</th>
      <th>Yield<br/>(%)</th></tr></thead>
    <tbody><tr><td>A</td><td>75</td></tr></tbody>
  </table></div>
  <div><table aria-hidden="true"><thead><tr><th>Entry</th>
      <th>Yield<break/>(%)</th></tr></thead>
    <tbody><tr><td>A</td><td>75</td></tr></tbody>
  </table></div>
  <div><a href="/view-large/185067599" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</div></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].table_id, "table_001")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Entry", "Yield\n(%)"], ["A", "75"]],
        )

    def test_acs_hybrid_semantic_figure_table_preserves_context_graphic(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS hybrid semantic table</h1><section><h2>Results</h2><p>Lead prose.</p>
<div id="208689229-content"><figure content-id="tbl1 " id="table-1">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Optimized conditions<a reveal-id="t1fn1">a</a></div></div>
  </div>
  <div id="fx1"><a><img alt="Graphic. Refer to the image caption for details." src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="/></a></div>
  <div content-id="tbl1"></div>
  <div><table role="table" aria-labelledby=" label-tbl1" aria-describedby=" caption-tbl1">
    <thead><tr><th>Entry</th><th>Time<a reveal-id="t1fn1">a</a> (min)</th></tr></thead>
    <tbody><tr><td>1</td><td>50&#x200b;</td></tr></tbody></table></div>
  <div><table aria-hidden="true"><thead><tr><th>Entry</th><th>Time<a reveal-id="t1fn1">a</a> (min)</th></tr></thead>
    <tbody><tr><td>1</td><td>50&#x200b;</td></tr></tbody></table></div>
  <div><div id="t1fn1" content-id="t1fn1"><span><span rel="nofollow">a</span></span>
    <p>Authored reaction conditions.</p></div></div>
  <div><a href="/view-large/208689229" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_001")
        self.assertEqual(table.source_id, "table-1")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.title_plain, "Table 1. Optimized conditions^{a}")
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Entry", "Time^{a} (min)"], ["1", "50"]],
        )
        self.assertEqual(
            table.parts[0].rows[0][1].markdown,
            "Time<sup>a</sup> (min)",
        )
        self.assertEqual(
            table.footnotes_plain, ["[a] Authored reaction conditions."]
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["table_001_context_001"],
        )
        self.assertEqual(result.embedded_assets[0].parent_table_id, "table_001")
        self.assertEqual(
            result.embedded_assets[0].output_path,
            "tables/main/table_001_cells/context_001.png",
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose."],
        )

        near_miss = self.extract(source.replace("<td>1</td><td>50&#x200b;</td></tr></tbody></table></div>",
                                                  "<td>1</td><td>49&#x200b;</td></tr></tbody></table></div>", 1))
        self.assertEqual(near_miss.tables, [])

    def test_acs_single_semantic_figure_table_preserves_context_graphic(self) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS single semantic table</h1><section><h2>Results</h2><p>Lead prose.</p>
<div id="208638394-content"><figure content-id="tbl1 " id="table-1">
  <div id="tbl1"><span id="label-tbl1">Table 1.</span>
    <div id="caption-tbl1"><div>Comparative yields<a reveal-id="tbl1-fn2">a</a></div></div>
  </div>
  <div id="gr3"><a><img alt="Graphic. Refer to the image caption for details." src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="/></a></div>
  <div content-id="tbl1"></div>
  <div><table role="table" aria-labelledby=" label-tbl1" aria-describedby=" caption-tbl1">
    <thead><tr><th>Entry<a reveal-id="tbl1-fn1">b</a></th><th>Yield (%)</th></tr></thead>
    <tbody><tr><td>A</td><td>&gt;98</td></tr></tbody></table></div>
  <div></div>
  <div>
    <div id="tbl1-fn2" content-id="tbl1-fn2"><span><span rel="nofollow">a</span></span><p>Based on HPLC.</p></div>
    <div id="tbl1-fn1" content-id="tbl1-fn1"><span><span rel="nofollow">b</span></span><p>Resin = &#x03b2;-Ala PAM.</p></div>
  </div>
  <div><a href="/view-large/208638394" target="_blank" rel="nofollow"
    aria-label="View large Table 1.">View Large</a></div>
</figure></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(result.figures, [])
        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_001")
        self.assertEqual(table.source_id, "table-1")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.title_plain, "Table 1. Comparative yields^{a}")
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Entry^{b}", "Yield (%)"], ["A", ">98"]],
        )
        self.assertEqual(
            table.footnotes_plain,
            ["[a] Based on HPLC.", "[b] Resin = β-Ala PAM."],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["table_001_context_001"],
        )
        self.assertEqual(result.embedded_assets[0].parent_table_id, "table_001")
        self.assertEqual(
            result.embedded_assets[0].output_path,
            "tables/main/table_001_cells/context_001.png",
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Lead prose."],
        )

        near_miss = self.extract(source.replace("<div></div>\n  <div>",
                                                "<div>rendering residue</div>\n  <div>", 1))
        self.assertEqual(near_miss.tables, [])

    def test_acs_accessible_note_free_table_accepts_section_scoped_content_id(
        self,
    ) -> None:
        source = """<!doctype html><html><body><article>
<h1>ACS accessible note-free table</h1><section><h2>Results</h2>
<div id="193145708-content"><div content-id="tbl2 sec2.2">
  <div id="tbl2"><span id="label-tbl2">Table 2.</span>
    <div id="caption-tbl2"><div>Binding reduction</div></div>
  </div>
  <div><table role="table" aria-labelledby=" label-tbl2"
      aria-describedby=" caption-tbl2"><thead><tr><th>Agent</th><th>Reduction</th></tr></thead>
    <tbody><tr><td>1</td><td>0.646</td></tr></tbody>
  </table></div>
  <div><table aria-hidden="true"><thead><tr><th>Agent</th><th>Reduction</th></tr></thead>
    <tbody><tr><td>1</td><td>0.646</td></tr></tbody>
  </table></div>
  <div><a href="/view-large/193145708" target="_blank" rel="nofollow"
    aria-label="View large Table 2.">View Large</a></div>
</div></div></section></article></body></html>"""

        result = self.extract(source)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.table_id, "table_002")
        self.assertEqual(table.title_plain, "Table 2. Binding reduction")
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Agent", "Reduction"], ["1", "0.646"]],
        )
        self.assertEqual(table.footnotes_plain, [])

        near_miss = self.extract(
            source.replace('content-id="tbl2 sec2.2"', 'content-id="tbl2 discussion"')
        )
        self.assertEqual(near_miss.tables, [])

    def test_old_acs_author_and_article_information_is_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS front matter</h1>
<div>
  <a rel="nofollow" aria-haspopup="true">Peter B. Dervan</a>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
  <div content-id="ja123AF1">Corresponding author. No contact information available.</div>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
</div>
<div id="notes2" content-id="notes2"><p><strong>COI Statement:</strong>
  The authors declare no competing financial interest.</p></div>
<div>Publisher: American Chemical Society</div>
<div><span>Received:</span><span>December 13, 1999</span></div>
<div><span>Revision Received:</span><span>March 20, 2000</span></div>
<div><span>Accepted:</span><span>March 27, 2000</span></div>
<div><span>Published Online:</span><span>April 27, 2000</span></div>
<div><span>Published in Issue:</span><span>May 24, 2000</span></div>
<div><strong>Subjects</strong><a>DNA binding</a><span>,</span><a>Ligands</a></div>
<div>Online ISSN: 1520-5126</div>
<div>Print ISSN: 0002-7863</div>
<div><div>Funding</div><div><strong>Funding Group: </strong><ul><li><div>
  <strong>Award Group: </strong><div><ul><li><strong>Funder(s): </strong>
  <div>Example Trust</div></li><li><strong>Award Id(s): </strong>
  <div>ABC-123</div></li><li><strong>Funding Statement(s): </strong>
  <div>Supported by Example Trust.</div></li></ul></div></div></li></ul></div></div>
<div>Copyright © 2000 American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Copyright: Copyright © 2000 American Chemical Society",
                "Corresponding author: Peter B. Dervan. No contact information available.",
                "COI Statement: The authors declare no competing financial interest.",
                "Publisher: American Chemical Society",
                (
                    "Article history: Received: December 13, 1999; "
                    "Revision Received: March 20, 2000; "
                    "Accepted: March 27, 2000; "
                    "Published Online: April 27, 2000; "
                    "Published in Issue: May 24, 2000"
                ),
                "Subjects: DNA binding; Ligands",
                "Online ISSN: 1520-5126",
                "Print ISSN: 0002-7863",
                (
                    "Funding: Funder(s): Example Trust; Award Id(s): ABC-123; "
                    "Funding Statement(s): Supported by Example Trust."
                ),
            ],
        )
        self.assertEqual(
            len({block.block_id for block in result.front_matter}),
            len(result.front_matter),
        )

    def test_old_acs_shared_correspondence_note_is_emitted_once(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS shared correspondence</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
</div>
<div><a rel="nofollow" aria-haspopup="true">Bob Example</a>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
</div>
<div content-id="ja123AF1">Corresponding author. E-mail: shared@example.test.</div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        correspondence = [
            block.plain_text
            for block in result.front_matter
            if block.plain_text.startswith("Corresponding author")
        ]
        self.assertEqual(
            correspondence,
            [
                "Corresponding authors: Ada Example; Bob Example. "
                "E-mail: shared@example.test."
            ],
        )

    def test_old_acs_collective_affiliation_separates_correspondence_email(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS collective affiliation</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a><div><div>
  <div><div>Ada Example</div></div>
  <div><div>Department of Chemistry, Example University
    <a href="mailto:shared@example.test.">shared@example.test.</a></div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Bob Example</a>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
  <div><div><div><div>Bob Example</div></div>
  <div><div>Department of Chemistry, Example University
    <a href="mailto:shared@example.test.">shared@example.test.</a></div></div>
  <div><div content-id="ja123AF1">Corresponding author. No contact information available.</div></div>
  </div></div>
</div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        front_matter = [block.plain_text for block in result.front_matter]
        self.assertIn(
            "Affiliations: Department of Chemistry, Example University",
            front_matter,
        )
        self.assertNotIn("Affiliation (Ada Example):", "\n".join(front_matter))
        self.assertNotIn("Affiliation (Bob Example):", "\n".join(front_matter))
        self.assertIn(
            "Corresponding author: Bob Example. E-mail: shared@example.test.",
            front_matter,
        )

    def test_old_acs_shared_affiliation_subsets_keep_author_associations(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS affiliation subsets</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a><div><div>
  <div><div>Ada Example</div></div><div><div>Department A</div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Bob Example</a><div><div>
  <div><div>Bob Example</div></div><div><div>Department A</div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Cara Example</a><div><div>
  <div><div>Cara Example</div></div><div><div>Department B</div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Dan Example</a><div><div>
  <div><div>Dan Example</div></div><div><div>Department B</div></div>
</div></div></div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Ada Example): Department A",
                "Affiliation (Bob Example): Department A",
                "Affiliation (Cara Example): Department B",
                "Affiliation (Dan Example): Department B",
                "Publisher: American Chemical Society",
            ],
        )

    def test_old_acs_unmapped_subset_with_mapped_author_keeps_associations(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS mixed affiliation sources</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a><div><div>
  <div><div>Ada Example</div></div><div><div>Department A</div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Bob Example</a><div><div>
  <div><div>Bob Example</div></div><div><div>Department A</div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Cara Example</a>
  <span><a reveal-id="ja123AF1" aria-label="Author note">1</a></span>
</div>
<div id="ja123AF1" content-id="ja123AF1"><p>Department B</p></div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        front_matter = [block.plain_text for block in result.front_matter]
        self.assertIn("Affiliation (Ada Example): Department A", front_matter)
        self.assertIn("Affiliation (Bob Example): Department A", front_matter)
        self.assertIn("Affiliation (Cara Example): Department B", front_matter)
        self.assertNotIn("Affiliations: Department A", front_matter)

    def test_aacr_author_and_article_information_is_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>AACR front matter</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a><div><div>
  <div><div>Ada Example</div></div>
  <div><div><span>1</span>Example Cancer Center.</div>
    <div><span>2</span>Example Research Institute.</div></div>
</div></div></div>
<div><a rel="nofollow" aria-haspopup="true">Bob Example<i title="Corresponding Author">
  <span>Corresponding Author</span></i></a><div><div>
  <div><div>Bob Example<span><a reveal-id="cor1">*</a></span></div></div>
  <div><div><span>2</span>Example University.</div></div>
  <div><div content-id="cor1"><span>*</span><strong>Corresponding Author:</strong>
    Bob Example, Example University. E-mail: bob@example.test</div></div>
</div></div></div>
<div><span>Received:</span><span>August 26 2015</span></div>
<div><span>Revision Received:</span><span>December 04 2015</span></div>
<div><span>Accepted:</span><span>January 14 2016</span></div>
<div>Online ISSN: 1557-3125</div><div>Print ISSN: 1541-7786</div>
<div><div>Funding</div><div><strong>Funding Group: </strong><ul><li><div>
  <strong>Award Group: </strong><div><ul><li><strong>Funder(s): </strong>
  <div>Example Trust</div></li><li><strong>Award Id(s): </strong>
  <div>ABC-123</div></li></ul></div></div></li></ul></div></div>
<div>©2016 American Association for Cancer Research.</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                (
                    "Affiliation (Ada Example): Example Cancer Center; "
                    "Example Research Institute."
                ),
                "Affiliation (Bob Example): Example University.",
                "Corresponding author: Bob Example, Example University. E-mail: bob@example.test",
                (
                    "Article history: Received: August 26 2015; Revision Received: "
                    "December 04 2015; Accepted: January 14 2016"
                ),
                "Online ISSN: 1557-3125",
                "Print ISSN: 1541-7786",
                "Funding: Funder(s): Example Trust; Award Id(s): ABC-123",
                "Copyright: ©2016 American Association for Cancer Research.",
            ],
        )

    def test_aacr_figure_card_preserves_rich_caption_notation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>AACR figure</h1>
<section><h2>Results</h2><p>Text.</p>
<figure reveal-group-id="sec1" id="figure-1">
  <span id="viewTranscriptId_"></span><div>Figure 1.</div>
  <div><a><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    alt="Figure 1. Signal at pO2 after 10 × 106 cells; P &lt; 0.05."></a>
    <div><a>View large</a><a>Download slide</a></div></div>
  <div><p>Signal at pO<sub>2</sub> after 10 × 10<sup>6</sup> cells;
    <em>P</em> &lt; 0.05.</p></div>
</figure></section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        figure = result.figures[0]
        self.assertEqual(
            figure.caption_plain,
            "Signal at pO_{2} after 10 × 10^{6} cells; P < 0.05.",
        )
        self.assertIn("pO<sub>2</sub>", figure.caption_markdown)
        self.assertIn("10<sup>6</sup>", figure.caption_markdown)
        self.assertIn("<em>P</em>", figure.caption_markdown)

    def test_old_acs_present_address_note_is_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS present address</h1>
<div id="notes-7" content-id="notes-7"><span><span rel="nofollow">#</span></span>
  <p>Ada Example: Department of Chemistry, Example University, Example City.</p>
</div>
<div id="notes3" content-id="notes3"><p><strong>COI Statement:</strong>
  The authors declare no competing financial interest.</p></div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertIn(
            (
                "Present address: Ada Example: Department of Chemistry, "
                "Example University, Example City."
            ),
            [block.plain_text for block in result.front_matter],
        )

    def test_acs_author_popovers_include_affiliations_and_orcids(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>ACS authors</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a><div><div>
  <div><div>Ada Example</div></div>
  <div><div><div>Department of Chemistry, Example University, Example City</div>,
    <div>County</div><div>12345</div>, <div>U.K.</div></div>
    <div>Institute for Molecular Science, Example City, U.K.</div></div>
  <div>Search for other works by this author on:</div><div><a>This Site</a></div>
  <div><a id="contrib-orcid-0000-0002-1825-0097"
    href="https://orcid.org/0000-0002-1825-0097"></a></div>
</div></div></div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                (
                    "Affiliation (Ada Example): Department of Chemistry, "
                    "Example University, Example City, County 12345, U.K.; Institute for "
                    "Molecular Science, Example City, U.K."
                ),
                "ORCID (Ada Example): 0000-0002-1825-0097",
                "Publisher: American Chemical Society",
            ],
        )

    def test_acs_author_note_markers_control_shared_popover_affiliations(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>ACS mapped authors</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a>
  <span><a reveal-id="ja123AF3" aria-label="Author note">‡</a></span>
  <span><a reveal-id="ja123AF4" aria-label="Author note">§</a></span>
  <div><div><div><div>Ada Example</div></div>
  <div><div>Department A, Example University, City A, and Department B,
    Example Institute, City B.</div></div></div></div>
</div>
<div><a rel="nofollow" aria-haspopup="true">Bob Example</a>
  <span><a reveal-id="ja123AF5" aria-label="Author note">‖</a></span>
  <div><div><div><div>Bob Example</div></div>
  <div><div>Department A, Example University, City A, and Department B,
    Example Institute, City B.</div></div></div></div>
</div>
<div id="ja123AF3" content-id="ja123AF3"><span><span rel="nofollow">‡</span></span>
  <p>Example University.</p></div>
<div id="ja123AF4" content-id="ja123AF4"><span><span rel="nofollow">§</span></span>
  <p>Current address: New Department, New University.</p></div>
<div id="ja123AF5" content-id="ja123AF5"><span><span rel="nofollow">‖</span></span>
  <p>Example Institute.</p></div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Ada Example): Example University.",
                "Present address (Ada Example): New Department, New University.",
                "Affiliation (Bob Example): Example Institute.",
                (
                    "Affiliations: Department A, Example University, City A, "
                    "and Department B, Example Institute, City B."
                ),
                "Publisher: American Chemical Society",
            ],
        )

    def test_acs_affiliation_preserves_text_around_nested_institution(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>ACS authors</h1>
<div><a rel="nofollow" aria-haspopup="true">Ada Example</a><div><div>
  <div><div>Ada Example</div></div>
  <div><div>Department of Chemistry, Graduate School of Science,
    <div>Example University</div>, Example City 12345, Country</div></div>
</div></div></div>
<div>Publisher: American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                (
                    "Affiliation (Ada Example): Department of Chemistry, "
                    "Graduate School of Science, Example University, "
                    "Example City 12345, Country"
                ),
                "Publisher: American Chemical Society",
            ],
        )

    def test_old_acs_adjacent_superscript_tokens_are_one_script(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS superscript dialect</h1><section><h2>Results</h2>
<p>ImImPyPy-γ<sup>(</sup><em><sup>R</sup></em><sup>)-</sup><em><sup>seco</sup></em><sup>-CBI</sup>.</p>
<p>ImImPyPy-(R)<sup>H</sup><sub><sup>2</sup></sub><sup>N</sup>γ.</p>
<p><strong>ImImPyPy-(R)<sup>H</sup></strong><sub><sup>2</sup></sub><strong><sup>N</sup></strong><strong>γ.</strong></p>
<p>ImPyPyPy-(R)<sup>β-H<sub>2</sub></sup><sup>N</sup>γ.</p>
</section></article></body></html>"""
        )

        first, second, third, fourth = result.sections[0].blocks
        self.assertEqual(first.plain_text, "ImImPyPy-γ^{(R)-seco-CBI}.")
        self.assertEqual(
            first.markdown,
            "ImImPyPy-γ<sup>(<em>R</em>)-<em>seco</em>-CBI</sup>.",
        )
        self.assertEqual(second.plain_text, "ImImPyPy-(R)^{H_{2}N}γ.")
        self.assertEqual(
            second.markdown,
            "ImImPyPy-(R)<sup>H<sub>2</sub>N</sup>γ.",
        )
        self.assertEqual(third.plain_text, "ImImPyPy-(R)^{H_{2}N}γ.")
        self.assertEqual(
            third.markdown,
            "<strong>ImImPyPy-(R)<sup>H<sub>2</sub>N</sup>γ.</strong>",
        )
        self.assertEqual(fourth.plain_text, "ImPyPyPy-(R)^{β-H_{2}N}γ.")
        self.assertEqual(
            fourth.markdown,
            "ImPyPyPy-(R)<sup>β-H<sub>2</sub>N</sup>γ.",
        )
        for block in (first, second, third, fourth):
            safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_whitespace_only_script_separates_exponent_and_table_footnote(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Whitespace script separator</h1><div id="tbl1">
<header>Table 1. Binding constants.</header>
<table><tr><th>Agent</th><th>K<sub>eq</sub></th></tr>
<tr><td>A</td><td><strong>1.2 × 10<sup>7</sup></strong><strong><sup>\u2009</sup></strong><em><sup>f</sup></em></td></tr>
</table></div></article></body></html>"""
        )

        cell = result.tables[0].parts[0].rows[1][1]
        self.assertEqual(cell.text, "1.2 × 10^{7} ^{f}")
        self.assertEqual(
            cell.markdown,
            "<strong>1.2 × 10<sup>7</sup></strong> <em><sup>f</sup></em>",
        )
        safe_html = block_markup_to_safe_html(cell.markdown, kind="table_cell")
        self.assertTrue(rich_text_matches_plain(cell.text, safe_html))

    def test_elsevier_citation_part_is_not_treated_as_markdown_link(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Multipart citation</h1>
<section><h2>Results</h2>
<p>Prior work <a href="#b0035"><span>[6](b)</span></a> established this.</p>
</section>
<section id="references"><h2>References</h2><ol>
<li id="b0035"><span>[6]</span> Synthetic reference.</li>
</ol></section>
</article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Prior work <sup>[6](b)</sup> established this.",
        )
        safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
        self.assertEqual(
            safe_html,
            "Prior work <sup>[6](b)</sup> established this.",
        )
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_sciencedirect_hash_bib_citation_style_is_label_gated(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Mixed citation styles</h1>
<section><h2>Results</h2>
<p>Numeric <a href="#bib2">2</a>; already bracketed <a href="#bib3">[3]</a>;
multipart <a href="#bib6b">6(b)</a>; author-year
(<a href="#bib41">Van Regenmortel et al., 2000</a>); noncitation
<a href="#bibliography">References</a>.</p>
<p>Comma list [<a href="#bib4">4</a>,<a href="#bib12">12</a>]; range
[<a href="#bib2-11">2–11</a>]; bracketed multipart
[<a href="#bib6b">6(b)</a>].</p>
<p>Expanded bracket labels [<a href="#bib5">[5]</a>,
<a href="#bib6">[6]</a>, <a href="#bib7b">[7](b)</a>].</p>
<p>Superscript <sup><a href="#bib7">7</a></sup> and Elsevier multipart
<a href="#b0035"><span>[6](b)</span></a>.</p>
<p>Elsevier literal bracket [<a href="#b1">1</a>] and expanded
[<a href="#b2">[2]</a>, <a href="#b3">[3]</a>].</p>
</section>
<section id="references"><h2>References</h2><ol>
<li id="bib2">2. Numeric.</li><li id="bib3">3. Bracketed.</li>
<li id="bib6b">6(b). Part.</li><li id="bib7">7. Superscript.</li>
<li id="bib41">41. Author-year.</li><li id="b0035">6(b). Multipart.</li>
<li id="bib4">4. List.</li><li id="bib12">12. List.</li>
<li id="bib2-11">2–11. Range.</li>
<li id="bib5">5. Expanded.</li><li id="bib6">6. Expanded.</li>
<li id="bib7b">7(b). Expanded.</li>
<li id="b1">1. Literal.</li><li id="b2">2. Literal.</li>
<li id="b3">3. Literal.</li>
</ol></section>
</article></body></html>"""
        )

        mixed, bracket_groups, expanded_brackets, superscript, literal = result.sections[0].blocks
        self.assertEqual(
            mixed.markdown,
            (
                "Numeric [2]; already bracketed [3]; multipart [6(b)]; "
                "author-year (Van Regenmortel et al., 2000); noncitation References."
            ),
        )
        self.assertNotIn("[[3]]", mixed.markdown)
        self.assertEqual(
            bracket_groups.markdown,
            "Comma list [4,12]; range [2–11]; bracketed multipart [6(b)].",
        )
        bracket_safe_html = block_markup_to_safe_html(
            bracket_groups.markdown, kind="paragraph"
        )
        self.assertEqual(
            plain_text_from_safe_html(bracket_safe_html), bracket_groups.plain_text
        )
        self.assertTrue(
            rich_text_matches_plain(bracket_groups.plain_text, bracket_safe_html)
        )
        self.assertEqual(
            expanded_brackets.markdown,
            "Expanded bracket labels [5, 6, 7(b)].",
        )
        self.assertEqual(
            expanded_brackets.plain_text,
            "Expanded bracket labels [5, 6, 7(b)].",
        )
        self.assertEqual(
            superscript.markdown,
            "Superscript <sup>[7]</sup> and Elsevier multipart <sup>[6](b)</sup>.",
        )
        self.assertEqual(
            literal.markdown,
            "Elsevier literal bracket [1] and expanded [2, 3].",
        )
        self.assertEqual(literal.plain_text, literal.markdown)
        author_year_plain = "Author-year (Van Regenmortel et al., 2000)."
        author_year = self.extract(
            """<!doctype html><html><body><article><h1>Author-year</h1>
<section><h2>Results</h2><p>Author-year
(<a href="#bib41">Van Regenmortel et al., 2000</a>).</p></section>
<section id="references"><h2>References</h2><ol>
<li id="bib41">41. Author-year.</li></ol></section></article></body></html>"""
        ).sections[0].blocks[0]
        safe_html = block_markup_to_safe_html(author_year.markdown, kind="paragraph")
        self.assertEqual(author_year.plain_text, author_year_plain)
        self.assertEqual(plain_text_from_safe_html(safe_html), author_year_plain)
        self.assertTrue(rich_text_matches_plain(author_year_plain, safe_html))

        abbreviated = self.extract(
            """<!doctype html><html><body><article><h1>Author-year continuation</h1>
<section><h2>Results</h2><p>Prior work
(<a href="#bib171">Weber et al., 2021</a>, <a href="#bib172">2022</a>).</p></section>
<section id="references"><h2>References</h2><ol>
<li id="bib171">171. Weber 2021.</li><li id="bib172">172. Weber 2022.</li>
</ol></section></article></body></html>"""
        ).sections[0].blocks[0]
        abbreviated_plain = "Prior work (Weber et al., 2021, 2022)."
        abbreviated_html = block_markup_to_safe_html(
            abbreviated.markdown, kind="paragraph"
        )
        self.assertEqual(abbreviated.markdown, abbreviated_plain)
        self.assertEqual(abbreviated.plain_text, abbreviated_plain)
        self.assertEqual(
            plain_text_from_safe_html(abbreviated_html), abbreviated_plain
        )
        self.assertTrue(
            rich_text_matches_plain(abbreviated_plain, abbreviated_html)
        )

        semicolon_abbreviated = self.extract(
            """<!doctype html><html><body><article><h1>Author-year continuation</h1>
<section><h2>Results</h2><p>Prior work
(<a href="#bib171">Brennan et al., 2004</a>; <a href="#bib172">2013</a>;
<a href="#bib173">2016</a>) and numeric work
(<a href="#bib1">1</a>; <a href="#bib2">2</a>).</p></section>
<section id="references"><h2>References</h2><ol>
<li id="bib1">1. Numeric.</li><li id="bib2">2. Numeric.</li>
<li id="bib171">171. Brennan 2004.</li><li id="bib172">172. Brennan 2013.</li>
<li id="bib173">173. Brennan 2016.</li>
</ol></section></article></body></html>"""
        ).sections[0].blocks[0]
        semicolon_markdown = (
            "Prior work (Brennan et al., 2004; 2013; 2016) and numeric work "
            "([1]; [2])."
        )
        semicolon_plain = (
            "Prior work (Brennan et al., 2004; 2013; 2016) and numeric work "
            "(1; 2)."
        )
        self.assertEqual(semicolon_abbreviated.markdown, semicolon_markdown)
        self.assertEqual(semicolon_abbreviated.plain_text, semicolon_plain)

    def test_sciencedirect_hash_b_author_year_citation_stays_on_baseline(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect author-year citation</h1>
<section><h2>Results</h2><p>Prior work
(<a href="#b0195" name="bb0195"><span>Munoz et al., 2003</span></a>)
established this result; numeric evidence <a href="#b0005">5</a>.</p></section>
<section><h2>References</h2><ol>
<li id="b0195">Munoz et al. Citation.</li>
<li id="b0005">Numeric citation.</li>
</ol></section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Prior work (Munoz et al., 2003) established this result; "
            "numeric evidence <sup>5</sup>.",
        )
        self.assertEqual(
            block.plain_text,
            "Prior work (Munoz et al., 2003) established this result; "
            "numeric evidence ^{5}.",
        )

    def test_external_link_parentheses_are_safe_in_rich_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Parenthesized link</h1>
<section><h2>Results</h2>
<p>See <a href="https://example.test/search?q=alpha(beta)">the source</a>.</p>
</section>
</article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertIn("alpha%28beta%29", block.markdown)
        safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
        self.assertIn('href="https://example.test/search?q=alpha%28beta%29"', safe_html)
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_external_link_angle_brackets_are_safe_in_rich_text(self) -> None:
        target = (
            "https://doi.org/10.1002/%28SICI%291097-0215%2819970904%29"
            "72:5<801::AID-IJC16>3.0.CO;2-B"
        )
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>Legacy DOI link</h1>
<section><h2>References</h2><ol><li>
<a href="{target}">{target}</a>.
</li></ol></section>
</article></body></html>"""
        )

        reference = result.references[0]
        self.assertIn("%3C801::AID-IJC16%3E", reference.markdown)
        safe_html = block_markup_to_safe_html(reference.markdown, kind="reference")
        self.assertIn("%3C801::AID-IJC16%3E", safe_html)
        self.assertTrue(rich_text_matches_plain(reference.plain_text, safe_html))

    def test_double_encoded_nonbreaking_space_does_not_leak_into_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Double-encoded spacing</h1>
<section><h2>References</h2><ol><li>
Clinicopathological result: a&amp;nbsp;meta-analysis.
</li></ol></section>
</article></body></html>"""
        )

        reference = result.references[0]
        self.assertEqual(
            reference.plain_text,
            "Clinicopathological result: a meta-analysis.",
        )
        self.assertEqual(
            reference.markdown,
            "Clinicopathological result: a meta-analysis.",
        )

    def test_unmatched_authored_bracket_before_link_does_not_swallow_text(self) -> None:
        markup = (
            "Bruce CD. 2014 [cited June 17, 2014. Available from "
            "[http://youtu.be/example](https://youtu.be/example)"
        )
        plain = (
            "Bruce CD. 2014 [cited June 17, 2014. Available from "
            "http://youtu.be/example"
        )

        safe_html = block_markup_to_safe_html(markup, kind="reference")

        self.assertEqual(plain_text_from_safe_html(safe_html), plain)
        self.assertIn(
            '<a href="https://youtu.be/example">http://youtu.be/example</a>',
            safe_html,
        )
        self.assertTrue(rich_text_matches_plain(plain, safe_html))

    def test_sciencedirect_paragraph_caption_recovers_scheme_identity(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect caption</h1>
<section><h2>Results</h2>
<figure id="f0015"><span><img alt=""></span><span><span>
<p id="sp025"><span>Scheme 1</span>. Reagents used SOCl<sub>2</sub>.</p>
</span></span></figure>
</section>
</article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        scheme = result.figures[0]
        self.assertEqual(
            (scheme.figure_id, scheme.kind, scheme.label),
            ("scheme_001", "scheme", "Scheme 1"),
        )
        self.assertEqual(scheme.caption_plain, "Reagents used SOCl_{2}.")
        self.assertEqual(
            scheme.caption_markdown,
            "Reagents used SOCl<sub>2</sub>.",
        )

    def test_sciencedirect_keyword_divs_without_ids_are_included(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect keywords</h1>
<div id="aep-keywords"><h2>Keywords</h2>
<div><span>Polyamides</span></div><div><span>Minor groove</span></div>
</div>
</article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].heading, "Keywords")
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Polyamides", "Minor groove"],
        )
        self.assertEqual(
            [block.kind for block in result.sections[0].blocks],
            ["keyword", "keyword"],
        )

    def test_sciencedirect_unheaded_abstract_colon_keywords_and_hex_paragraph(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Current ScienceDirect article</h1>
<div id="abstracts"><div id="ab0005"><div id="abs0005">
<div id="sp0060">An unheaded <em>RUNX1</em> abstract.</div>
</div></div></div>
<div id="keys0005"><h2>Keywords:</h2>
<div id="key0005"><span>Polyamides</span></div>
<div id="key0010"><span>Minor groove</span></div></div>
<section id="ack1"><h2>Acknowledgments</h2>
<div id="p005a">Exact authored acknowledgement.</div>
<div id="p005z">Near-miss publisher text.</div></section>
</article></body></html>"""
        )

        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Abstract", ["An unheaded RUNX1 abstract."]),
                ("Keywords:", ["Polyamides", "Minor groove"]),
                ("Acknowledgments", ["Exact authored acknowledgement."]),
            ],
        )
        self.assertEqual(
            result.sections[0].blocks[0].markdown,
            "An unheaded <em>RUNX1</em> abstract.",
        )

    def test_sciencedirect_unheaded_abstract_requires_complete_wrapper_chain(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Near-miss abstract wrapper</h1>
<div id="not-abstracts"><div id="ab0005"><div id="abs0005">
<div id="sp0060">Do not promote this UI fragment.</div>
</div></div></div>
<section><h2>Results</h2><p>Authored result.</p></section>
</article></body></html>"""
        )

        self.assertEqual([section.heading for section in result.sections], ["Results"])
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored result.")

    def test_sciencedirect_research_highlights_exclude_issue_navigation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect highlights</h1>
<div><h3>Research highlights</h3>
<div id="sp005">► First authored highlight. ► Second authored highlight.</div>
</div>
<ul class="issue-navigation"><li>Previous article in issue</li>
<li>Next article in issue</li></ul>
</article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].heading, "Research highlights")
        self.assertEqual(len(result.sections[0].blocks), 1)
        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "list")
        self.assertEqual(
            block.plain_text,
            "- First authored highlight.\n- Second authored highlight.",
        )
        self.assertNotIn("article in issue", block.plain_text)

    def test_sciencedirect_abspara_is_scoped_to_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect abstract dialect</h1>
<section id="abstract"><h2>Abstract</h2>
<div id="abspara0010"><span>The ability of <i>AVP</i> to inhibit β virus was evaluated.</span></div>
<div id="sp0015"><span>Existing abstract dialect remains visible.</span></div>
</section>
<section id="methods"><h2>Methods</h2>
<div id="abspara0090"><span>Wrongly named non-abstract UI text.</span></div>
<p>Authored method prose.</p>
</section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            [
                "The ability of AVP to inhibit β virus was evaluated.",
                "Existing abstract dialect remains visible.",
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Authored method prose."],
        )
        self.assertNotIn(
            "Wrongly named non-abstract UI text",
            "\n".join(
                block.plain_text
                for section in result.sections
                for block in section.blocks
            ),
        )
        for block in result.sections[0].blocks:
            safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertEqual(plain_text_from_safe_html(safe_html), block.plain_text)
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_sciencedirect_bibliographic_and_affiliation_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="publication"><div>Volume 404, Issue 3, 21 January 2011,
Pages 848-852</div></div>
<h1>ScienceDirect front matter</h1>
<div id="banner"><div><div id="author-group">
<span type="button"><span>Alice Example</span><span id="baff1"><sup>a</sup></span></span>
<a href="/author/example"><span>Bob Example</span><span id="baff2"><sup>b</sup></span></a>
<dl><dt><sup>a</sup></dt><dd>Department A, University A</dd></dl>
<dl><dt><sup>b</sup></dt><dd>Department B, University B</dd></dl>
</div><p>Received 8 December 2010, Available online 23 December 2010.</p></div></div>
<section><h2>Results</h2><p>Body.</p></section>
<div><span>Copyright © 2010 Publisher. All rights reserved.</span></div>
</article></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "404",
                "issue": "3",
                "pages": "848-852",
                "date": "21 January 2011",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation assignments: Alice Example (a); Bob Example (b)",
                "Affiliation a: Department A, University A",
                "Affiliation b: Department B, University B",
                "Article history: Received 8 December 2010, Available online 23 December 2010.",
                "Copyright: Copyright © 2010 Publisher. All rights reserved.",
            ],
        )

    def test_sciencedirect_article_number_author_note_and_license_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="publication"><h2>Example Journal</h2><div><a href="/journal/example/vol/230">Volume 230</a>, 15 June 2023, 115256</div></div>
<h1>ScienceDirect current front matter</h1>
<div id="banner"><div id="author-group">
<span>Author links open overlay panel</span>
<span type="button"><span>Alice Example</span><span id="bfn1"><sup>1</sup></span></span>,
<a href="/author/bob"><span>Bob Example</span><span id="bfn1"><sup>1</sup></span></a>
</div></div>
<div id="article-identifier-links"><a href="https://doi.org/10.1000/example">DOI</a></div>
<div><div><span>Under a Creative Commons </span>
<a href="http://creativecommons.org/licenses/by/4.0/">license</a></div>
<div>Open access</div></div>
<section><h2>Results</h2><p>Body.</p></section>
<div><dl><dt><a href="#bfn1"><sup>1</sup></a></dt>
<dd><div id="ntpara0010">These authors contributed equally to this work.</div></dd>
</dl></div>
<div><span>© 2023 The Authors. Published by Example Publisher.</span></div>
</article></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "article_number": "115256",
                "date": "15 June 2023",
                "volume": "230",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author note assignments: Alice Example (1); Bob Example (1)",
                "Author note 1: These authors contributed equally to this work.",
                "License: Creative Commons license: http://creativecommons.org/licenses/by/4.0/",
                "Access: Open access",
                "Copyright: © 2023 The Authors. Published by Example Publisher.",
            ],
        )

    def test_sciencedirect_reference_nested_blocks_keep_word_boundaries(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect references</h1>
<section><h2>References</h2><ol><li>
  <span><a id="ref-id-b0005"><span>[1]</span></a></span>
  <span id="h0005">
    <div><div>A. Example, <em>et al.</em></div>
      <div id="ref-id-h0005">A β-target <i>study</i></div></div>
    <div>Journal Name, 2 (2020), p. 3,
      <a href="https://doi.org/10.1000/example">10.1000/example</a></div>
    <div lang="en"><a href="https://scholar.example/">Google Scholar</a></div>
  </span>
</li></ol></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertIn("A. Example, et al. A β-target study Journal Name", reference.plain_text)
        self.assertIn(
            "A. Example, <em>et al.</em> A β-target <em>study</em> Journal Name",
            reference.markdown,
        )
        self.assertNotIn("Google Scholar", reference.plain_text)
        self.assertEqual(reference.plain_text.count("10.1000/example"), 1)
        self.assertTrue(reference.plain_text.endswith("10.1000/example"))

    def test_sciencedirect_b_anchor_with_authored_label_and_unidentified_body(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect authored-label references</h1>
<section><h2>References</h2><ol><li>
  <span><a href="#bb0005" id="ref-id-b0005"><span>Abbate et al., 2004</span></a></span>
  <span>
    <div><div>E.A. Abbate, J.M. Berger, M.R. Botchan</div>
      <div>The X-ray structure of the papillomavirus helicase in complex with its molecular matchmaker E2</div></div>
    <div>Genes Dev., 18 (2004), pp. 1981-1996</div>
    <div lang="en"><a href="https://doi.org/10.1101/gad.1220104">Crossref</a>
      <a href="https://scopus.example/">View in Scopus</a>
      <a href="https://scholar.example/">Google Scholar</a></div>
  </span>
</li></ol></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertEqual(
            reference.plain_text,
            (
                "Abbate et al., 2004. E.A. Abbate, J.M. Berger, M.R. Botchan "
                "The X-ray structure of the papillomavirus helicase in complex "
                "with its molecular matchmaker E2 Genes Dev., 18 (2004), "
                "pp. 1981-1996 DOI: https://doi.org/10.1101/gad.1220104"
            ),
        )
        self.assertNotIn("Crossref", reference.plain_text)
        self.assertNotIn("View in Scopus", reference.plain_text)
        self.assertNotIn("Google Scholar", reference.plain_text)
        self.assertEqual(reference.markdown, reference.plain_text)

    def test_sciencedirect_aria_caption_keeps_split_multi_panel_legend(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect split caption</h1>
<section><h2>Results</h2><p>Body.</p>
<figure id="fig2"><span>
  <img aria-describedby="cap0020"
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Fig. 2">
  <ol><li><a href="https://example.invalid/figure">Download image</a></li></ol>
</span><span><span id="cap0020">
  <p id="fspara0010"><span>Fig. 2</span>. Synthetic result.</p>
  <div id="fspara0015"><strong>(A)</strong> First panel. <strong>(B)</strong> Second panel.</div>
</span></span></figure></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        caption = result.figures[0]
        self.assertEqual(caption.label, "Figure 2")
        self.assertEqual(
            caption.caption_plain,
            "Synthetic result. (A) First panel. (B) Second panel.",
        )
        self.assertEqual(
            caption.caption_markdown,
            "Synthetic result.\n\n<strong>(A)</strong> First panel. "
            "<strong>(B)</strong> Second panel.",
        )

    def test_springer_semantic_caption_appends_local_aria_legend(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer split caption</h1>
<section><h2>Results</h2><p>Body.</p>
<figure id="figure-1">
  <figcaption><b id="Fig1">Fig. 1: Complete result.</b></figcaption>
  <div><div><picture><img aria-describedby="figure-1-desc"
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Figure 1"></picture><a><span>Full size image</span></a></div>
    <div id="figure-1-desc"><p><b>A</b> First panel. <b>B</b> Second panel with
    <i>n</i> = 3 and <i>P</i> &lt; 0.01.</p></div>
  </div>
</figure></section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        caption = result.figures[0]
        self.assertEqual(caption.label, "Figure 1")
        self.assertEqual(
            caption.caption_plain,
            "Fig. 1: Complete result. A First panel. B Second panel with "
            "n = 3 and P < 0.01.",
        )
        self.assertIn("<strong>A</strong> First panel.", caption.caption_markdown)
        self.assertIn("<em>n</em> = 3", caption.caption_markdown)
        self.assertNotIn("Full size image", caption.caption_plain)

    def test_springer_stripped_hidden_title_is_recovered_only_with_full_signature(self) -> None:
        result = self.extract(
            """<body><main>
<div aria-hidden="true"><div>
  <div>Recovered current Springer title</div>
  <div><a href="/content/pdf/10.1007/s00000-001-00001-0.pdf" download="">
    <span>Download PDF</span></a></div>
</div></div>
<div><header><ul><li><a href="#auth-Alpha-A-Aff1">Alpha A.</a></li></ul></header></div>
<section><h2>Results</h2><p>Body.</p></section>
<section aria-labelledby="Bib1"><div id="Bib1-section"><h2 id="Bib1">References</h2>
  <div id="Bib1-content"><div><ol><li><p id="ref-CR1">Citation.</p></li></ol>
  </div></div></div></section>
</main></body>"""
        )

        self.assertEqual(result.title, "Recovered current Springer title")
        self.assertEqual(result.sections[0].heading, "Results")

        near_miss = self.extract(
            """<body><main>
<div aria-hidden="true"><div>
  <div>Untrusted anonymous heading</div>
  <div><a href="/content/pdf/10.1007/s00000-001-00001-0.pdf" download="">
    <span>Download PDF</span></a></div>
</div></div>
<div><header><ul><li><a href="#auth-Alpha-A-Aff1">Alpha A.</a></li></ul></header></div>
<section><h2>Results</h2><p>Body.</p></section>
</main></body>"""
        )
        self.assertEqual(near_miss.title, "")

    def test_springer_stripped_book_chapter_title_is_recovered_from_cite_card(self) -> None:
        result = self.extract(
            """<body><article>
<div><header><ul><li><a href="#auth-Alpha-A">Alpha A.</a></li></ul></header></div>
<p>Chapter body.</p>
<p id="ref-CR1">Citation.</p>
<a href="/content/pdf/10.1007/978-1-234-56789-0_4.pdf?pdf=inline" download="">PDF</a>
<section><div id="chapter-info-section"><h2>About this chapter</h2><div>
  <h3 id="citeas">Cite this chapter</h3>
  <p>Alpha, A. (2022). Recovered Springer Chapter Title. In: Example Book.
  Springer. https://doi.org/10.1007/978-1-234-56789-0_4</p>
</div></div></section>
</article></body>"""
        )
        self.assertEqual(result.title, "Recovered Springer Chapter Title")

        near_miss = self.extract(
            """<body><article>
<div><header><ul><li><a href="#auth-Alpha-A">Alpha A.</a></li></ul></header></div>
<p>Chapter body.</p><p id="ref-CR1">Citation.</p>
<section><div id="chapter-info-section"><h2>About this chapter</h2><div>
  <h3 id="citeas">Cite this chapter</h3>
  <p>Alpha, A. (2022). Untrusted Chapter Title. In: Example Book.
  Springer. https://doi.org/10.1007/978-1-234-56789-0_4</p>
</div></div></section>
</article></body>"""
        )
        self.assertEqual(near_miss.title, "")

    def test_springer_stripped_protocol_title_is_recovered_from_cite_card(self) -> None:
        pixel = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<body><article>
<div><header><ul>
  <li><a href="#auth-Alpha-A">Alpha A.</a><sup><a href="#Aff2">2</a></sup></li>
  <li><a href="#auth-Beta-B">Beta B.</a><sup><a href="#Aff2">2</a></sup></li>
</ul></header></div>
<section><h2>Materials</h2><p>Protocol body (<i>see</i> <b>Note</b> <a href="#Sec9">
<b>1</b>
</a>).</p></section>
<div id="cobranding-and-download-availability-text"><div><p>
  Access provided by Example Library.
  <a href="/content/pdf/10.1007/978-1-234-56789-0_4.pdf?pdf=inline"
    id="js-body-chapter-download" download="">Download protocol PDF</a>
</p></div></div>
<section><h2>Notes</h2><ol>
  <li><span>1.</span><p>First note.</p></li>
  <li><span>2.</span><p>Formula follows:</p><div id="Equa"><div><span>
    $$ C=1\\times {10}^{{-2}} $$
  </span></div></div><p>where C is concentration.</p></li>
  <li><span>3.</span><p>Last note.</p></li>
</ol></section>
<div id="figure-1"><figure id="figure-1">
  <figcaption><b id="Fig1">Fig. 1</b></figcaption>
  <div><div><a><picture><img aria-describedby="Fig1"
    src="data:image/gif;base64,{pixel}" alt="figure 1"></picture></a></div>
    <div id="figure-1-desc"><p>Complete protocol legend.</p></div></div>
  <div><a href="/protocol/10.1007/978-1-234-56789-0_4/figures/1">Full size image</a></div>
</figure></div>
<p id="ref-CR1">Citation.</p>
<section><h2>Author information</h2><h3 id="affiliations">Authors and Affiliations</h3>
  <ol><li id="Aff2"><p>Example Institute, Example City</p><p>Alpha A. &amp; Beta B.</p></li></ol>
  <h3 id="corresponding-author">Corresponding author</h3>
  <p id="corresponding-author-list">Correspondence to
    <a aria-label="email Alpha A." href="mailto:alpha@example.test">Alpha A.</a>.</p>
</section>
<section><div id="chapter-info-section"><h2 id="chapter-info">About this protocol</h2><div>
  <h3 id="citeas">Cite this protocol</h3>
  <p>Alpha, A. (2022). Recovered Springer Protocol Title. In: Example Book.
  Springer. https://doi.org/10.1007/978-1-234-56789-0_4</p>
  <ul><li><p>Published: <time>03 January 2022</time></p></li></ul>
  <h3>Key words</h3><ul><li>Alpha</li><li>Beta</li></ul>
</div></div></section>
</article></body>"""
        )

        self.assertEqual(result.title, "Recovered Springer Protocol Title")
        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            "Protocol body (see Note 1).",
        )
        notes = next(section for section in result.sections if section.heading == "Notes")
        self.assertEqual(
            [block.kind for block in notes.blocks[:5]],
            ["list", "paragraph", "equation", "paragraph", "paragraph"],
        )
        self.assertEqual(notes.blocks[0].plain_text, "1. First note.")
        self.assertEqual(notes.blocks[1].plain_text, "2. Formula follows:")
        self.assertEqual(notes.blocks[2].plain_text, "$$ C=1\\times 10^{-2} $$")
        self.assertEqual(notes.blocks[3].plain_text, "where C is concentration.")
        self.assertEqual(notes.blocks[4].plain_text, "3. Last note.")
        self.assertEqual(result.figures[0].caption_plain, "Complete protocol legend.")
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Correspondence: Alpha A. — alpha@example.test",
                "Affiliation — Alpha A. & Beta B.: Example Institute, Example City",
                "Chapter history: Published: 03 January 2022",
                "Keywords: Alpha; Beta",
            ],
        )
        self.assertFalse(
            any(
                "Access provided by" in block.plain_text
                for section in result.sections
                for block in section.blocks
            )
        )

    def test_springer_book_chapter_hierarchical_figure_and_artwork(self) -> None:
        pixel = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<body><article>
<div><header><ul><li><a href="#auth-Alpha-A">Alpha A.</a></li></ul></header></div>
<a href="/content/pdf/10.1007/978-1-234-56789-0_4.pdf?pdf=inline" download="">PDF</a>
<p id="ref-CR1">Citation.</p>
<div id="figure-1"><figure id="figure-1">
  <figcaption><b id="Fig1">Fig. 4.1</b></figcaption>
  <div><div><a><picture><img aria-describedby="Fig1"
    src="data:image/gif;base64,{pixel}" alt="figure 1"></picture></a></div>
    <div id="figure-1-desc"><p>Complete hierarchical legend.</p></div></div>
  <div><a href="/chapter/10.1007/978-1-234-56789-0_4/figures/1">Full size image</a></div>
</figure></div>
<div id="figure-a"><figure><div id="Figa"><div><picture><img
  aria-describedby="Figa" src="data:image/gif;base64,{pixel}"
  alt="figure a"></picture></div></div></figure></div>
<section><div id="chapter-info-section"><h2>About this chapter</h2><div>
  <h3 id="citeas">Cite this chapter</h3>
  <p>Alpha, A. (2022). Recovered Springer Chapter Title. In: Example Book.
  Springer. https://doi.org/10.1007/978-1-234-56789-0_4</p>
</div></div></section>
</article></body>"""
        )
        self.assertEqual(
            [(item.figure_id, item.label, item.caption_plain) for item in result.figures],
            [
                ("figure_004_001", "Figure 4.1", "Complete hierarchical legend."),
                ("unnumbered_artwork_001", "Unnumbered artwork 1", ""),
            ],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["figure_004_001", "unnumbered_artwork_001"],
        )

    def test_springer_book_chapter_recovers_front_matter_and_unnumbered_equation(self) -> None:
        result = self.extract(
            """<body><article>
<div><header><ul><li><a href="#auth-Alpha-A">Alpha A.</a>
  <sup><a href="#Aff2">2</a></sup></li></ul></header></div>
<section><h2>Methods</h2><p>Concentration was calculated below.</p>
  <div id="Equa"><div><span>$$ c = \\frac{Abs}{9900 \\times a \\times d} (M) $$</span></div></div>
</section>
<div id="cobranding-and-download-availability-text"><div><p>
  Access provided by Example Library.
  <a href="/content/pdf/10.1007/978-1-234-56789-0_4.pdf?pdf=inline"
    id="js-body-chapter-download" download="">Download chapter PDF</a>
</p></div></div>
<p id="ref-CR1">Citation.</p>
<section><h2>Author information</h2><h3 id="affiliations">Authors and Affiliations</h3>
  <ol><li id="Aff2"><p>Example Institute, Example City</p><p>Alpha A.</p></li></ol>
  <h3 id="corresponding-author">Corresponding author</h3>
  <p id="corresponding-author-list">Correspondence to
    <a aria-label="email Alpha A." href="mailto:alpha@example.test">Alpha A.</a>.</p>
</section>
<section><div id="chapter-info-section"><h2 id="chapter-info">About this chapter</h2><div>
  <h3 id="citeas">Cite this chapter</h3>
  <p>Alpha, A. (2022). Recovered Springer Chapter Title. In: Example Book.
  Springer. https://doi.org/10.1007/978-1-234-56789-0_4</p>
  <ul><li><p>Published: <time>03 January 2022</time></p></li></ul>
  <h3>Keywords</h3><ul><li>Alpha</li><li>Beta</li></ul>
</div></div></section>
</article></body>"""
        )

        self.assertEqual(result.title, "Recovered Springer Chapter Title")
        self.assertEqual(
            result.bibliographic,
            {"date": "03 January 2022", "first_published": "03 January 2022"},
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Correspondence: Alpha A. — alpha@example.test",
                "Affiliation — Alpha A.: Example Institute, Example City",
                "Chapter history: Published: 03 January 2022",
                "Keywords: Alpha; Beta",
            ],
        )
        methods = next(section for section in result.sections if section.heading == "Methods")
        blocks = methods.blocks
        self.assertEqual([block.kind for block in blocks[:2]], ["paragraph", "equation"])
        self.assertEqual(
            blocks[1].plain_text,
            "$$ c = \\frac{Abs}{9900 \\times a \\times d} (M) $$",
        )
        self.assertNotIn(
            "Access provided by Example Library",
            " ".join(block.plain_text for block in blocks),
        )

    def test_springer_stripped_hidden_title_authenticates_about_article_metadata(self) -> None:
        result = self.extract(
            """<body><main>
<div aria-hidden="true"><div>
  <div>Recovered current Springer title</div>
  <div><a href="/content/pdf/10.1007/s00000-001-00001-0.pdf" download="">
    <span>Download PDF</span></a></div>
</div></div>
<div><header><ul><li><a href="#auth-Alpha-A-Aff1">Alpha A.</a></li></ul></header></div>
<section><h2>Results</h2><p>Body.</p></section>
<section aria-labelledby="Bib1"><div id="Bib1-section"><h2 id="Bib1">References</h2>
  <div id="Bib1-content"><div><ol><li><p id="ref-CR1">Citation.</p></li></ol>
  </div></div></div></section>
<section><h2>Acknowledgments</h2><p>Thanks.</p><h3>Conflict of interest</h3>
  <p>None.</p></section>
<section><h2>Author information</h2><h3>Authors and Affiliations</h3>
  <ol><li id="Aff1"><p>Institute.</p><p>Alpha A.</p></li></ol>
  <h3 id="corresponding-author">Corresponding author</h3>
  <p id="corresponding-author-list">Correspondence to
    <a aria-label="email Alpha A." href="mailto:alpha@example.test">Alpha A.</a>.</p>
</section>
<section><h2>Electronic supplementary material</h2><p>Linked supplement.</p></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
<section><div id="article-info-section"><h2 id="article-info">About this article</h2><div>
  <h3 id="citeas">Cite this article</h3>
  <p>Alpha, A. Recovered current Springer title. <i>Example Journal</i>
    <b>7</b>, 101–109 (2001). https://doi.org/10.1007/s00000-001-00001-0</p>
  <ul><li><p>Received: <time>01 January 2001</time></p></li>
    <li><p>Accepted: <time>02 February 2001</time></p></li>
    <li><p>Published: <time>03 March 2001</time></p></li></ul>
  <h3>Keywords</h3><ul><li>Alpha</li><li>Beta</li></ul>
</div></div></section>
</main></body>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "7",
                "pages": "101–109",
                "date": "03 March 2001",
                "first_published": "03 March 2001",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Correspondence: Alpha A. — alpha@example.test",
                "Article history: Received: 01 January 2001; "
                "Accepted: 02 February 2001; Published: 03 March 2001",
                "Keywords: Alpha; Beta",
            ],
        )
        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Acknowledgments",
                "Conflict of interest",
                "Author information",
                "Authors and Affiliations",
                "Corresponding author",
            ],
        )

    def test_springer_adjacent_italic_statistical_comparison_keeps_space(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer statistical comparison</h1>
<section><h2>Statistics</h2>
<p><i>p</i> values &lt; 0.05 and <i>p</i><i>&lt;</i>\u20090.01 were significant.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            "p values < 0.05 and p < 0.01 were significant.",
        )
        self.assertEqual(
            block.markdown,
            "<em>p</em> values &lt; 0.05 and "
            "<em>p</em> <em>&lt;</em> 0.01 were significant.",
        )

    def test_springer_reading_companion_figure_duplicates_are_excluded(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer reading companion</h1>
<main><section><h2>Results</h2><p>Body.</p>
<div id="figure-1"><figure>
  <figcaption><b id="Fig1">Fig. 1</b></figcaption>
  <div><picture><img aria-describedby="figure-1-desc"
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Fig. 1"></picture>
    <div id="figure-1-desc"><p>Complete authored caption.</p></div></div>
</figure></div></section></main>
<aside aria-label="reading companion"><div>
  <div id="tabpanel-figures" role="tabpanel"><ul><li><figure>
    <figcaption><b id="rc-Fig1">Fig. 1</b></figcaption>
    <picture></picture><p><a>View in article</a><a>Full size image</a></p>
  </figure></li></ul></div>
</div></aside></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Fig. 1 Complete authored caption.",
        )
        self.assertEqual(len(result.embedded_assets), 1)

    def test_springer_table_link_wrapper_is_not_a_scientific_figure(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer table link wrapper</h1>
<section><h2>Results</h2><p>Body.</p>
<div id="table-1"><figure>
  <figcaption><b id="Tab1">Table 1 Authored values.</b></figcaption>
  <div><a rel="nofollow" href="/articles/s12345-001-00001-0/tables/1"
    aria-label="Full size table 1"><span>Full size table</span></a></div>
</figure></div>
<div id="table-2"><figure>
  <figcaption><b id="Tab2">Table 2 More authored values.</b></figcaption>
  <div><a rel="nofollow" href="/article/10.1007/s00109-013-1118-x/tables/2"
    aria-label="Full size table 2"><span>Full size table</span></a></div>
</figure></div>
<div id="table-3"><figure>
  <figcaption><b id="Tab3">Table 4.3 Chapter values.</b></figcaption>
  <div><a rel="nofollow" href="/chapter/10.1007/978-1-234-56789-0_4/tables/3"
    aria-label="Full size table 3"><span>Full size table</span></a></div>
</figure></div>
<div id="figure-1"><figure id="figure-1">
  <figcaption><b id="Fig1">Figure 1</b></figcaption>
  <picture><img aria-describedby="figure-1-desc"
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Figure 1"></picture>
  <div id="figure-1-desc"><p>Authored figure caption.</p></div>
</figure></div></section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(result.figures[0].caption_plain, "Figure 1 Authored figure caption.")
        self.assertEqual(len(result.embedded_assets), 1)

    def test_springer_machine_suggested_related_subjects_widget_is_excluded(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Springer widget</h1>
<section><h2>Abstract</h2><p>Authored abstract.</p></section>
<section aria-labelledby="content-related-subjects">
  <h3 id="content-related-subjects">Explore related subjects</h3>
  <span>Discover the latest articles, books and news in related subjects, suggested using machine learning.</span>
  <ul role="list"><li><a href="/subjects/chromatin">Chromatin</a></li></ul>
</section><section><h2>Results</h2><p>Authored result.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Results"],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertNotIn("machine learning", visible)
        self.assertNotIn("Chromatin", visible)

    def test_springer_related_subjects_near_miss_is_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Authored subjects</h1>
<section aria-labelledby="content-related-subjects">
  <h3 id="content-related-subjects">Explore related subjects</h3>
  <p>Authors selected these experimental themes.</p>
  <ul role="list"><li><a href="/subjects/chromatin">Chromatin</a></li></ul>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].heading, "Explore related subjects")
        self.assertIn(
            "Authors selected these experimental themes.",
            result.sections[0].blocks[0].plain_text,
        )

    def test_reading_companion_near_miss_preserves_authored_aside_figure(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Authored aside</h1><aside aria-label="reading companion">
<div id="tabpanel-authored" role="tabpanel"><figure>
  <figcaption><b id="Fig1">Fig. 1</b></figcaption>
  <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Fig. 1">
</figure></div></aside></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")

    def test_semantic_caption_does_not_follow_external_aria_description(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scoped caption</h1>
<section><h2>Results</h2><p>Body.</p>
<figure id="figure-1">
  <figcaption><b id="Fig1">Fig. 1: Local title.</b></figcaption>
  <img aria-describedby="outside-caption"
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Figure 1">
</figure>
<div id="outside-caption"><p>Unrelated external prose.</p></div>
</section></article></body></html>"""
        )

        self.assertEqual(result.figures[0].caption_plain, "Fig. 1: Local title.")
        self.assertNotIn("Unrelated", result.figures[0].caption_markdown)

    def test_springer_split_references_remove_only_exact_linkout_toolbar(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer references</h1><section><h2>References</h2><ol>
<li><p id="ref-CR1">Alpha A. A <i>FOXO1</i> study. Journal. 2023;1:1–2.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/cas/alpha">CAS</a><a href="/pubmed/alpha">PubMed</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li>
<li><p id="ref-CR2">Beta B. A β-cell study. Journal. 2024;2:3–4.</p>
  <p><a href="https://link.springer.com/doi/10.1000%2Fbeta">Article</a>
  <a href="/pubmed/beta">PubMed</a><a href="/pmc/beta">PubMed Central</a>
  <a href="/scholar/beta">Google Scholar</a></p></li>
</ol></section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 2)
        first, second = result.references
        self.assertEqual(
            first.plain_text,
            "1. Alpha A. A FOXO1 study. Journal. 2023;1:1–2. "
            "DOI: https://doi.org/10.1000/alpha",
        )
        self.assertIn("<em>FOXO1</em>", first.markdown)
        self.assertIn("DOI: https://doi.org/10.1000/beta", second.plain_text)
        for reference in result.references:
            self.assertNotIn("Article", reference.plain_text)
            self.assertNotIn("Google Scholar", reference.plain_text)
            self.assertNotIn("PubMed", reference.plain_text)

    def test_springer_unordered_doi_less_reference_toolbar_is_removed(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer DOI-less references</h1><section><h2>References</h2><ul>
<li><p id="ref-CR1">Alpha A. Authored citation.</p><p>
  <a aria-label="CAS reference 1" href="/cas/alpha">CAS</a>
  <a aria-label="PubMed reference 1" href="/pubmed/alpha">PubMed</a>
  <a aria-label="Google Scholar reference 1" href="/scholar/alpha">Google Scholar</a>
</p></li><li><p id="ref-CR2">Beta B. Authored citation.</p><p>
  <a aria-label="Google Scholar reference 2" href="/scholar/beta">Google Scholar</a>
</p></li>
</ul></section></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Alpha A. Authored citation.",
                "2. Beta B. Authored citation.",
            ],
        )

    def test_springer_unlabelled_exact_scholar_query_toolbar_is_removed(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Springer Scholar reference</h1><section><h2>References</h2><ol>
<li><p id="ref-CR1">Alpha A. Exact authored citation. Journal 1:2–3</p><p>
  <a href="https://scholar.google.com/scholar?&amp;q=Alpha%20A.%20Exact%20authored%20citation.%20Journal%201%3A2%E2%80%933">Google Scholar</a>
</p></li>
</ol></section></article></body></html>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Alpha A. Exact authored citation. Journal 1:2–3"],
        )

    def test_springer_direct_anchor_reference_toolbar_is_removed(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Current Springer references</h1><section><h2>References</h2><ol>
<li><p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a></p>
  <div id="libkey-nomad-10-1000-alpha-0"><div></div></div>
  <a aria-label="CAS reference 1" href="/cas/alpha">CAS</a>
  <a aria-label="PubMed reference 1" href="/pubmed/alpha">PubMed</a>
  <a aria-label="Google Scholar reference 1" href="/scholar/alpha">Google Scholar</a>
</li><li><p id="ref-CR2">Beta B. Citation without publisher linkouts.</p></li>
</ol></section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 2)
        self.assertEqual(
            result.references[0].plain_text,
            "1. Alpha A. Authored citation. DOI: https://doi.org/10.1000/alpha",
        )
        self.assertEqual(
            result.references[1].plain_text,
            "2. Beta B. Citation without publisher linkouts.",
        )
        self.assertNotIn("Google Scholar", result.references[0].markdown)

    def test_springer_direct_anchor_reference_near_miss_is_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Current Springer near miss</h1><section><h2>References</h2><ol>
<li><p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a></p>
  <div id="libkey-nomad-10-1000-alpha-0"><div></div></div>
  <a aria-label="Author note reference 1" href="/note/alpha">Author note</a>
  <a aria-label="Google Scholar reference 1" href="/scholar/alpha">Google Scholar</a>
</li></ol></section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertIn("Author note", result.references[0].plain_text)

    def test_springer_split_reference_near_miss_preserves_second_paragraph(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Near-miss reference</h1><section><h2>References</h2><ol>
<li><p id="ref-CR1">Alpha A. Authored citation.</p>
  <p>Author correction note.
  <a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li>
</ol></section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertIn("Author correction note.", result.references[0].plain_text)

    def test_springer_authored_doi_with_nomad_and_scholar_control_is_recovered(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Springer DOI reference</h1>
<section><h2>References</h2><ol><li>
<p id="ref-CR1">Alpha A. Authored citation. DOI:
  <a href="https://doi.org/10.1000/alpha">10.1000/alpha</a></p>
<div id="libkey-nomad-10-1000-alpha-0"><div><div></div></div></div>
<p><a aria-label="Google Scholar reference 1" href="/scholar/alpha">
  Google Scholar</a></p>
</li></ol></section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertIn("DOI: 10.1000/alpha", result.references[0].plain_text)
        self.assertNotIn("Google Scholar", result.references[0].plain_text)

    def test_springer_post_reference_authored_back_matter_is_recovered(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Springer back matter</h1>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a aria-label="ADS reference 1">ADS</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></section>
<section><h2>Acknowledgements</h2><p>Funded work.</p></section>
<section><h2>Author information</h2><div>
  <h3>Authors and Affiliations</h3>
  <ol><li id="Aff1"><p>Research Institute, Japan.</p><p>Alpha A.</p></li></ol>
  <div><ol><li id="auth-Alpha-A-Aff1"><span>Alpha A.</span><div>
    <div>View author publications</div><div>Search author on:<span>
    <a>PubMed</a> <a>Google Scholar</a></span></div></div></li></ol></div>
  <h3>Contributions</h3><p>AA designed the study.</p>
  <h3>Corresponding author</h3><p>Correspondence to Alpha A.</p>
</div></section>
<section><h2>Ethics declarations</h2><div>
  <h3>Competing interests</h3><p>No competing interests.</p>
  <h3>Ethics approval and consent to participate</h3><p>Approved.</p>
</div></section>
<section><h2>Additional information</h2><p>Publisher’s note retained.</p></section>
<section><h2>Supplementary information</h2><h3>Download DOCX</h3></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Acknowledgements",
                "Author information",
                "Authors and Affiliations",
                "Contributions",
                "Corresponding author",
                "Ethics declarations",
                "Competing interests",
                "Ethics approval and consent to participate",
                "Additional information",
            ],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("Research Institute, Japan. Alpha A.", visible)
        self.assertIn("AA designed the study.", visible)
        self.assertIn("Publisher’s note retained.", visible)
        self.assertNotIn("View author publications", visible)
        self.assertNotIn("Publisher interface", visible)
        self.assertNotIn("Download DOCX", visible)
        self.assertNotIn("ADS", result.references[0].plain_text)

    def test_springer_legacy_singular_acknowledgment_back_matter_is_recovered(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Legacy Springer back matter</h1>
<section><h2>Results</h2><p>Result.</p></section>
<section aria-labelledby="Bib1"><div id="Bib1-section"><h2 id="Bib1">References</h2>
<div id="Bib1-content"><div><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></div></div></div></section>
<section><h2>Acknowledgment</h2><p>Funded work.</p>
  <h3>Conflict of interest</h3><p>No conflict.</p></section>
<section><h2>Author information</h2><p>Authored affiliation.</p></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
<section><h2>About this article</h2><p>Publisher interface.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Results", "Acknowledgment", "Conflict of interest", "Author information"],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("Funded work.", visible)
        self.assertIn("No conflict.", visible)
        self.assertIn("Authored affiliation.", visible)
        self.assertNotIn("Publisher interface.", visible)

    def test_springer_electronic_supplementary_material_boundary_recovers_back_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Archived Springer variant</h1>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></section>
<section><h2>Acknowledgements</h2><p>Funded work.</p></section>
<section><h2>Author information</h2>
  <h3>Contributions</h3><p>AA designed the study.</p></section>
<section><h2>Ethics declarations</h2>
  <h3>Competing Interests</h3><p>No competing interests.</p></section>
<section><h2>Additional information</h2><p>Publisher note retained.</p></section>
<section><h2>Electronic supplementary material</h2>
  <h3>Supplementary information (download PDF)</h3></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Acknowledgements",
                "Author information",
                "Contributions",
                "Ethics declarations",
                "Competing Interests",
                "Additional information",
            ],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("Funded work.", visible)
        self.assertIn("AA designed the study.", visible)
        self.assertNotIn("download PDF", visible)
        self.assertNotIn("Publisher interface", visible)

    def test_springer_additional_information_before_references_recovers_back_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Archived Nature variant</h1>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>Additional information</h2><p>How to cite this article.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></section>
<section><h2>Acknowledgements</h2><p>Funded work.</p></section>
<section><h2>Author information</h2>
  <h3>Authors and Affiliations</h3><p>Research Institute, Japan.</p>
  <h3>Contributions</h3><p>AA designed the study.</p></section>
<section><h2>Ethics declarations</h2>
  <h3>Competing interests</h3><p>No competing interests.</p></section>
<section><h2>Supplementary information</h2><h3>Download PDF</h3></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Additional information",
                "Acknowledgements",
                "Author information",
                "Authors and Affiliations",
                "Contributions",
                "Ethics declarations",
                "Competing interests",
            ],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("How to cite this article.", visible)
        self.assertIn("Funded work.", visible)
        self.assertIn("AA designed the study.", visible)
        self.assertNotIn("Download PDF", visible)
        self.assertNotIn("Publisher interface", visible)

    def test_archived_springer_header_metadata_subjects_and_back_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<header><ul><li>Article</li><li><a>Open access</a></li>
  <li>Published: <time datetime="2019-08-05">05 August 2019</time></li></ul>
<h1>Archived Springer article</h1>
<ul><li><a href="#auth-Alpha-A-Aff1">Alpha A.</a><span>
  <a href="https://orcid.org/0000-0001-2345-6789"><span>ORCID: </span>
  orcid.org/0000-0001-2345-6789</a></span></li></ul>
<p><a><i>Example Journal</i></a> <b><span>volume</span> 9</b>,
Article number: <span>12345</span> (<span>2019</span>)
<a href="#citeas">Cite this article</a></p></header>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a aria-label="ADS reference 1">ADS</a>
  <a aria-label="Google Scholar reference 1">Google Scholar</a></p>
</li></ol></section>
<section><h2>Acknowledgements</h2><p>Funded work.</p></section>
<section><h2>Author information</h2>
  <h3>Authors and Affiliations</h3><ol><li id="Aff1"><p>Institute.</p>
  <p>Alpha A.</p></li></ol>
  <h3>Contributions</h3><p>AA designed the study.</p>
  <h3 id="corresponding-author">Corresponding author</h3>
  <p id="corresponding-author-list">Correspondence to
  <a aria-label="email Alpha A." href="mailto:alpha@example.test">Alpha A.</a>.</p>
</section>
<section><h2>Ethics declarations</h2><h3>Competing interests</h3>
  <p>No competing interests.</p></section>
<section><h2>Additional information</h2><p>Publisher note.</p></section>
<section><h2>Supplementary information</h2><h3>Download DOCX</h3></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
<div id="article-info-section"><h2 id="article-info">About this article</h2>
<div><h3 id="citeas">Cite this article</h3><ul>
  <li><p>Received: <time datetime="2019-02-20">20 February 2019</time></p></li>
  <li><p>Accepted: <time datetime="2019-07-18">18 July 2019</time></p></li>
  <li><p>Published: <time datetime="2019-08-05">05 August 2019</time></p></li>
  <li><p>Version of record: <time datetime="2019-08-05">05 August 2019</time></p></li>
</ul><h3>Keywords</h3><ul><li>AML</li><li>Polyamide</li></ul>
<h3>Subjects</h3><ul><li>Cancer screening</li><li>Diagnostics</li></ul>
</div></div>
</article></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "9",
                "article_number": "12345",
                "date": "05 August 2019",
                "first_published": "05 August 2019",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "ORCID — Alpha A.: 0000-0001-2345-6789",
                "Correspondence: Alpha A. — alpha@example.test",
                "Article history: Received: 20 February 2019; "
                "Accepted: 18 July 2019; Published: 05 August 2019; "
                "Version of record: 05 August 2019",
                "Keywords: AML; Polyamide",
                "Subjects: Cancer screening; Diagnostics",
            ],
        )
        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Acknowledgements",
                "Author information",
                "Authors and Affiliations",
                "Contributions",
                "Corresponding author",
                "Ethics declarations",
                "Competing interests",
                "Additional information",
            ],
        )
        self.assertEqual(
            result.references[0].plain_text,
            "1. Alpha A. Authored citation. DOI: https://doi.org/10.1000/alpha",
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertNotIn("Download DOCX", visible)
        self.assertNotIn("Publisher interface", visible)
        self.assertNotIn("ADS", result.references[0].plain_text)

    def test_springer_post_reference_sequence_allows_authored_funding(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Springer funding</h1>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></section>
<section><h2>Acknowledgements</h2><p>Thanks.</p></section>
<section><h2>Funding</h2><p>Grant 123.</p></section>
<section><h2>Author information</h2><p>Author details.</p></section>
<section><h2>Ethics declarations</h2><p>None.</p></section>
<section><h2>Additional information</h2><p>Publisher note.</p></section>
<section><h2>Supplementary information</h2><h3>Download TIFF</h3></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Acknowledgements",
                "Funding",
                "Author information",
                "Ethics declarations",
                "Additional information",
            ],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("Grant 123.", visible)
        self.assertNotIn("Download TIFF", visible)
        self.assertNotIn("Publisher interface", visible)

    def test_springer_no_supplement_retains_back_matter_header_and_keywords(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<section><div><div><div><nav>Home Example Journal Article</nav>
<h1>Springer no-supplement article</h1>
<ul><li>Original Research</li><li>Published: 26 February 2020</li></ul>
<ul><li>Volume 29, pages 607–616 (2020)</li><li>Cite this article</li></ul>
</div></div></div></section>
<header><ul><li>Alpha A. <span><a href="https://orcid.org/0000-0001-5480-2074">
ORCID: orcid.org/0000-0001-5480-2074</a></span><sup>1</sup></li></ul></header>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Authored citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></section>
<section><h2>Acknowledgements</h2><p>Funded work.</p></section>
<section><h2>Author information</h2><p>Author details.</p>
<h3 id="corresponding-author">Corresponding author</h3>
<p id="corresponding-author-list">Correspondence to
<a aria-label="email Alpha A." href="mailto:alpha@example.test">Alpha A.</a>.</p>
</section>
<section><h2>Ethics declarations</h2><h3>Conflict of interest</h3>
<p>No conflict.</p></section>
<section><h2>Additional information</h2><p>Publisher note retained.</p></section>
<section><h2>Rights and permissions</h2><p>Publisher interface.</p></section>
<section><div id="article-info-section"><h2 id="article-info">About this article</h2>
<div><ul>
<li><p>Received: <time datetime="2019-11-05">05 November 2019</time></p></li>
<li><p>Accepted: <time datetime="2019-12-19">19 December 2019</time></p></li>
<li><p>Published: <time datetime="2020-02-26">26 February 2020</time></p></li>
<li><p>Version of record: <time datetime="2020-02-26">26 February 2020</time></p></li>
<li><p>Issue date: <time datetime="2020-04">April 2020</time></p></li>
</ul><h3>Keywords</h3><ul><li>ERRα</li><li>Polyamide</li></ul></div>
</div></section>
</article></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "date": "26 February 2020",
                "first_published": "26 February 2020",
                "pages": "607–616",
                "volume": "29",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "ORCID — Alpha A.: 0000-0001-5480-2074",
                "Correspondence: Alpha A. — alpha@example.test",
                "Article history: Received: 05 November 2019; "
                "Accepted: 19 December 2019; Published: 26 February 2020; "
                "Version of record: 26 February 2020; Issue date: April 2020",
                "Keywords: ERRα; Polyamide",
            ],
        )
        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Results",
                "Acknowledgements",
                "Author information",
                "Corresponding author",
                "Ethics declarations",
                "Conflict of interest",
                "Additional information",
            ],
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("Publisher note retained.", visible)
        self.assertNotIn("Publisher interface.", visible)

    def test_post_reference_near_miss_remains_terminal(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Near miss</h1>
<section><h2>Results</h2><p>Result.</p></section>
<section><h2>References</h2><ol><li>
  <p id="ref-CR1">Alpha A. Citation.</p>
  <p><a href="https://doi.org/10.1000%2Falpha">Article</a>
  <a href="/scholar/alpha">Google Scholar</a></p></li></ol></section>
<section><h2>Acknowledgements</h2><p>Not exact sequence.</p></section>
<section><h2>Additional information</h2><p>Interface.</p></section>
</article></body></html>"""
        )

        self.assertEqual([section.heading for section in result.sections], ["Results"])

    def test_list_block_children_gain_only_their_missing_boundary_space(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>List spacing</h1>
<section><h2>Author details</h2><ol>
  <li><p>Institute, Japan.</p><p><em>Alpha</em> A.</p></li>
  <li><span>Authored</span><span>adjacency</span></li>
</ol></section></article></body></html>"""
        )

        value = result.sections[0].blocks[0]
        self.assertIn("Institute, Japan. Alpha A.", value.plain_text)
        self.assertIn("Institute, Japan. <em>Alpha</em> A.", value.markdown)
        self.assertIn("Authoredadjacency", value.plain_text)

    def test_sciencedirect_bib_sref_references_preserve_authored_labels_and_ui_rules(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>New ScienceDirect reference dialect</h1>
<section><h2>References</h2><ol>
<li><span><a href="#bbib1" id="ref-id-bib1">Abend et al., 2007</a></span>
  <span id="sref1"><div><div>J.R. Abend, J.A. Low, M.J. Imperiale</div>
    <div id="ref-id-sref1">Inhibitory effect on viral replication</div></div>
    <div>J. Virol., 81 (2007), pp. 272-279,
      <a href="https://doi.org/10.1000/example">10.1000/example</a></div>
    <div lang="en"><a href="https://scopus.example/">View in Scopus</a>
      <a href="https://scholar.example/">Google Scholar</a></div></span></li>
<li><span><a href="#bbib2" id="ref-id-bib2">2</a></span>
  <span id="sref2"><div><div>Numeric authors</div>
    <div id="ref-id-sref2">Numeric-title study</div></div>
    <div>Journal Two, 2 (2008), pp. 10-20</div></span></li>
<li><span><a href="#bbib3" id="ref-id-bib3">Verhalen et al., 2015</a></span>
  <span id="sref3"><div><div>B. Verhalen, J.L. Justic[Truncated]</div></div></span></li>
</ol></section></article></body></html>"""
        )

        self.assertEqual(
            [reference.block_id for reference in result.references],
            ["reference-001", "reference-002", "reference-003"],
        )
        first, second, third = result.references
        self.assertEqual(
            first.plain_text,
            (
                "Abend et al., 2007. J.R. Abend, J.A. Low, M.J. Imperiale "
                "Inhibitory effect on viral replication J. Virol., 81 (2007), "
                "pp. 272-279, 10.1000/example DOI: "
                "https://doi.org/10.1000/example"
            ),
        )
        self.assertNotIn("View in Scopus", first.plain_text)
        self.assertNotIn("Google Scholar", first.plain_text)
        self.assertEqual(
            first.plain_text.count("https://doi.org/10.1000/example"), 1
        )
        self.assertEqual(
            first.markdown.count("DOI: https://doi.org/10.1000/example"), 1
        )
        self.assertEqual(
            second.plain_text,
            "2. Numeric authors Numeric-title study Journal Two, 2 (2008), pp. 10-20",
        )
        self.assertIn("[Truncated]", third.plain_text)
        self.assertTrue(third.plain_text.startswith("Verhalen et al., 2015. "))
        for reference in result.references:
            safe_html = block_markup_to_safe_html(
                reference.markdown, kind="reference"
            )
            self.assertEqual(
                plain_text_from_safe_html(safe_html), reference.plain_text
            )
            self.assertTrue(
                rich_text_matches_plain(reference.plain_text, safe_html)
            )

    def test_sciencedirect_references_and_notes_heading_is_bibliography(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect references and notes</h1>
<section><h2>Results</h2><p>Authored body.</p></section>
<section id="bi005"><h2>References and notes</h2><section><ol>
<li><span><a href="#bb0005" id="ref-id-b0005"><span>1</span></a></span>
<span id="h0005"><div><div>Alpha A.</div></div>
<div id="ref-id-h0005">Journal One, 1 (2001), p. 1</div>
<div lang="en"><a href="https://scopus.example/">View in Scopus</a></div></span></li>
</ol></section></section></article></body></html>"""
        )

        self.assertEqual([section.heading for section in result.sections], ["Results"])
        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            "1. Alpha A. Journal One, 1 (2001), p. 1",
        )
        self.assertNotIn("Scopus", result.references[0].plain_text)

    def test_old_sciencedirect_reference_keeps_all_subentries_and_note_body(self) -> None:
        conventional = lxml_html.fromstring(
            """<li><span><a id="ref-id-b0010"><span>2</span></a></span>
            <span id="h0010"><div><div>Alpha A.</div><div>Journal 1.</div></div>
            <div lang="en"><a href="https://example.invalid">View in Scopus</a></div></span>
            <span id="h0015"><div><div>Beta B.</div><div>Journal 2.</div></div>
            <div lang="en"><a href="https://example.invalid">Google Scholar</a></div></span>
            </li>"""
        )
        note = lxml_html.fromstring(
            """<li><span><a id="ref-id-b0045"><span>9</span></a></span>
            <div><div id="sp0015">Authored note with <strong>compound 1</strong>.</div>
            <figure><img src="data:image/png;base64,AA=="><ol><li>Download image</li></ol></figure>
            </div></li>"""
        )

        conventional_rendered, conventional_plain = (
            _sciencedirect_reference(conventional) or ("", "")
        )
        note_rendered, note_plain = _sciencedirect_reference(note) or ("", "")

        self.assertEqual(
            conventional_plain,
            "2. Alpha A. Journal 1. Beta B. Journal 2.",
        )
        self.assertNotIn("Scopus", conventional_plain)
        self.assertNotIn("Scholar", conventional_plain)
        self.assertEqual(note_plain, "9. Authored note with compound 1.")
        self.assertNotIn("Download", note_plain)
        self.assertIn("<strong>compound 1</strong>", note_rendered)
        self.assertEqual(
            plain_text_from_safe_html(conventional_rendered), conventional_plain
        )
        self.assertEqual(plain_text_from_safe_html(note_rendered), note_plain)

    def test_old_sciencedirect_unheaded_body_does_not_remain_under_keywords(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
            <h1>Legacy ScienceDirect body</h1>
            <a href="https://doi.org/10.1016/j.example.2017.01.001">DOI</a>
            <div><div id="kg005"><h2>Keywords</h2><div>Copper</div></div></div>
            <div id="body"><div><div id="p0005">First unheaded body paragraph.</div>
              <div id="p0010">Second unheaded body paragraph.</div></div></div>
            <section><h2>References</h2><ol>
              <li><span><a id="ref-id-b0005"><span>1</span></a></span>
                <div><div id="sp0005">Author A. Source.</div></div>
              </li>
            </ol></section>
            </article></body></html>"""
        )

        main_text = [
            section for section in result.sections if section.heading == "Main text"
        ]
        self.assertEqual(len(main_text), 1)
        self.assertEqual(
            [block.plain_text for block in main_text[0].blocks],
            [
                "First unheaded body paragraph.",
                "Second unheaded body paragraph.",
            ],
        )
        keywords = [
            section for section in result.sections if section.heading == "Keywords"
        ]
        self.assertTrue(keywords)
        self.assertNotIn(
            "First unheaded body paragraph.",
            " ".join(block.plain_text for block in keywords[0].blocks),
        )

    def test_old_sciencedirect_reference_artwork_is_not_renumbered_as_a_figure(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
            <h1>Reference artwork</h1><section><h2>References</h2><ol>
            <li><span><a id="ref-id-b0045"><span>9</span></a></span>
              <div><div id="sp0015">Authored chemical note.</div>
                <figure id="f0045"><img
                  src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
                  <ol><li><a download href="image.jpg">Download image</a></li></ol>
                </figure>
              </div>
            </li></ol></section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.references[0].plain_text, "9. Authored chemical note.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "reference_009_artwork")
        self.assertEqual(result.figures[0].label, "Reference 9 artwork")

    def test_sciencedirect_reference_requires_matching_id_suffixes(self) -> None:
        item = lxml_html.fromstring(
            """<li><span><a id="ref-id-bib4">4</a></span>
<span id="sref5"><div>Mismatched content.</div>
<div lang="en"><a href="https://scholar.example/">Google Scholar</a></div></span></li>"""
        )

        self.assertIsNone(_sciencedirect_reference(item))

    def test_sciencedirect_bib_sbref_reference_is_exactly_recognized(self) -> None:
        item = lxml_html.fromstring(
            """<li><span><a id="ref-id-bib4">Example 2022</a></span>
<span id="sbref4"><div><div>A. Example</div>
<div id="ref-id-sbref4">An exact archived reference</div></div>
<div>Journal, 4 (2022), pp. 1-2</div>
<div lang="en"><a href="https://scopus.example/">View in Scopus</a>
<a href="https://scholar.example/">Google Scholar</a></div></span></li>"""
        )

        rendered, visible = _sciencedirect_reference(item) or ("", "")
        self.assertEqual(
            visible,
            "Example 2022. A. Example An exact archived reference Journal, 4 (2022), pp. 1-2",
        )
        self.assertEqual(rendered, visible)
        self.assertNotIn("View in Scopus", visible)
        self.assertNotIn("Google Scholar", visible)

        near_miss = lxml_html.fromstring(
            """<li><span><a id="ref-id-bib4">Example 2022</a></span>
<span id="sbref5"><div>A mismatched archived reference.</div></span></li>"""
        )
        self.assertIsNone(_sciencedirect_reference(near_miss))

    def test_sciencedirect_supplement_download_ui_is_exactly_excluded(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect supplement UI</h1>
<section><h2>Supplemental Materials</h2><div id="p0205">
<em>Authored supplementary note.</em><span><span id="ec1"><span>
<a href="https://ars.els-cdn.com/content/image/1-s2.0-example-mmc1-sup.pdf"
 download="" title="Download Acrobat PDF file (1MB)">Download: Download Acrobat PDF file (1MB)</a>
</span></span></span></div></section>
<section><h2>Data availability</h2><p>
<a href="https://ars.els-cdn.com/content/image/1-s2.0-example-mmc1-sup.pdf"
 download="" title="Download Acrobat PDF file (1MB)">Authored download link.</a>
</p></section></article></body></html>"""
        )

        self.assertEqual(result.sections[0].blocks[0].plain_text, "Authored supplementary note.")
        self.assertEqual(
            result.sections[1].blocks[0].plain_text, "Authored download link."
        )

    def test_bibliographic_fallback_excludes_structural_reference_ranges(self) -> None:
        reference_only = self.extract(
            """<!doctype html><html><body><article>
<h1>Reference-only page ranges</h1>
<section><h2>Results</h2><p>Body prose without article-level pagination.</p></section>
<section><h2>References</h2><ol><li>
<span><a id="ref-id-bib1">Prior et al., 2007</a></span>
<span id="sref1"><div>J. Virol., 81 (2007), pp. 272-279</div></span>
</li><li><span><a id="ref-id-b0002">[2]</a></span>
<span id="h0002"><div>Journal Two, 2 (2008), pp. 10-20</div></span></li>
</ol></section></article></body></html>"""
        )
        legitimate_front_matter = self.extract(
            """<!doctype html><html><body>
<div>Pages: 68-75</div><article><h1>Fallback pages</h1>
<section><h2>References</h2><ol><li>Prior study, pp. 272-279.</li></ol></section>
</article></body></html>"""
        )
        explicit_publication = self.extract(
            """<!doctype html><html><body>
<div>Pages: 1-2</div>
<div id="publication">Volume 152, Issue 4, 16 February 2018, Pages 68-75</div>
<article><h1>Explicit pages win</h1>
<section><h2>References</h2><ol><li>Prior study, pp. 272-279.</li></ol></section>
</article></body></html>"""
        )

        self.assertNotIn("pages", reference_only.bibliographic)
        self.assertEqual(legitimate_front_matter.bibliographic["pages"], "68-75")
        self.assertEqual(explicit_publication.bibliographic["pages"], "68-75")

    def test_sciencedirect_cap_table_title_is_scoped_and_lower_precedence(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect table captions</h1>
<span id="cap-outside"><p>Table 4. Unrelated outside caption.</p></span>
<div id="tbl1"><span><span id="cap0045"><p id="tspara0010">
  <span>Table 1</span>. Antiviral <i>β</i> activity (with variance).</p></span></span>
  <table><thead><tr><th>Agent</th><th>IC50</th></tr></thead>
  <tbody><tr><td>PA1</td><td>100 (±15)</td></tr></tbody></table>
  <ul><li id="note-a">[a] Authored table note.</li></ul></div>
<div id="tbl2"><header>Table 2. Header title.</header>
  <span id="cn0020"><p>Table 2. CN title.</p></span>
  <span id="cap0020"><p>Table 2. CAP title.</p></span>
  <table><tr><th>H</th></tr><tr><td>2</td></tr></table></div>
<div id="tbl3"><span id="cn0030"><p>Table 3. CN title.</p></span>
  <span id="cap0030"><p>Table 3. CAP title.</p></span>
  <table><tr><th>H</th></tr><tr><td>3</td></tr></table></div>
<div id="tbl4"><table><tr><th>H</th></tr><tr><td>4</td></tr></table></div>
</article></body></html>"""
        )

        self.assertEqual(len(result.tables), 4)
        self.assertEqual(
            [table.title_plain for table in result.tables],
            [
                "Table 1. Antiviral β activity (with variance).",
                "Table 2. Header title.",
                "Table 3. CN title.",
                "Table 4",
            ],
        )
        first = result.tables[0]
        self.assertEqual(
            first.title_markdown,
            "Table 1. Antiviral <em>β</em> activity (with variance).",
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in first.parts[0].rows],
            [["Agent", "IC50"], ["PA1", "100 (±15)"]],
        )
        self.assertEqual(first.footnotes_plain, ["[a] Authored table note."])
        safe_html = block_markup_to_safe_html(
            first.title_markdown, kind="table_title"
        )
        self.assertEqual(plain_text_from_safe_html(safe_html), first.title_plain)
        self.assertTrue(rich_text_matches_plain(first.title_plain, safe_html))

    def test_archived_sciencedirect_spara_table_caption_and_note_are_kept(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Archived ScienceDirect semantic table</h1>
<section><figure id="tbl1"><div><table>
  <thead><tr><th>Agent</th><th>K<sub>D</sub></th></tr></thead>
  <tbody><tr><td>PA1</td><td>2 × 10<sup>−9</sup></td></tr></tbody>
</table></div><figcaption>
  <div><span>Table 1</span><div id="spara80" role="paragraph">
    Binding constants for synthetic agents</div></div>
  <div><div id="spara90" role="paragraph"><i>Abbreviation:</i>
    K<sub><sub>D</sub></sub>, dissociation equilibrium constant. Values are mean ± SD.
  </div></div>
  <div><ul><li><a href="/action/showFullTableHTML?isHtml=true&amp;tableId=tbl1">
    Open table in a new tab</a></li></ul></div>
</figcaption></figure></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            table.title_plain,
            "Table 1. Binding constants for synthetic agents",
        )
        self.assertEqual(
            table.footnotes_plain,
            [
                "Abbreviation: K_{D}, dissociation equilibrium constant. "
                "Values are mean ± SD."
            ],
        )
        self.assertEqual(
            table.footnotes_markdown,
            [
                "<em>Abbreviation:</em> K<sub>D</sub>, dissociation "
                "equilibrium constant. Values are mean ± SD."
            ],
        )

    def test_table_definition_list_notes_are_scoped_ordered_and_deduplicated(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Table definition-list notes</h1>
<dl><dt>a</dt><dd>Outside-table affiliation must remain unrelated.</dd></dl>
<div id="tbl1"><span id="cap0005"><p>Table 1. Binding energies.</p></span>
  <table><tr><th>DNA</th><th>K<sub>d,app</sub></th></tr>
    <tr><td>SFTT</td><td>0.0077<sup>b</sup></td></tr></table>
  <ul><li id="note-a">[a] Existing list note.</li></ul>
  <dl>
    <dt>a</dt><dd>Existing list note.</dd>
    <dt>b</dt><dd>Fit <em>error</em> for K<sub>d,app</sub>.</dd>
    <dt>⁎</dt><dd>Authors' star marker.</dd>
    <dt>orphan</dt><dt>c</dt><dd></dd>
  </dl>
  <div><dl><dt>d</dt><dd>Nested unrelated definition.</dd></dl></div>
</div></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] Existing list note.",
                "[b] Fit error for K_{d,app}.",
                "[⁎] Authors' star marker.",
            ],
        )
        self.assertEqual(
            table.footnotes_markdown,
            [
                "[a] Existing list note.",
                "[b] Fit <em>error</em> for K<sub>d,app</sub>.",
                "[⁎] Authors' star marker.",
            ],
        )
        for plain, markup in zip(
            table.footnotes_plain, table.footnotes_markdown, strict=True
        ):
            safe_html = block_markup_to_safe_html(markup, kind="table_footnote")
            self.assertEqual(plain_text_from_safe_html(safe_html), plain)
            self.assertTrue(rich_text_matches_plain(plain, safe_html))

        with TemporaryDirectory() as directory:
            extraction_root = Path(directory)
            write_table_derivatives(result.tables, extraction_root)
            payload = json.loads(
                (extraction_root / "tables/main/table_001.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(
            payload["footnotes_typed"],
            [
                {
                    "label": "a",
                    "scope": "table",
                    "text": "Existing list note.",
                },
                {
                    "label": "b",
                    "scope": "table",
                    "text": "Fit error for K_{d,app}.",
                },
            ],
        )

    def test_wiley_fn_table_notes_are_scoped_and_preserve_rich_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley table notes</h1>
<ul><li id="fn99"><span>z </span>Outside-table note.</li></ul>
<div id="tbl1"><header>Table 1. Ion abundances.</header>
  <table><thead><tr><th>Ion</th><th>Value<a href="#fn1_22">a</a></th></tr></thead>
    <tbody><tr><td>ds<sup>5−</sup></td><td>N.D.</td></tr></tbody></table>
  <div><ul><li id="fn1" title="Footnote 1"><span>a </span>
    N.D. = not <em>detectable</em>.</li></ul></div>
</div></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.footnotes_plain, ["[a] N.D. = not detectable."])
        self.assertEqual(
            table.footnotes_markdown,
            ["[a] N.D. = not <em>detectable</em>."],
        )
        self.assertNotIn("Outside-table note", table.footnotes_plain)

    def test_wiley_note_p_table_note_is_scoped_and_preserves_rich_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley note-p table note</h1>
<ul><li id="note-p-99">Outside-table note.</li></ul>
<div id="tbl1"><header>Table 1. Binding values.</header>
  <table><tr><th>Agent</th><th>Value</th></tr><tr><td>1</td><td>2</td></tr></table>
  <div><ul><li id="note-p-65">[a] Each value is <em>source-authored</em>.</li></ul></div>
</div></article></body></html>"""
        )

        table = result.tables[0]
        self.assertEqual(
            table.footnotes_plain,
            ["[a] Each value is source-authored."],
        )
        self.assertEqual(
            table.footnotes_markdown,
            ["[a] Each value is <em>source-authored</em>."],
        )
        self.assertNotIn("Outside-table note", table.footnotes_plain)

    def test_legacy_wiley_combined_table_notes_are_split_and_scoped(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley combined notes</h1>
<ul><li id="t9_note7"><span id="t9n1"><sup>z</sup> Outside-table note.</span></li></ul>
<section><h2>Results</h2><div id="t2">
  <header><span>Table 2.</span> NOE crosspeaks.</header>
  <div><table><thead><tr><th>N<sub>o</sub><sup>a</sup></th><th>distance<sup>c</sup></th></tr></thead>
    <tbody><tr><td>189</td><td>3.09</td></tr></tbody></table></div>
  <div><ul><li id="t2_note7">
    <span id="t2n4"><sup>a</sup> Experimental intensities; </span>
    <span id="t2n3"><sup>b</sup> calculated intensities; </span>
    <span id="t2n2"><sup>c</sup> distances for d(GAACCGGTTC)</span><sub>2</sub>;
    <span id="t2n1"><sup>d</sup> relative intensities.</span>
  </li></ul></div>
</div></section></article></body></html>"""
        )

        table = result.tables[0]
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] Experimental intensities;",
                "[b] calculated intensities;",
                "[c] distances for d(GAACCGGTTC)_{2};",
                "[d] relative intensities.",
            ],
        )
        self.assertEqual(
            table.footnotes_markdown[2],
            "[c] distances for d(GAACCGGTTC)<sub>2</sub>;",
        )
        self.assertNotIn("Outside-table note", table.footnotes_plain)

    def test_legacy_wiley_separate_table_notes_keep_labels_and_body(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley separate notes</h1>
<ul><li id="t9n9" title="Footnote 9"><span>z </span>Outside note.</li></ul>
<section><h2>Results</h2><div id="t1">
  <header><span>Table 1.</span> Structural statistics.</header>
  <div><table><tr><th>Statistic</th><th>Value</th></tr>
    <tr><td>NOE deviations<sup>a</sup></td><td>0.300 Å</td></tr></table></div>
  <div><ul>
    <li id="t1n6" title="Footnote 1"><span>a </span>
      Pairwise distance deviations between refined structures.</li>
    <li id="t1n5" title="Footnote 2"><span>b </span>
      <sup>b</sup> excluding hydrogen atoms.</li>
  </ul></div>
</div></section></article></body></html>"""
        )

        self.assertEqual(
            result.tables[0].footnotes_plain,
            [
                "[a] Pairwise distance deviations between refined structures.",
                "[b] excluding hydrogen atoms.",
            ],
        )
        self.assertNotIn("Outside note", result.tables[0].footnotes_plain)

    def test_wiley_prefixed_unmarked_table_note_is_scoped(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley unmarked table note</h1>
<ul><li id="cbic202100533-tnote-9999">Outside-table note.</li></ul>
<div id="cbic202100533-tbl-0002"><header>Table 2. Binding values.</header>
  <table><tr><th>Agent</th><th>Value</th></tr><tr><td>1</td><td>2</td></tr></table>
  <div><ul><li id="cbic202100533-tnote-0001">All values are determined by
    fitting with a 1 : 1 binding model.</li></ul></div>
</div></article></body></html>"""
        )

        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["All values are determined by fitting with a 1: 1 binding model."],
        )

    def test_cancer_science_wiley_semantic_table_keeps_caption_and_note(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Cancer Science Wiley table</h1>
<ul><li id="cas14785-note-9999">Outside-table note.</li></ul>
<section><h2>Results</h2><div id="cas14785-tbl-0001"><div>
  <table><caption><span>TABLE 1.</span> Binding affinity.</caption>
    <thead><tr><th>PI polyamide</th><th><i>K</i><sub>D</sub> (M)</th></tr></thead>
    <tbody><tr><td rowspan="2">P3AE5K-Dp</td><td>3.78 × 10<sup>−5</sup></td></tr>
    <tr><td>6.67 × 10<sup>−7</sup></td></tr></tbody></table>
  </div><div><ul><li id="cas14785-note-0001">Abbreviations:
    <i>K</i><sub>D</sub>, dissociation equilibrium constant.</li></ul></div>
</div></section></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "cas14785-tbl-0001")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.title_plain, "TABLE 1. Binding affinity.")
        self.assertEqual(table.parts[0].rows[0][1].text, "K_{D} (M)")
        self.assertEqual(table.parts[0].rows[1][0].rowspan, 2)
        self.assertEqual(table.parts[0].rows[1][1].text, "3.78 × 10^{−5}")
        self.assertEqual(
            table.footnotes_plain,
            ["Abbreviations: K_{D}, dissociation equilibrium constant."],
        )
        self.assertNotIn("Outside-table note", table.footnotes_plain)

        near_miss = self.extract(
            """<!doctype html><html><body><article><h1>Near miss</h1>
<section><h2>Results</h2><div id="cas1478-tbl-0001">
<table><caption>Table 1. Not exact.</caption><tr><td>1</td></tr></table>
</div></section></article></body></html>"""
        )
        self.assertEqual(near_miss.tables, [])

    def test_jmr_wiley_semantic_table_id_is_recognized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>JMR table</h1>
<section><h2>Results</h2><div id="jmr2448-tbl-0001">
<header><span>Table 1.</span> Backbone populations.</header>
<table><thead><tr><th>Base</th><th>Percent</th></tr></thead>
<tbody><tr><td>G<sub>5</sub></td><td>87.8</td></tr></tbody></table>
</div></section></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "jmr2448-tbl-0001")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.label, "Table 1")
        self.assertEqual(table.parts[0].rows[1][0].text, "G_{5}")

    def test_allergy_wiley_semantic_table_id_is_recognized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Allergy table</h1>
<section><h2>Introduction</h2><div id="all15959-tbl-0001">
<header><span>TABLE 1.</span> Comparison of agents.</header>
<div><table><thead><tr><th>Reagent</th><th>Target</th></tr></thead>
<tbody><tr><td>RNAi</td><td>mRNA</td></tr></tbody></table></div>
</div></section></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "all15959-tbl-0001")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.label, "Table 1")
        self.assertEqual(table.title_plain, "TABLE 1. Comparison of agents.")
        self.assertEqual(table.parts[0].rows[1][1].text, "mRNA")

        near_miss = self.extract(
            """<!doctype html><html><body><article><h1>Near miss</h1>
<div id="all1595-tbl-0001"><header>Table 1. Not exact.</header>
<table><tr><td>1</td></tr></table></div></article></body></html>"""
        )
        self.assertEqual(near_miss.tables, [])

    def test_biopolymers_wiley_semantic_table_id_is_recognized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Biopolymers table</h1>
<section><h2>Results</h2><div id="bip22205-tbl-0003">
<header><span>Table 2.</span> Dissociation half-lives.</header>
<table><thead><tr><th>PA</th><th>∼<i>T</i><sub>1/2</sub> (app<a title="Link to note" id="bip22205-note-0006_38-controller" href="#bip22205-note-0006_38" aria-haspopup="false" aria-expanded="false" aria-label="Note">a</a>) (hr)</th>
<th>Control<a title="Link to note" id="bip2220-note-0006_38-controller" href="#bip2220-note-0006_38" aria-haspopup="false" aria-expanded="false" aria-label="Note">a</a></th></tr></thead>
<tbody><tr><td>1</td><td>&gt;8.3</td><td>value</td></tr></tbody></table>
<div><ul><li id="bip22205-note-0006"><span><sup><i>a</i></sup> </span>
Apparent half-lives from SPR experiments.</li></ul></div>
</div></section></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source_id, "bip22205-tbl-0003")
        self.assertEqual(table.source_kind, "html")
        self.assertEqual(table.label, "Table 2")
        self.assertEqual(table.title_plain, "Table 2. Dissociation half-lives.")
        self.assertEqual(
            table.parts[0].rows[0][1].text,
            "∼T_{1/2} (app^{a}) (hr)",
        )
        self.assertEqual(table.parts[0].rows[0][2].text, "Controla")
        self.assertEqual(table.parts[0].rows[1][1].text, ">8.3")
        self.assertEqual(
            table.footnotes_plain,
            ["^{a} Apparent half-lives from SPR experiments."],
        )

        near_miss = self.extract(
            """<!doctype html><html><body><article><h1>Near miss</h1>
<div id="bip2220-tbl-0001"><header>Table 1. Not exact.</header>
<table><tr><td>1</td></tr></table></div></article></body></html>"""
        )
        self.assertEqual(near_miss.tables, [])

    def test_html_table_graphical_cells_are_preserved_as_embedded_assets(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>Graphical HTML table cell</h1>
<div id="tbl1"><header>Table 1. Structures.</header><table>
  <tr><th>Compound</th><th>Structure</th></tr>
  <tr><td>1</td><td><img src="{pixel}" alt="image"></td></tr>
</table></div></article></body></html>"""
        )

        [asset] = result.embedded_assets
        self.assertEqual(asset.category, "table_cell")
        self.assertEqual(asset.asset_id, "table_001_part_01_row_002_column_002")
        self.assertEqual(asset.parent_table_id, "table_001")
        self.assertEqual(asset.compound_id, "part_01_row_002_column_002")
        self.assertEqual(
            asset.output_path,
            "tables/main/table_001_cells/part_01_row_002_column_002.gif",
        )

    def test_wiley_figure_controls_citations_and_reference_labels_are_normalized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley figure structure</h1>
<section><h2>Results</h2><p>See Figure 2.</p>
<figure id="cas123-fig-0002"><figcaption>
  <div><strong>Figure 2</strong><div><a href="#">Open in figure viewer</a>
    <a href="/download">PowerPoint</a></div></div>
  <div>Visible caption.<span><sup><a href="#cas123-bib-0007">7</a></sup></span></div>
</figcaption></figure></section>
<section id="article-references"><h2>References</h2><ol><li>
  <span>7</span><span>Example A. Visible reference.</span>
</li></ol></section>
</article></body></html>"""
        )

        self.assertEqual(
            [(item.figure_id, item.kind, item.label) for item in result.figures],
            [("figure_002", "figure", "Figure 2")],
        )
        caption = result.figures[0]
        self.assertEqual(caption.caption_markdown, "Visible caption.<sup>7</sup>")
        self.assertEqual(caption.caption_plain, "Visible caption.^{7}")
        self.assertNotIn("Open in figure viewer", caption.caption_plain)
        self.assertNotIn("PowerPoint", caption.caption_plain)
        self.assertEqual(result.references[0].plain_text, "7. Example A. Visible reference.")

    def test_wiley_author_year_citation_year_stays_on_the_baseline(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley author-year citations</h1><section><h2>Introduction</h2>
<p>Prior work (Pandian et al., <span><a href="#jcp30140-bib-0031">2014</a></span>)
and a numeric citation <span><a href="#jcp30140-bib-0007">7</a></span>.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Prior work (Pandian et al., 2014) and a numeric citation <sup>7</sup>.",
        )
        self.assertEqual(
            block.plain_text,
            "Prior work (Pandian et al., 2014) and a numeric citation ^{7}.",
        )

    def test_wiley_continued_figure_has_distinct_identity_and_asset(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><article>
<h1>Wiley continued figure</h1><section><h2>Results</h2>
<figure id="jcp123-fig-0002"><img src="{pixel}" alt="Figure 2">
  <figcaption><div><strong>Figure 2</strong><div>
    <a href="#">Open in figure viewer</a><a href="/download">PowerPoint</a>
  </div></div><div>Panels a and b.</div></figcaption>
</figure>
<figure id="figure-2b"><img src="{pixel}" alt="Figure 2 continued">
  <figcaption><div><strong>Figure 2 (continued)</strong><div>
    <a href="#">Open in figure viewer</a><a href="/download">PowerPoint</a>
  </div></div><div>Panels c through e.</div></figcaption>
</figure></section></article></body></html>"""
        )

        self.assertEqual(
            [(item.figure_id, item.label) for item in result.figures],
            [
                ("figure_002", "Figure 2"),
                ("figure_002_continued", "Figure 2 (continued)"),
            ],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["figure_002", "figure_002_continued"],
        )
        self.assertEqual(
            [asset.output_path for asset in result.embedded_assets],
            [
                "figures/main/figure_002.gif",
                "figures/main/figure_002_continued.gif",
            ],
        )

    def test_wiley_hindawi_split_panels_form_one_complete_figure(self) -> None:
        pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        result = self.extract(
            f"""<!doctype html><html><body><article><div id="article__content">
<div id="journal-banner-text"><a href="/journal/1">Synthetic Hindawi</a></div>
<a href="https://doi.org/10.1155/2012/715928">DOI</a>
<h1>Wiley Hindawi split panels</h1><section><h2>Results</h2>
<span id="fig-0001">
<figure id="figpt-0001"><a><img src="{pixel}" alt="Details are in the caption following the image"></a>
<figcaption><div><strong>Figure 1<span> (a) Compound α</span></strong><div>
<a href="#">Open in figure viewer</a><a href="/action/downloadFigures?id=fig-0001&amp;partId=figpt-0001&amp;doi=10.1155%2F2012%2F715928">PowerPoint</a>
</div></div><div>Complete authored legend with <i>β</i>.</div></figcaption></figure>
<figure id="figpt-0002"><a><img src="{pixel}" alt="Details are in the caption following the image"></a>
<figcaption><div><strong>Figure 1<span> (b) Compound γ</span></strong><div>
<a href="#">Open in figure viewer</a><a href="/action/downloadFigures?id=fig-0001&amp;partId=figpt-0002&amp;doi=10.1155%2F2012%2F715928">PowerPoint</a>
</div></div><div>Complete authored legend with <i>β</i>.</div></figcaption></figure>
</span></section></div></article></body></html>"""
        )

        self.assertEqual(
            [(figure.figure_id, figure.source_id, figure.label) for figure in result.figures],
            [("figure_001", "fig-0001", "Figure 1")],
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            (
                "Complete authored legend with <em>β</em>. Panel labels: "
                "(a) Compound α; (b) Compound γ."
            ),
        )
        self.assertEqual(
            result.figures[0].caption_plain,
            (
                "Complete authored legend with β. Panel labels: "
                "(a) Compound α; (b) Compound γ."
            ),
        )
        [asset] = result.embedded_assets
        self.assertEqual((asset.asset_id, asset.media_type), ("figure_001", "image/png"))
        self.assertEqual(asset.output_path, "figures/main/figure_001.png")
        self.assertTrue(asset.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_wiley_hindawi_stacked_table_label_has_no_false_bullets(self) -> None:
        source = """<!doctype html><html><body><article><div id="article__content">
<div id="journal-banner-text"><a href="/journal/1">Synthetic Hindawi</a></div>
<a href="https://doi.org/10.1155/2012/715928">DOI</a>
<h1>Wiley Hindawi table labels</h1><section><h2>Results</h2>
<div id="tbl-0001"><header>Table 1. Concentrations.</header><table><tr>
<td><ul><li>Observed concentration</li><li>(mean of <i>n</i> = 3, <i>μ</i>g/mL)</li></ul></td>
<td>0.52</td></tr></table></div></section></div></article></body></html>"""
        result = self.extract(source)

        cell = result.tables[0].parts[0].rows[0][0]
        self.assertEqual(
            cell.text,
            "Observed concentration\n(mean of n = 3, μg/mL)",
        )
        self.assertNotIn("•", cell.markdown)

        near_miss = self.extract(
            source.replace('id="article__content"', 'id="article-content"')
        )
        self.assertTrue(near_miss.tables[0].parts[0].rows[0][0].text.startswith("• "))

    def test_exact_duplicate_front_matter_is_emitted_once(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Front matter deduplication</h1>
<p>Copyright © 2012 Example Authors.</p><section><h2>Results</h2><p>Body.</p></section>
</article><div id="pane-pcw-details"><section><section><h3>Details</h3>
<p>Copyright © 2012 Example Authors.</p></section></section></div></body></html>"""
        )

        copyright_blocks = [
            block.plain_text
            for block in result.front_matter
            if block.plain_text == "Copyright: Copyright © 2012 Example Authors."
        ]
        self.assertEqual(copyright_blocks, ["Copyright: Copyright © 2012 Example Authors."])

    def test_exact_wiley_snapshot_recovers_mislabeled_jpeg_and_prefixed_table(self) -> None:
        encoded_jpeg = base64.b64encode(b"\xff\xd8\xff\xe0wiley-jpeg").decode("ascii")
        result = self.extract(
            f"""<!doctype html><html><body><div id="article__content">
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<article lang="en"><a href="https://doi.org/10.1002/cbic.202200124">DOI</a>
<h1>Exact Wiley snapshot</h1><section><h2>Results</h2><p>Body.</p>
<figure id="cbic202200124-fig-0001">
  <img src="data:image/png;base64,{encoded_jpeg}" alt="Figure 1">
  <figcaption><div><strong>Figure 1</strong></div><div>Caption.</div></figcaption>
</figure>
<div id="cbic202200124-tbl-0001"><header>Table 1. Values.</header>
  <table><thead><tr><th>Compound</th><th>Value</th></tr></thead>
  <tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>
</section></article></div></body></html>"""
        )

        self.assertEqual(len(result.embedded_assets), 1)
        self.assertEqual(result.embedded_assets[0].media_type, "image/jpeg")
        self.assertEqual(
            result.embedded_assets[0].output_path,
            "figures/main/figure_001.jpg",
        )
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_id, "cbic202200124-tbl-0001")

        wrong_doi = self.extract(
            f"""<!doctype html><html><body><div id="article__content">
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<article lang="en"><a href="https://doi.org/10.1002/cbic.202200125">DOI</a>
<h1>Near-miss Wiley snapshot</h1><section><h2>Results</h2>
<figure id="cbic202200124-fig-0001">
  <img src="data:image/png;base64,{encoded_jpeg}" alt="Figure 1">
  <figcaption><div><strong>Figure 1</strong></div><div>Caption.</div></figcaption>
</figure></section></article></div></body></html>"""
        )
        self.assertEqual(wrong_doi.embedded_assets, [])
        self.assertTrue(
            any(
                warning["code"] == "invalid_embedded_figure_image"
                for warning in wrong_doi.warnings
            )
        )

        near_miss_table = self.extract(
            """<!doctype html><html><body><article><h1>Near-miss table</h1>
<section><h2>Results</h2><div id="cbic20220012-tbl-0001">
<header>Table 1. Not an exact Wiley ID.</header>
<table><tr><th>A</th></tr><tr><td>1</td></tr></table>
</div></section></article></body></html>"""
        )
        self.assertEqual(near_miss_table.tables, [])

    def test_legacy_rcm_wiley_letter_recovers_unheaded_body_and_table(self) -> None:
        pixel = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        source = f"""<!doctype html><html><body><article><div id="article__content">
<div id="journal-banner-text">Rapid Communications in Mass Spectrometry</div>
<h1>Legacy RCM letter</h1><a href="https://doi.org/10.1002/rcm.4953">DOI</a>
<article lang="en"><section>Dear Editor,
<section id="rcm4953-sec-0001"><p>First paragraph.</p><p>Second paragraph.</p>
<figure id="rcm4953-fig-0001"><img src="data:image/gif;base64,{pixel}"
alt="Figure 1."><figcaption><div><strong>Figure 1</strong></div>
<div>Exact figure caption.</div></figcaption></figure>
<div id="rcm4953-tbl-0001"><header><span>Table 1.</span> Exact values.</header>
<table><thead><tr><th>Compound</th><th>R</th></tr></thead>
<tbody><tr><td>P1</td><td>0.11</td></tr></tbody></table></div>
</section><div><h2>Acknowledgements</h2><p>Supported.</p></div>
<section id="article-references-section-1"><h2>REFERENCES</h2>
<ul><li><span>1</span><span>A. Author.</span></li></ul></section>
</section></article></div></article></body></html>"""
        result = self.extract(source)

        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Main text", ["Dear Editor,", "First paragraph.", "Second paragraph."]),
                ("Acknowledgements", ["Supported."]),
            ],
        )
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_id, "rcm4953-tbl-0001")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Compound", "R"], ["P1", "0.11"]],
        )

        near_miss = self.extract(
            source.replace('id="article-references-section-1"', 'id="related-reading"')
        )
        self.assertFalse(
            any(
                section.heading == "Main text"
                and section.blocks
                and section.blocks[0].plain_text == "Dear Editor,"
                for section in near_miss.sections
            )
        )

    def test_legacy_jms_wiley_letter_recovers_body_and_terminal_signoff(self) -> None:
        pixel = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        source = f"""<!doctype html><html><body><article><div id="article__content">
<div id="journal-banner-text">Journal of Mass Spectrometry</div>
<h1>Legacy JMS letter</h1><a href="https://doi.org/10.1002/jms.803">DOI</a>
<article lang="en"><section>Dear Sir,
<section id="sec1-1"><p>First paragraph with MS<sup>2</sup> notation.</p>
<section><figure id="sch1"><img src="data:image/gif;base64,{pixel}"
alt="Scheme 1."><figcaption><strong>Scheme 1</strong>
Exact scheme caption.</figcaption></figure></section>
<p>Second paragraph.</p><p><i>Yours,</i></p></section>
<section id="sec-bibl-1"><section id="article-references-section-1">
<h2><span>References</span></h2><ul><li><span>1</span><span>A. Author.</span></li></ul>
</section></section><div id=""><p>ALICE AUTHOR* * Department of Chemistry.</p></div>
</section></article></div></article></body></html>"""
        result = self.extract(source)

        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                (
                    "Main text",
                    [
                        "Dear Sir,",
                        "First paragraph with MS^{2} notation.",
                        "Second paragraph.",
                        "Yours,",
                        "ALICE AUTHOR* * Department of Chemistry.",
                    ],
                ),
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.references],
            ["1. A. Author."],
        )

        near_miss = self.extract(
            source.replace('id="sec-bibl-1"', 'id="related-reading"')
        )
        self.assertFalse(
            any(
                section.heading == "Main text"
                and section.blocks
                and section.blocks[0].plain_text == "Dear Sir,"
                for section in near_miss.sections
            )
        )

    def test_wiley_editorial_medal_placeholder_is_not_article_content(self) -> None:
        medal_href = (
            "/cms/asset/77c53439-7f11-4bac-b575-2b149a97cf2b/mmedal.jpg"
        )
        result = self.extract(
            f"""<!doctype html><html><body>
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<article><h1>Wiley medal boundary</h1><section><h2>Results</h2>
<p><span id="medal"><a target="_blank" href="{medal_href}"><picture>
<span>[Image omitted: chemical structure image]</span>
</picture></a></span></p><p>Authored article text.</p>
</section></article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored article text."],
        )

        near_miss = self.extract(
            """<!doctype html><html><body>
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<article><h1>Wiley authored image boundary</h1>
<section><h2>Results</h2><p><span id="medal">
<a target="_blank" href="/cms/asset/77c53439-7f11-4bac-b575-2b149a97cf2b/figure.jpg">
<picture><span>[Image omitted: chemical structure image]</span></picture></a>
</span></p></section></article></body></html>"""
        )
        self.assertEqual(
            near_miss.sections[0].blocks[0].plain_text,
            "[Image omitted: chemical structure image]",
        )

    def test_wiley_author_popovers_yield_deduplicated_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<h1>Wiley front matter</h1>
<a href="https://creativecommons.org/licenses/by/4.0/">Creative Commons license</a>
<div><span><a href="/authored-by/Example/Alice" aria-controls="am1"
 id="am1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <ul><li><a href="https://orcid.org/0000-0001-2345-678X">orcid.org/0000-0001-2345-678X</a></li></ul>
 <p>Department A, Example University</p><p><b>Correspondence</b></p>
 <p>Alice Example, Department A, Example University.</p>
 <p>Email: <a href="mailto:alice@example.test">alice@example.test</a></p>
 <p>Contribution: &#x200b;Conceptualization, Investigation</p>
</div></span><span><a href="/authored-by/Example/Bob" aria-controls="am2"
 id="am2_Ctrl"><span>Bob Example</span></a><div role="region"
 aria-labelledby="am2_Ctrl" id="am2"><p>Bob Example</p>
 <p>Department B, Example Institute</p>
 <p>These authors contributed equally to this work.</p></div></span></div>
<div id="sb-1"><span><a href="/authored-by/Example/Alice" aria-controls="a1"
 id="a1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="a1_Ctrl" id="a1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <ul><li><a href="https://orcid.org/0000-0001-2345-678X">orcid.org/0000-0001-2345-678X</a></li></ul>
 <p>Department A, Example University</p><p><b>Correspondence</b></p>
 <p>Alice Example, Department A, Example University.</p>
 <p>Email: <a href="mailto:alice@example.test">alice@example.test</a></p>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                (
                    "License: Creative Commons license: "
                    "https://creativecommons.org/licenses/by/4.0/"
                ),
                "Affiliation (Alice Example): Department A, Example University",
                "Affiliation (Bob Example): Department B, Example Institute",
                "Contribution (Alice Example): Conceptualization, Investigation",
                (
                    "Contribution (Bob Example): These authors contributed "
                    "equally to this work."
                ),
                (
                    "Correspondence: Alice Example, Department A, Example "
                    "University. Email: alice@example.test"
                ),
                "ORCID (Alice Example): https://orcid.org/0000-0001-2345-678X",
            ],
        )
        self.assertEqual(
            len({block.block_id for block in result.front_matter}),
            len(result.front_matter),
        )
        self.assertTrue(
            all("Search for more papers" not in block.plain_text for block in result.front_matter)
        )
        correspondence = next(
            block
            for block in result.front_matter
            if block.plain_text.startswith("Correspondence:")
        )
        affiliation = next(
            block
            for block in result.front_matter
            if block.plain_text.startswith("Affiliation (Alice Example):")
        )
        self.assertRegex(correspondence.source_locator, r"/div$")
        # Near miss: a single-paragraph affiliation keeps its precise source
        # paragraph instead of being widened to the author panel.
        self.assertRegex(affiliation.source_locator, r"/p\[\d+\]$")

    def test_wiley_named_correspondence_cards_are_not_extra_affiliations(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley named correspondence cards</h1>
<div><span><a href="/authored-by/Rant/Ulrich" aria-controls="am1"
 id="am1_Ctrl"><span>Dr. Ulrich Rant</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>Dr. Ulrich Rant</p>
 <ul><li><a href="mailto:ulrich@example.test">ulrich@example.test</a></li></ul>
 <p>Walter Schottky Institute, Technical University of Munich</p>
 <p>Ulrich Rant, Walter Schottky Institute, Technical University of Munich</p>
 <p>Glenn A. Burley, Department of Chemistry, University of Leicester</p>
</div></span><span><a href="/authored-by/Burley/Glenn+A." aria-controls="am2"
 id="am2_Ctrl"><span>Dr. Glenn A. Burley</span></a><div role="region"
 aria-labelledby="am2_Ctrl" id="am2">
 <p>Corresponding Author</p><p>Dr. Glenn A. Burley</p>
 <ul><li><a href="mailto:glenn@example.test">glenn@example.test</a></li></ul>
 <p>Department of Chemistry, University of Leicester</p>
 <p>Ulrich Rant, Walter Schottky Institute, Technical University of Munich</p>
 <p>Glenn A. Burley, Department of Chemistry, University of Leicester</p>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                (
                    "Affiliation (Dr. Ulrich Rant): Walter Schottky Institute, "
                    "Technical University of Munich"
                ),
                (
                    "Affiliation (Dr. Glenn A. Burley): Department of Chemistry, "
                    "University of Leicester"
                ),
                "Correspondence: Dr. Ulrich Rant: ulrich@example.test",
                "Correspondence: Dr. Glenn A. Burley: glenn@example.test",
            ],
        )

    def test_wiley_explicit_corresponding_role_keeps_panel_author_once(self) -> None:
        address = "Department A, Example University."
        email = '<a href="mailto:shared@example.test">shared@example.test</a>'
        cases = [
            # Archived legacy cards put email before the affiliation and
            # leave the correspondence address as an unwrapped text node.
            ("Alice Example", True,
             f'<ul><li>{email}</li></ul><p>Department A</p>{address}',
             f"Alice Example: {address} shared@example.test"),
            ("Alice Example", True,
             f'<p>Department A</p><p><b>Correspondence</b></p><p>{address}</p>'
             f'<p>Email: {email}</p>',
             f"Alice Example: {address} Email: shared@example.test"),
            ("Alice Example", True,
             f'<p>Department A</p><p><b>Correspondence</b> {address}</p>'
             f'<p>Email: {email}</p>',
             f"Alice Example: {address} Email: shared@example.test"),
            ("Alice Example", True,
             f'<p>Department A</p><p>Correspondence to: {address}</p>'
             f'<p>Email: {email}</p>',
             f"Alice Example: {address} Email: shared@example.test"),
            # Full names already in the prose (including without the panel's
            # honorific) retain their source wording, without a second name.
            ("Dr. Alice Example", True,
             f'<p>Department A</p>Contact Alice Example, {address}{email}',
             f"Contact Alice Example, {address} shared@example.test"),
            ("Alice Example", True,
             f'<p>Department A</p><p>Correspondence to: Alice Example, {address}</p>'
             f'<p>Email: {email}</p>',
             f"Alice Example, {address} Email: shared@example.test"),
            # Neither a longer name sharing a prefix nor an email address
            # counts as naming this panel's explicitly corresponding author.
            ("Alice Example", True,
             f'<p>Department A</p>Contact Alice Exampleton, {address}{email}',
             f"Alice Example: Contact Alice Exampleton, {address} shared@example.test"),
            ("Ann", True,
             '<p>Department A</p><a href="mailto:ann@example.test">ann@example.test</a>',
             "Ann: ann@example.test"),
            # A correspondence heading alone may contain shared contacts;
            # it does not mark the enclosing author as corresponding.
            ("Alice Example", False,
             f'<p>Department A</p><p><b>Correspondence</b></p><p>{address}</p>'
             f'<p>Email: {email}</p>',
             f"{address} Email: shared@example.test"),
        ]
        for author, has_role, content, expected in cases:
            with self.subTest(author=author, has_role=has_role, content=content):
                role = "<p>Corresponding Author</p>" if has_role else ""
                panels = "".join(
                    f'<span><a id="{panel_id}_Ctrl" aria-controls="{panel_id}" '
                    f'href="/authored-by/Example/Alice">{author}</a>'
                    f'<div role="region" id="{panel_id}" '
                    f'aria-labelledby="{panel_id}_Ctrl">{role}<p>{author}</p>'
                    f'{content}<a href="/authored-by/Example/Alice">'
                    'Search for more papers by this author</a></div></span>'
                    for panel_id in ("am1", "a1")
                )
                result = self.extract(
                    '<html><body><article><h1>Synthetic Wiley correspondence</h1>'
                    f'{panels}<section><h2>Results</h2><p>Scientific body.</p>'
                    '</section></article></body></html>'
                )
                correspondence = [block for block in result.front_matter
                                  if block.plain_text.startswith("Correspondence:")]
                self.assertEqual([block.plain_text for block in correspondence],
                                 [f"Correspondence: {expected}"])
                self.assertEqual(correspondence[0].markdown,
                                 f"**Correspondence:** {expected}")
                self.assertRegex(correspondence[0].source_locator, r"/div$")
                self.assertEqual(
                    [block.plain_text for section in result.sections for block in section.blocks],
                    ["Scientific body."],
                )

    def test_wiley_shared_mailbox_preserves_each_explicit_corresponding_author(self) -> None:
        panels = "".join(
            f'<span><a id="{prefix}{number}_Ctrl" aria-controls="{prefix}{number}" '
            f'href="/authored-by/Example/{number}">{author}</a>'
            f'<div role="region" id="{prefix}{number}"><p>Corresponding Author</p>'
            f'<p>{author}</p><p>Department A</p>'
            '<a href="mailto:shared@example.test">shared@example.test</a></div></span>'
            for prefix in ("am", "a")
            for number, author in enumerate(("Alice Example", "Bob Example"), 1)
        )
        result = self.extract(
            '<html><body><article><h1>Synthetic shared mailbox</h1>'
            f'{panels}<section><h2>Results</h2><p>Body.</p></section>'
            '</article></body></html>'
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter
             if block.plain_text.startswith("Correspondence:")],
            ["Correspondence: Alice Example: shared@example.test",
             "Correspondence: Bob Example: shared@example.test"],
        )

    def test_wiley_mailto_only_author_panel_is_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley mailto-only correspondence</h1>
<div><span><a href="/authored-by/Yuan/Gu" aria-controls="am1"
 id="am1_Ctrl"><span>Gu Yuan</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1"><p>Gu Yuan</p>
 <ul><li><a href="mailto:guyuan%40example.test?subject=article">guyuan@example.test</a></li></ul>
 <p>Department of Chemistry, Example University</p>
 <a href="/authored-by/Yuan/Gu">Search for more papers by this author</a>
</div></span></div>
<div id="sb-1"><span><a href="/authored-by/Yuan/Gu" aria-controls="a1"
 id="a1_Ctrl"><span>Gu Yuan</span></a><div role="region"
 aria-labelledby="a1_Ctrl" id="a1"><p>Gu Yuan</p>
 <ul><li><a href="mailto:guyuan%40example.test?subject=article">guyuan@example.test</a></li></ul>
 <p>Department of Chemistry, Example University</p></div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Gu Yuan): Department of Chemistry, Example University",
                "Correspondence: guyuan@example.test",
            ],
        )
        correspondence = result.front_matter[1]
        self.assertRegex(correspondence.source_locator, r"/div$")
        self.assertNotIn("Search for more papers", correspondence.plain_text)

    def test_wiley_direct_text_equal_contribution_note_is_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<h1>Legacy Wiley equal-contribution note</h1>
<div><span><a href="/authored-by/Example/Alice" aria-controls="am1"
 id="am1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1"><p>Department A</p>
 These authors contribute equally to this work.
 <a href="/authored-by/Example/Alice">Search for more papers by this author</a>
</div></span></div><section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A",
                (
                    "Contribution (Alice Example): These authors contribute "
                    "equally to this work."
                ),
            ],
        )
        self.assertTrue(
            all(
                "Search for more papers" not in block.plain_text
                for block in result.front_matter
            )
        )

    def test_wiley_legacy_popover_raw_correspondence_is_included_once(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley correspondence</h1>
<div><span><a href="/authored-by/Example/Alice" aria-controls="am1"
 id="am1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <p>Department A, Example University</p>
 To whom correspondence should be addressed. E-mail:===
 <a href="mailto:alice@example.test"><span>alice@example.test</span></a>
 <a href="/authored-by/Example/Alice">Search for more papers by this author</a>
</div></span></div>
<div><span><a href="/authored-by/Example/Alice" aria-controls="a1"
 id="a1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="a1_Ctrl" id="a1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <p>Department A, Example University</p>
 To whom correspondence should be addressed. E-mail:===
 <a href="mailto:alice@example.test"><span>alice@example.test</span></a>
 <a href="/authored-by/Example/Alice">Search for more papers by this author</a>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                (
                    "Correspondence: Alice Example: To whom correspondence should be addressed. "
                    "E-mail: alice@example.test"
                ),
            ],
        )
        self.assertTrue(
            all("Search for more papers" not in block.plain_text for block in result.front_matter)
        )

    def test_wiley_plain_correspondence_to_paragraph_starts_boundary(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Plain Wiley correspondence boundary</h1>
<div><span><a href="/authored-by/David+Wilson/W." aria-controls="am1"
 id="am1_Ctrl"><span>W. David Wilson</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>W. David Wilson</p>
 <p>Department of Chemistry, Georgia State University, Atlanta, GA, 30303 USA</p>
 <p>Correspondence to: W. David Wilson, Department of Chemistry, Georgia State University, Atlanta, GA 30303, USA.</p>
 <p>E-mail: <a href="mailto:wdw@example.test">wdw@example.test</a></p>
</div></span></div>
<div><span><a href="/authored-by/David+Wilson/W." aria-controls="a1"
 id="a1_Ctrl"><span>W. David Wilson</span></a><div role="region"
 aria-labelledby="a1_Ctrl" id="a1">
 <p>Corresponding Author</p><p>W. David Wilson</p>
 <p>Department of Chemistry, Georgia State University, Atlanta, GA, 30303 USA</p>
 <p>Correspondence to: W. David Wilson, Department of Chemistry, Georgia State University, Atlanta, GA 30303, USA.</p>
 <p>E-mail: <a href="mailto:wdw@example.test">wdw@example.test</a></p>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                (
                    "Affiliation (W. David Wilson): Department of Chemistry, "
                    "Georgia State University, Atlanta, GA, 30303 USA"
                ),
                (
                    "Correspondence: W. David Wilson, Department of Chemistry, "
                    "Georgia State University, Atlanta, GA 30303, USA. "
                    "E-mail: wdw@example.test"
                ),
            ],
        )

    def test_wiley_inline_correspondence_heading_stops_affiliation_collection(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Inline Wiley correspondence</h1>
<div><span><a href="/authored-by/Example/Alice" aria-controls="am1"
 id="am1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <ul><li><a href="mailto:alice@example.test">alice@example.test</a></li></ul>
 <p>Department A, Example University</p>
 <p><b>Correspondence</b> Alice Example, Department A, Example University.</p>
 <p>Email: <a href="mailto:alice@example.test">alice@example.test</a></p>
 <p>Bob Example, Department B, Example Institute.</p>
 <p>Email: <a href="mailto:bob@example.test">bob@example.test</a></p>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                (
                    "Correspondence: Alice Example, Department A, Example "
                    "University. Email: alice@example.test Bob Example, "
                    "Department B, Example Institute. Email: bob@example.test"
                ),
            ],
        )

    def test_inline_prose_before_one_trailing_list_is_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Mixed wrapper</h1>
<section><h3>2.1 Cell culture</h3><div>Cells expressing
<i>PRCC</i>-<i>TFE3</i> used shRNA<sup>7</sup>:
<ul><li>shTFE3#1 5-AAAA-3;</li><li>shTFE3#2 5-CCCC-3.</li></ul></div>
<p>Following method prose.</p></section></article></body></html>"""
        )

        section = result.sections[0]
        self.assertEqual(
            [block.kind for block in section.blocks],
            ["paragraph", "list", "paragraph"],
        )
        self.assertEqual(
            section.blocks[0].plain_text,
            "Cells expressing PRCC-TFE3 used shRNA^{7}:",
        )
        self.assertIn("<em>PRCC</em>-<em>TFE3</em>", section.blocks[0].markdown)
        self.assertEqual(
            section.blocks[1].plain_text,
            "- shTFE3#1 5-AAAA-3;\n- shTFE3#2 5-CCCC-3.",
        )
        self.assertEqual(section.blocks[2].plain_text, "Following method prose.")

    def test_mixed_wrapper_with_authored_tail_is_not_normalized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><h1>Near miss</h1>
<section><h3>Methods</h3><div>Ambiguous leading prose
<em>with markup</em><ul><li>One item.</li></ul><span>Authored tail.</span></div>
<p>Following method prose.</p></section></article></body></html>"""
        )

        self.assertEqual(
            [block.kind for block in result.sections[0].blocks],
            ["list", "paragraph"],
        )
        self.assertEqual(result.sections[0].blocks[0].plain_text, "- One item.")

    def test_wiley_abbreviations_table_and_unheaded_intro_are_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley abbreviations</h1>
<section><h3>Abbreviations:</h3><div><table><tbody>
<tr><th><li>GI<sub>50</sub></li></th><td><li>50% growth inhibition concentration</li></td></tr>
<tr><th><li>Py</li></th><td><li>pyrrole</li></td></tr>
</tbody></table></div>
<section id="ss100"><p>Unheaded introductory prose.</p></section>
<section id="ss1"><h2>Materials and Methods</h2><p>Method prose.</p></section>
</section></article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abbreviations:", "Introduction", "Materials and Methods"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            [
                "- GI_{50}: 50% growth inhibition concentration\n- Py: pyrrole"
            ],
        )
        self.assertEqual(result.sections[0].blocks[0].kind, "list")
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded introductory prose."],
        )
        self.assertEqual(result.tables, [])

    def test_abbreviation_table_blank_term_continues_previous_definition(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Abbreviation continuation</h1>
<section id="html-glossary"><h2>Abbreviations</h2><table><tbody>
<tr><td>OTI</td><td>Oligonucleotide-recognizing topoisomerase inhibitor</td></tr>
<tr><td></td><td>(see individual definitions for: PIP-OTIs and WC-OTIs)</td></tr>
<tr><td>WC</td><td>Watson–Crick</td></tr>
</tbody></table></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.sections[0].blocks), 1)
        self.assertEqual(result.sections[0].blocks[0].kind, "list")
        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            "- OTI: Oligonucleotide-recognizing topoisomerase inhibitor "
            "(see individual definitions for: PIP-OTIs and WC-OTIs)\n"
            "- WC: Watson–Crick",
        )

    def test_sciencedirect_nested_kwrd_abbreviations_are_one_semantic_list(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect abbreviations</h1>
<div id="kwrds0015" lang="en"><h2>Abbreviations</h2>
<div id="kwrd0035"><span>ATM</span><div id="kwrd0040"><span>ataxia telangiectasia mutated</span></div></div>
<div id="kwrd0045"><span>IC<sub>50</sub></span><div id="kwrd0050"><span>half maximal inhibitory concentration</span></div></div>
<div id="kwrd0055"><span><em>MYCN</em>-amp</span><div id="kwrd0060"><span><em>MYCN</em> amplification</span></div></div>
</div><section><h2>Introduction</h2><p>Body.</p></section>
</article></body></html>"""
        )

        abbreviations = result.sections[0]
        self.assertEqual(abbreviations.heading, "Abbreviations")
        self.assertEqual(len(abbreviations.blocks), 1)
        self.assertEqual(abbreviations.blocks[0].kind, "list")
        self.assertEqual(
            abbreviations.blocks[0].plain_text,
            "- ATM: ataxia telangiectasia mutated\n"
            "- IC_{50}: half maximal inhibitory concentration\n"
            "- MYCN-amp: MYCN amplification",
        )
        self.assertIn("<sub>50</sub>", abbreviations.blocks[0].markdown)
        self.assertIn("<em>MYCN</em>-amp", abbreviations.blocks[0].markdown)

    def test_legacy_sciencedirect_nested_kw_abbreviations_are_one_list(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy ScienceDirect abbreviations</h1>
<div id="ks0010"><h2>Abbreviations</h2>
<div id="kw0030"><span id="tx0035">PIPs</span><div id="kw0035"><span id="tx0040">pyrrole–imidazole polyamides</span></div></div>
<div id="kw0040"><span id="tx0045">CTB</span><div id="kw0045"><span id="tx0050">[<em>N</em>-ethylbenzamide]</span></div></div>
</div><section><h2>Introduction</h2><p>Body.</p></section>
</article></body></html>"""
        )

        abbreviations = result.sections[0]
        self.assertEqual(abbreviations.heading, "Abbreviations")
        self.assertEqual(len(abbreviations.blocks), 1)
        self.assertEqual(abbreviations.blocks[0].kind, "list")
        self.assertEqual(
            abbreviations.blocks[0].plain_text,
            "- PIPs: pyrrole–imidazole polyamides\n- CTB: [N-ethylbenzamide]",
        )
        self.assertIn("[<em>N</em>-ethylbenzamide]", abbreviations.blocks[0].markdown)

    def test_sciencedirect_archived_card_and_glossary_abbreviations_consolidate(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect archived abbreviation cards</h1>
<div id="pc_A1b2"><h2>Abbreviations</h2>
<div id="pc_C3d4"><span>PI</span><div id="pc_E5f6"><span>pyrrole-imidazole</span></div></div>
</div>
<section><h2>Abbreviations</h2><ul>
<dt>PIP</dt><dd><div>pyrrole-imidazole polyamide</div></dd>
<dt>Py</dt><dd><div>N-methylpyrrole</div></dd>
</ul></section>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abbreviations", "Results"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            [
                "- PI: pyrrole-imidazole",
                "- PIP: pyrrole-imidazole polyamide\n- Py: N-methylpyrrole",
            ],
        )
        self.assertTrue(
            all(block.kind == "list" for block in result.sections[0].blocks)
        )

        malformed = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect malformed abbreviation card</h1>
<div id="pc_A1b2"><h2>Abbreviations</h2>
<div id="unsafe"><span>PI</span><div id="pc_E5f6"><span>pyrrole-imidazole</span></div></div>
</div><section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )
        self.assertFalse(
            any(
                "pyrrole-imidazole" in block.plain_text
                for section in malformed.sections
                for block in section.blocks
            )
        )

    def test_acs_nested_definition_list_abbreviations_are_one_list(self) -> None:
        source = """
<html><body><h1>ACS abbreviations</h1>
<h3>Abbreviations Used</h3><div><dl>
<a id=""></a><div><dt>Py</dt><div><dd><p><em>N</em>-methylpyrrole</p></dd></div></div>
<a id=""></a><div><dt>p<em>K</em><sub>a</sub></dt><div><dd><p>acid dissociation constant</p></dd></div></div>
</dl></div></body></html>
"""
        result = self.extract(source)
        abbreviations = result.sections[0]
        self.assertEqual(len(abbreviations.blocks), 1)
        self.assertEqual(abbreviations.blocks[0].kind, "list")
        self.assertEqual(
            abbreviations.blocks[0].plain_text,
            "- Py: N-methylpyrrole\n- pK_{a}: acid dissociation constant",
        )
        self.assertIn("<sub>a</sub>", abbreviations.blocks[0].markdown)

        canonical_heading = self.extract(source.replace("Abbreviations Used", "Abbreviations"))
        self.assertEqual(
            canonical_heading.sections[0].blocks[0].plain_text,
            abbreviations.blocks[0].plain_text,
        )

        malformed = source.replace("<div><dd><p>", "<section><dd><p>", 1)
        near_miss = self.extract(malformed)
        self.assertNotEqual(
            [block.kind for block in near_miss.sections[0].blocks], ["list"]
        )

    def test_wiley_simple_header_yields_bibliographic_and_author_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><main><header>
<p>Synthetic Wiley Journal; Volume 20, Issue 5; pp. 1310-1317</p>
<h1>Wiley simple header</h1>
<section id="authors"><h2>Authors</h2><ul>
<li><strong>Alice Example</strong><p>Department A, Example University</p></li>
<li><strong>Dr. Bob Example</strong><span> — Corresponding Author</span>
<p>Department B, Example Institute</p><p>bob@example.test</p></li>
</ul></section>
<p>First published: 30 December 2013; https://doi.org/10.0000/example</p>
</header><article><section><h2>Results</h2><p>Body.</p></section></article>
</main></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "20",
                "issue": "5",
                "pages": "1310-1317",
                "first_published": "30 December 2013",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                "Affiliation (Dr. Bob Example): Department B, Example Institute",
                "Correspondence: Dr. Bob Example: bob@example.test",
            ],
        )

    def test_wiley_bare_author_panels_retain_affiliations_correspondence_and_orcid(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article><div>
<div id="journal-banner-text">Cancer Science</div>
<h1>Bare Wiley author panels</h1>
<div><div>
  <span><strong><span>Alice Example</span></strong><span>, </span><div>
    <p>Department A, Example University</p></div></span>
  <span><strong><span>Bob Example</span></strong><span>, </span><div>
    <p>Corresponding Author</p>
    <a href="https://orcid.org/0000-0002-1234-5678">ORCID</a>
    <p>Department B, Example Institute</p><p><b>Correspondence</b></p>
    <p>Bob Example, Department B, Example Institute.</p>
    <p>Email: <a href="mailto:bob@example.test">bob@example.test</a></p>
  </div></span>
</div></div></div><article><section><h2>Results</h2><p>Body.</p></section></article>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                "Affiliation (Bob Example): Department B, Example Institute",
                (
                    "Correspondence: Bob Example, Department B, Example Institute. "
                    "Email: bob@example.test"
                ),
                "ORCID (Bob Example): https://orcid.org/0000-0002-1234-5678",
            ],
        )

    def test_wiley_information_panel_and_top_note_are_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div id="pbc28789-note-0001"><div>Tomoo Daifu and Masamitsu Mikami contributed equally to this work.</div></div>
<article><h1>Wiley information panel</h1>
<section><h2>Results</h2><p>Body.</p></section></article>
<div id="pane-pcw-details"><section>
  <section><h3>Details</h3><p>© 2020 Wiley Periodicals LLC</p><p></p>
    <ul><li><a role="button">Check for updates</a></li></ul></section>
  <section><h3>Research funding</h3><ul>
    <li>Japan Society for the Promotion of Science. Grant Number: 17H03597</li>
    <li>Japan Agency for Medical Research and Development. Grant Number: BINDS/19am0101101j0003</li>
  </ul></section>
  <section><h3>Keywords</h3><div><ul>
    <li><a href="/action/doSearch?text1=polyamide">polyamide</a></li>
    <li><a href="/action/doSearch?text1=RUNX1">RUNX1</a></li>
  </ul></div></section>
  <section><h3>Publication History</h3><ul>
    <li><label>Issue Online: </label>22 December 2020</li>
    <li><label>Manuscript received: </label>17 December 2019</li>
  </ul></section>
</section></div></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author note: Tomoo Daifu and Masamitsu Mikami contributed equally to this work.",
                "Copyright: © 2020 Wiley Periodicals LLC",
                (
                    "Research funding: Japan Society for the Promotion of Science. "
                    "Grant Number: 17H03597; Japan Agency for Medical Research and "
                    "Development. Grant Number: BINDS/19am0101101j0003"
                ),
                "Keywords: polyamide; RUNX1",
                (
                    "Publication history: Issue Online: 22 December 2020; "
                    "Manuscript received: 17 December 2019"
                ),
            ],
        )
        for block in result.front_matter:
            safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertEqual(
                plain_text_from_safe_html(safe_html),
                block.plain_text,
            )
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_wiley_front_matter_excludes_controls_body_notes_and_duplicates(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<a id="link_chem202002166-note-1001">title control</a>
<div id="empty-note-1001"></div>
<div id="abc-note-1002"><div>Equal contribution note.</div></div>
<div id="def-note-1003"><div>Equal contribution note.</div></div>
<div id="ghi-note-1004-controller"><div>controller content</div></div>
<article><h1>Wiley front-matter exclusions</h1><section><h2>Results</h2>
<p>Body.</p><ul><li id="note-p-61">Body note.</li></ul>
<table><tbody><tr><td><a id="tbl-note-0001_1-controller">table control</a></td></tr></tbody></table>
</section></article>
<div id="pane-pcw-details"><section>
  <section><h3>Details</h3><p>© 2020 Wiley Periodicals LLC</p>
    <ul><li><a role="button">Check for updates</a></li></ul></section>
  <section><h3>Related</h3><ul><li>Recommended article.</li></ul></section>
</section></div></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author note: Equal contribution note.",
                "Copyright: © 2020 Wiley Periodicals LLC",
            ],
        )
        combined = "\n".join(block.plain_text for block in result.front_matter)
        for excluded in (
            "title control",
            "controller content",
            "Body note",
            "table control",
            "Check for updates",
            "Recommended article",
        ):
            self.assertNotIn(excluded, combined)
        for block in result.front_matter:
            safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertEqual(
                plain_text_from_safe_html(safe_html),
                block.plain_text,
            )
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_graphical_abstract_prose_remains_machine_readable(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Graphical abstract prose</h1>
<section id="abstract"><h2>Abstract</h2><p>Main summary.</p></section>
<section id="abstract-graphical"><h2>Graphical Abstract</h2>
<p>Distinct authored graphical summary.</p>
<figure id="graphical-abstract"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Graphical abstract"></figure>
</section>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Graphical Abstract", "Results"],
        )
        self.assertEqual(
            result.sections[1].blocks[0].plain_text,
            "Distinct authored graphical summary.",
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

    def test_unheaded_intro_after_graphical_abstract_gets_own_section(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Graphical abstract followed by introduction</h1>
<section id="abstract-graphical"><h2>Graphical Abstract</h2>
<p>Distinct authored graphical summary.</p>
<figure><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Graphical abstract"></figure>
</section>
<section><section id="intro"><p>Unheaded introductory prose.</p></section>
<section id="results"><h2>Results</h2><p>Result prose.</p></section></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Graphical Abstract", "Introduction", "Results"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Distinct authored graphical summary."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded introductory prose."],
        )

    def test_acs_asset_only_visual_abstract_does_not_capture_article_body(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div id="acs-article-3743301">
<div><h1>ACS visual abstract boundary</h1></div><div><div>
<h2 id="200">Abstract</h2><div><div id="200-content">
  <p>Authored summary.</p></div>
  <div id="201-content"><figure id="article-visual-abstract"><h2>Visual Abstract</h2>
    <section aria-label="Main abstract"><p></p><div><img
      src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
      alt="Graphic. Refer to the image caption for details."></div></section>
  </figure></div>
  <div id="202-content"><p>Unheaded article opening.</p></div>
  <div id="203-content"><p>Continuation of the article body.</p></div>
</div><h2 id="300">Acknowledgements</h2><div id="300-content"><p>Supported.</p></div>
</div></div></div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction", "Acknowledgements"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded article opening.", "Continuation of the article body."],
        )
        self.assertEqual(
            result.sections[1].source_locator,
            "/html/body/div/div[2]/div/div[1]/div[3]",
        )

    def test_anonymous_acs_numeric_content_keeps_body_out_of_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div><h1 id="aria757493">Anonymous ACS communication</h1>
<a href="https://doi.org/10.1021/jacs.example">DOI</a></div>
<div id="ContentTab">
  <h2 id="100">Abstract</h2>
  <div id="100-content"><p>Authored summary.</p></div>
  <div id="101-content"><figure><h2>Visual Abstract</h2><img
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Graphic. Refer to the image caption for details."></figure></div>
  <div id="102-content"><p>Unheaded article opening.</p></div>
  <div id="103-content"><p>Continuation of the article body.</p></div>
  <h2 id="200">References</h2>
  <div id="200-content"><p>Reference content.</p></div>
</div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored summary."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded article opening.", "Continuation of the article body."],
        )

    def test_anonymous_acs_misc_title_notes_leave_body_and_references(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div><h1 id="aria757493">Anonymous ACS title-note article</h1>
<a href="https://doi.org/10.1021/bi.example">DOI</a></div>
<div id="ContentTab">
  <h2 id="100">Abstract</h2><div>
    <div id="100-content"><section aria-label="Main abstract"><p>Summary.</p></section></div>
  </div>
  <div><span>†</span></div>
  <div>
    <div id="101-content"><p>This work was supported by Grant ABC.</p></div>
    <div id="102-content"><p>Human receptor (hERR2, (<a
      ref-data-modal-source-id="bi.exampleb00001">1</a>) ESRRB) has a domain
      described previously (<em> (<a
      ref-data-modal-source-id="bi.exampleb00001">1</a>)</em>).</p>
      <figure><img
        src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
        alt="Figure 1. Result visual."><figcaption>Figure 1. Result visual.</figcaption>
      </figure></div>
  </div>
  <h2 id="200">Methods</h2><div id="200-content"><p>Method prose.</p></div>
  <h2 id="300">References</h2><div><div id="300-content"><div>
    <div><div><div><span>1.</span><div><div>Real reference.</div></div></div></div></div>
    <div><div><div><span>2.</span><div id="micit1"><div>Abbreviations: hERR2,
      human estrogen-related receptor 2.</div></div></div></div></div>
  </div></div></div>
</div></body></html>"""
        )

        section_text = [
            block.plain_text for section in result.sections for block in section.blocks
        ]
        self.assertNotIn("This work was supported by Grant ABC.", section_text)
        self.assertIn(
            "Human receptor (hERR2, ESRRB) has a domain described previously (1).",
            section_text,
        )
        self.assertIn(
            "Funding: This work was supported by Grant ABC.",
            [block.plain_text for block in result.front_matter],
        )
        self.assertIn(
            "Abbreviations: hERR2, human estrogen-related receptor 2.",
            [block.plain_text for block in result.front_matter],
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            ["1. Real reference."],
        )

    def test_current_acs_reference_dois_do_not_block_line_wrap_repair(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div><h1 id="aria757493">Current ACS full page</h1>
<a href="https://doi.org/10.1021/bi.example">Article DOI</a></div>
<div id="ContentTab">
  <h2 id="100">Abstract</h2>
  <div id="100-content"><p>A one-
dimensional experiment with pyrrole-
and imidazole-containing compounds and keto-
or enol tautomers.</p></div>
  <div id="101-content"><figure><img
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Graphic. Refer to the image caption for details."></figure></div>
  <h2 id="200">References</h2>
  <div id="200-content"><p><a href="https://doi.org/10.1021/ja.reference">Reference DOI</a></p></div>
</div></body></html>"""
        )

        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            (
                "A one-dimensional experiment with pyrrole- and "
                "imidazole-containing compounds and keto- or enol tautomers."
            ),
        )

    def test_anonymous_legacy_acs_abstract_wrapper_stays_in_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><div>
<header><h1>Legacy ACS body export</h1>
<a href="https://doi.org/10.1021/jacs.example">DOI</a></header>
<article><div><div>
  <div id="100-content"><div><h2>Visual Abstract</h2><figure><img
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Visual abstract"></figure></div></div>
  <h2 id="101">Abstract</h2><div><div id="101-content">
    <p>Authored summary.</p></div></div>
  <h2 id="102">Introduction</h2><div><div id="103-content">
    <p>Authored introduction.</p></div>
    <div id="104-content"><figure id="fig1"><img
      src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
      alt="Figure 1. Result visual."><figcaption>Figure 1. Result visual.</figcaption>
    </figure></div></div>
  <h2 id="200">References</h2><div><div id="200-content"><ol>
    <li><span>1</span> Example reference.</li></ol></div></div>
</div></div></article></div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored summary."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Authored introduction."],
        )

    def test_main_article_acs_communication_splits_unheaded_body_from_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><main><header>
<h1>Legacy ACS communication</h1>
<p>Bioconjugate Chem. (2011) 22 (2): 120–124.
https://doi.org/10.1021/bc.example; Received: August 23, 2010
Revision Received: December 08, 2010 Published Online: December 30, 2010
Published in Issue: February 16, 2011</p>
<p>Publisher: American Chemical Society</p></header>
<article>
  <div id="90-content"><figure id="visual-abstract"><h2>Visual Abstract</h2>
    <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
      alt="Visual abstract"></figure></div>
  <h2 id="100">Abstract</h2><div>
    <div id="100-content"><section aria-label="Main abstract"><p>Authored summary.</p></section></div>
    <div id="101-content"><p>Unheaded article opening.</p></div>
    <div id="102-content"><figure id="bc-example-figure-1"><img
      src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
      alt="Figure 1. Result visual."><figcaption>Figure 1. Result visual.</figcaption>
    </figure></div>
    <div id="103-content"><p>Continuation of the article body.</p></div>
  </div>
  <h2 id="notes-1">Supporting Information</h2><div><p>Supporting details.</p></div>
  <h2 id="200">Acknowledgments</h2><div><p>Supported.</p></div>
  <h2 id="300">References</h2><div><ol><li>Example reference.</li></ol></div>
</article></main></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction", "Acknowledgments"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored summary."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded article opening.", "Continuation of the article body."],
        )
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "22",
                "issue": "2",
                "pages": "120–124",
                "first_published": "December 30, 2010",
                "date": "February 16, 2011",
            },
        )
        self.assertIn(
            "Article history: Received: August 23, 2010; Revision Received: "
            "December 08, 2010; Published Online: December 30, 2010; "
            "Published in Issue: February 16, 2011",
            [block.plain_text for block in result.front_matter],
        )

    def test_anonymous_acs_preserves_authored_deep_chemical_heading(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div><h1 id="aria757493">Anonymous ACS chemistry article</h1>
<a href="https://doi.org/10.1021/jacs.example">DOI</a></div>
<div id="ContentTab">
  <h2 id="100">Abstract</h2>
  <div id="100-content"><p>Authored summary.</p></div>
  <div id="101-content"><figure><img
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Graphic. Refer to the image caption for details."></figure></div>
  <h2 id="110">Materials and Methods</h2>
  <div id="110-content"><h4>Syntheses and Analytical Data</h4>
    <h5>NO<sub>2</sub>PL(F)CO<sub>2</sub>Et (<strong>5</strong>)</h5>
    <p>Compound 5 was obtained as a brown powder.</p>
  </div>
  <h2 id="200">References</h2>
  <div id="200-content"><p>Reference content.</p></div>
</div></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Abstract",
                "Materials and Methods",
                "Syntheses and Analytical Data",
                "NO<sub>2</sub>PL(F)CO<sub>2</sub>Et (<strong>5</strong>)",
            ],
        )
        self.assertEqual(result.sections[-1].level, 5)
        self.assertEqual(
            [block.plain_text for block in result.sections[-1].blocks],
            ["Compound 5 was obtained as a brown powder."],
        )

    def test_emphasis_preserves_unicode_boundary_whitespace(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley significance spacing</h1>
<section><h2>Results</h2><p>See Figure 1.</p>
<figure id="fig1"><figcaption>
  <div><strong>Figure 1</strong></div>
  <div>Significance: *<i>P&#x2003;&lt;&#x2003;</i>0.05.</div>
</figcaption></figure></section>
</article></body></html>"""
        )

        caption = result.figures[0]
        self.assertEqual(
            caption.caption_markdown,
            "Significance: \\*<em>P &lt;</em> 0.05.",
        )
        self.assertEqual(caption.caption_plain, "Significance: *P < 0.05.")
        safe_html = block_markup_to_safe_html(
            caption.caption_markdown, kind="figure_caption"
        )
        self.assertTrue(rich_text_matches_plain(caption.caption_plain, safe_html))

    def test_numbered_image_alt_recovers_figure_and_scheme_captions(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>AACR image alt captions</h1><section><h2>Results</h2>
<figure id="figure-2"><img alt="Figure 2. Effect at 10 μmol/L on 5′-TTGGT-3′."></figure>
<figure id="scheme-3"><img alt="Scheme 3. Reagents A &amp; B &lt; C."></figure>
</section></article></body></html>"""
        )

        self.assertEqual(
            [(item.figure_id, item.kind, item.label) for item in result.figures],
            [
                ("figure_002", "figure", "Figure 2"),
                ("scheme_003", "scheme", "Scheme 3"),
            ],
        )
        self.assertEqual(
            [item.caption_plain for item in result.figures],
            ["Effect at 10 μmol/L on 5′-TTGGT-3′.", "Reagents A & B < C."],
        )
        self.assertEqual(
            [item.caption_markdown for item in result.figures],
            [
                "Effect at 10 μmol/L on 5′-TTGGT-3′.",
                "Reagents A &amp; B &lt; C.",
            ],
        )
        for caption in result.figures:
            safe_html = block_markup_to_safe_html(
                caption.caption_markdown, kind="figure_caption"
            )
            self.assertTrue(
                rich_text_matches_plain(caption.caption_plain, safe_html)
            )
            self.assertEqual(
                plain_text_from_safe_html(safe_html), caption.caption_plain
            )

    def test_acs_accessible_double_bond_and_soft_breaks_are_semantic_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Accessible chemical notation</h1><section><h2>Results</h2>
<p>The ν\u00ad(C<span role="img" aria-label="double bond"></span>O) mode and
ΔAbs\u00ad(A<sub>R</sub>) response were measured.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            "The ν(C=O) mode and ΔAbs(A_{R}) response were measured.",
        )
        self.assertEqual(
            block.markdown,
            "The ν(C=O) mode and ΔAbs(A<sub>R</sub>) response were measured.",
        )

    def test_acs_accessible_single_bond_is_semantic_em_dash(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Accessible reference punctuation</h1><section><h2>Results</h2>
<p>Current cancer therapies<span role="img" aria-label="single bond"></span>a guide.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.plain_text, "Current cancer therapies—a guide.")
        self.assertEqual(block.markdown, "Current cancer therapies—a guide.")

    def test_numbered_image_alt_rejects_empty_generic_and_punctuation_bodies(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Generic image alts</h1><section><h2>Results</h2>
<figure id="figure-1"><img alt=""></figure>
<figure id="figure-2"><img alt="Figure 2. Refer to the image caption for details."></figure>
<figure id="figure-3"><img alt="Figure 3. Description unavailable."></figure>
<figure id="figure-4"><img alt="Figure 4. The caption contains a description of this image."></figure>
<figure id="figure-5"><img alt="Figure 5. ..."></figure>
<figure id="figure-6"><img alt="thumbnail"></figure>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 6)
        self.assertTrue(
            all(not item.caption_plain and not item.caption_markdown for item in result.figures)
        )

    def test_semantic_figcaption_precedes_conflicting_numbered_image_alt(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Caption precedence</h1><section><h2>Results</h2>
<figure id="figure-7"><img alt="Figure 7. Conflicting alt caption."><figcaption>
  <div><strong>Figure 7</strong></div><div>Authored β caption.</div>
</figcaption></figure>
</section></article></body></html>"""
        )

        caption = result.figures[0]
        self.assertEqual(caption.caption_plain, "Authored β caption.")
        self.assertEqual(caption.caption_markdown, "Authored β caption.")
        safe_html = block_markup_to_safe_html(
            caption.caption_markdown, kind="figure_caption"
        )
        self.assertTrue(rich_text_matches_plain(caption.caption_plain, safe_html))
        self.assertEqual(plain_text_from_safe_html(safe_html), caption.caption_plain)

    def test_leading_decimals_keep_authored_space_during_punctuation_cleanup(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley leading decimals</h1>
<section><h2>Statistical analysis</h2>
<p>Data are expressed as mean&#xa0;±&#xa0;SD. Differences in mean values between groups were analyzed by the Student's <i>t</i>-test using JMP 13 (SAS Institute Inc., Cary, NC). <i>P</i>-values&#xa0;&lt;&#xa0;.01 or .05 were considered to be significant.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        expected_plain = (
            "Data are expressed as mean ± SD. Differences in mean values between "
            "groups were analyzed by the Student's t-test using JMP 13 (SAS "
            "Institute Inc., Cary, NC). P-values < .01 or .05 were considered "
            "to be significant."
        )
        self.assertEqual(block.plain_text, expected_plain)
        self.assertEqual(
            block.markdown,
            (
                "Data are expressed as mean ± SD. Differences in mean values "
                "between groups were analyzed by the Student's <em>t</em>-test "
                "using JMP 13 (SAS Institute Inc., Cary, NC). <em>P</em>-values "
                "&lt; .01 or .05 were considered to be significant."
            ),
        )
        safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
        self.assertEqual(plain_text_from_safe_html(safe_html), expected_plain)
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_unheaded_structural_introduction_is_not_folded_into_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Implicit introduction</h1>
<section id="abstract"><h2>Abstract</h2><p>Summary only.</p></section>
<section id="body">
  <section id="opening"><p>Opening context.</p><p>Study rationale.</p></section>
  <section id="methods"><h2>Methods</h2><p>Experimental details.</p></section>
</section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction", "Methods"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Summary only."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Opening context.", "Study rationale."],
        )
        self.assertEqual(
            result.sections[1].source_locator,
            "/html/body/article/section[2]/section[1]",
        )

    def test_unequal_diagram_rows_preserve_intentional_blank_cell(self) -> None:
        result = self.extract(
            """<!doctype html>
<html><body><article>
  <h1>Synthetic table study</h1>
  <section><h2>Data</h2><p>Results are tabulated.</p></section>
  <div id="tbl7">
    <header><strong>Table 7.</strong> Synthetic measurements</header>
    <table>
      <thead><tr><th>Entry</th><th>Species</th><th>Selectivity</th></tr></thead>
      <tbody>
        <tr><td><a href="#for001">-1</a></td><td>X<sub>2</sub></td><td></td></tr>
        <tr><td>7</td><td>product β</td></tr>
      </tbody>
    </table>
  </div>
</article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_kind, "html")
        rows = result.tables[0].parts[0].rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [cell.text for cell in rows[1]], ["7", "X_{2} product β", ""]
        )
        self.assertEqual(rows[1][1].markdown, "X<sub>2</sub><br>product β")
        self.assertEqual(rows[1][2].markdown, "")
        self.assertEqual(rows[1][2].as_dict()["html"], "")

    def test_standalone_bold_paragraph_becomes_subsection_heading(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Synthetic hierarchy</h1>
<section><h2>Methods</h2><p><b>Preparation details</b></p>
<p><b>Compound A</b>: Synthetic procedure.</p></section>
</article></body></html>"""
        )

        blocks = result.sections[0].blocks
        self.assertEqual(blocks[0].kind, "subsection_heading")
        self.assertEqual(blocks[0].markdown, "### Preparation details")
        self.assertEqual(blocks[1].kind, "paragraph")

    def test_long_standalone_bold_callout_remains_paragraph(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Synthetic hierarchy</h1>
<section><h2>Significance</h2><div id="p0145"><strong>This authored significance
callout is fully bold for visual emphasis, but it contains several complete
sentences and substantially more text than a plausible subsection heading.
It must remain prose so downstream readers do not mistake an entire summary
paragraph for document hierarchy.</strong></div></section>
</article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "paragraph")
        self.assertTrue(block.markdown.startswith("<strong>This authored"))
        self.assertTrue(block.markdown.endswith("hierarchy.</strong>"))


class ReferenceEntryOverrideTests(unittest.TestCase):
    @staticmethod
    def source() -> SourceFile:
        return SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )

    @staticmethod
    def block(number: int, value: str) -> ContentBlock:
        return ContentBlock(
            block_id=f"reference-{number:03d}",
            kind="reference",
            markdown=f"{number}. {value}",
            plain_text=f"{number}. {value}",
            source_path="papers (private)/00001/html/main.html",
            source_locator=f"#ref-{number}",
        )

    def test_exact_source_entries_replace_and_contiguously_extend(self) -> None:
        source = self.source()
        article = SimpleNamespace(
            references=[self.block(1, "Complete."), self.block(2, "Truncated")]
        )
        _apply_reference_entries(
            article,
            [
                {
                    "number": 2,
                    "value": "Recovered second reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, reference 2",
                },
                {
                    "number": 3,
                    "value": "Recovered third reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 2, reference 3",
                    "source_geometry": [
                        {"page": 2, "bbox": [72.0, 300.0, 500.0, 330.0]}
                    ],
                },
            ],
            [source],
        )

        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "1. Complete.",
                "2. Recovered second reference.",
                "3. Recovered third reference.",
            ],
        )
        self.assertEqual(article.references[2].source_path, source.relative_path)
        self.assertEqual(
            article.references[2].source_geometry,
            [{"page": 2, "bbox": [72.0, 300.0, 500.0, 330.0]}],
        )
        self.assertEqual(
            article.references[2].source_locator,
            "PDF page 2, box [72.00, 300.00, 500.00, 330.00]",
        )

    def test_declared_reference_source_hash_rejects_stale_snapshot(self) -> None:
        source = self.source()
        article = SimpleNamespace(references=[self.block(1, "Complete.")])
        spec = {
            "number": 1,
            "value": "Reviewed first reference.",
            "source_path": source.relative_path,
            "source_sha256": "f" * 64,
            "source_locator": "physical page 1, reference 1",
        }
        with self.assertRaisesRegex(ExtractionError, "invalid reference_entries"):
            _apply_reference_entries(article, [spec], [source])

    def test_sciencedirect_bracketed_sequence_extends_in_the_same_style(self) -> None:
        source = self.source()
        first = self.block(1, "Complete.")
        first.markdown = "[1]. Complete."
        first.plain_text = "[1]. Complete."
        article = SimpleNamespace(references=[first])

        _apply_reference_entries(
            article,
            [
                {
                    "number": 2,
                    "value": "Recovered second reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, reference 2",
                }
            ],
            [source],
        )

        self.assertEqual(
            [block.plain_text for block in article.references],
            ["[1]. Complete.", "[2]. Recovered second reference."],
        )

    def test_closing_parenthesis_sequence_extends_in_the_same_style(self) -> None:
        source = self.source()
        first = self.block(1, "Complete.")
        first.markdown = "1) Complete."
        first.plain_text = "1) Complete."
        article = SimpleNamespace(references=[first])

        _apply_reference_entries(
            article,
            [
                {
                    "number": 2,
                    "value": "Recovered second reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, reference 2",
                }
            ],
            [source],
        )

        self.assertEqual(
            [block.plain_text for block in article.references],
            ["1) Complete.", "2) Recovered second reference."],
        )

    def test_review_interest_marks_do_not_break_contiguous_numbering(self) -> None:
        source = self.source()
        first = self.block(1, "Complete.")
        second = self.block(2, "Outstanding review reference.")
        second.markdown = second.plain_text = "2.•• Outstanding review reference."
        article = SimpleNamespace(references=[first, second])

        _apply_reference_entries(
            article,
            [
                {
                    "number": 2,
                    "value": "Reviewed second reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, reference 2",
                }
            ],
            [source],
        )

        self.assertEqual(article.references[1].plain_text, "2. Reviewed second reference.")

    def test_fullwidth_closing_parenthesis_is_normalized_by_reviewed_entries(self) -> None:
        source = self.source()
        first = self.block(1, "Complete.")
        first.markdown = first.plain_text = "1） Complete."
        article = SimpleNamespace(references=[first])

        _apply_reference_entries(
            article,
            [
                {
                    "number": 1,
                    "value": "Reviewed first reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, reference 1",
                }
            ],
            [source],
        )

        self.assertEqual(article.references[0].plain_text, "1) Reviewed first reference.")

    def test_replacement_from_same_source_retains_existing_geometry(self) -> None:
        source = self.source()
        first = self.block(1, "Corrupt native text.")
        first.source_path = source.relative_path
        first.source_geometry = [
            {"page": 7, "bbox": [45.0, 700.0, 290.0, 720.0]}
        ]
        article = SimpleNamespace(references=[first])

        _apply_reference_entries(
            article,
            [
                {
                    "number": 1,
                    "value": "Reviewed source text.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 7, reference 1",
                }
            ],
            [source],
        )

        self.assertEqual(
            article.references[0].source_geometry,
            [{"page": 7, "bbox": [45.0, 700.0, 290.0, 720.0]}],
        )
        self.assertEqual(article.references[0].source_locator, "#ref-1")

    def test_unlabeled_canonical_sequence_extends_without_inventing_labels(self) -> None:
        source = self.source()
        first = self.block(1, "First author-year reference.")
        second = self.block(2, "Second author-year reference.")
        first.markdown = first.plain_text = "First author-year reference."
        second.markdown = second.plain_text = "Second author-year reference."
        article = SimpleNamespace(references=[first, second])

        _apply_reference_entries(
            article,
            [
                {
                    "number": 3,
                    "value": "Recovered third author-year reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 2, reference 3",
                }
            ],
            [source],
        )

        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "First author-year reference.",
                "Second author-year reference.",
                "Recovered third author-year reference.",
            ],
        )

    def test_unlabeled_sequence_with_noncanonical_identity_fails_closed(self) -> None:
        source = self.source()
        first = self.block(1, "First author-year reference.")
        first.markdown = first.plain_text = "First author-year reference."
        first.block_id = "reference-002"
        with self.assertRaisesRegex(ExtractionError, "contiguous numbered bibliography"):
            _apply_reference_entries(
                SimpleNamespace(references=[first]),
                [
                    {
                        "number": 2,
                        "value": "Recovered second reference.",
                        "source_path": source.relative_path,
                        "source_locator": "physical page 2, reference 2",
                    }
                ],
                [source],
            )

    def test_mixed_existing_reference_label_styles_fail_closed(self) -> None:
        source = self.source()
        second = self.block(2, "Complete.")
        second.markdown = "[2]. Complete."
        second.plain_text = "[2]. Complete."
        article = SimpleNamespace(
            references=[self.block(1, "Complete."), second]
        )

        with self.assertRaisesRegex(ExtractionError, "contiguous numbered bibliography"):
            _apply_reference_entries(
                article,
                [
                    {
                        "number": 3,
                        "value": "Recovered third reference.",
                        "source_path": source.relative_path,
                        "source_locator": "physical page 1, reference 3",
                    }
                ],
                [source],
            )

    def test_reference_entry_gap_fails_closed(self) -> None:
        source = self.source()
        article = SimpleNamespace(references=[self.block(1, "Complete.")])
        with self.assertRaisesRegex(ExtractionError, "bibliography gap"):
            _apply_reference_entries(
                article,
                [
                    {
                        "number": 3,
                        "value": "Gap.",
                        "source_path": source.relative_path,
                        "source_locator": "physical page 1, reference 3",
                    }
                ],
                [source],
            )


class FrontMatterOverrideTests(unittest.TestCase):
    def test_matching_label_replaces_partial_publisher_block(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )
        article = SimpleNamespace(
            front_matter=[
                ContentBlock(
                    block_id="front-matter-html-001",
                    kind="front_matter",
                    markdown="**Affiliation assignments:** Alice Example (a)",
                    plain_text="Affiliation assignments: Alice Example (a)",
                    source_path="papers (private)/00001/html/main.html",
                    source_locator="//*[@id='author-group']",
                )
            ]
        )

        _apply_front_matter_overrides(
            article,
            [
                {
                    "label": "Affiliation assignments",
                    "value": "Alice Example (a); Bob Example (b)",
                    "source_path": source.relative_path,
                    "source_sha256": source.sha256,
                    "source_locator": "physical page 1, author line",
                    "source_geometry": [
                        {"page": 1, "bbox": [72, 110, 500, 145]}
                    ],
                }
            ],
            [source],
        )

        self.assertEqual(len(article.front_matter), 1)
        self.assertEqual(
            article.front_matter[0].plain_text,
            "Affiliation assignments: Alice Example (a); Bob Example (b)",
        )
        self.assertEqual(article.front_matter[0].source_path, source.relative_path)
        self.assertEqual(
            article.front_matter[0].source_geometry,
            [{"page": 1, "bbox": [72, 110, 500, 145]}],
        )

    def test_front_matter_override_rejects_invalid_source_geometry(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )
        with self.assertRaisesRegex(ExtractionError, "source_geometry item 1 is invalid"):
            _apply_front_matter_overrides(
                SimpleNamespace(front_matter=[]),
                [
                    {
                        "label": "Affiliation",
                        "value": "Synthetic University",
                        "source_path": source.relative_path,
                        "source_sha256": source.sha256,
                        "source_locator": "physical page 1",
                        "source_geometry": [
                            {"page": 1, "bbox": [72, 145, 500, 110]}
                        ],
                    }
                ],
                [source],
            )

        with self.assertRaisesRegex(ExtractionError, "discovered source evidence"):
            _apply_front_matter_overrides(
                SimpleNamespace(front_matter=[]),
                [
                    {
                        "label": "Affiliation",
                        "value": "Stale evidence",
                        "source_path": source.relative_path,
                        "source_sha256": "f" * 64,
                        "source_locator": "physical page 1",
                    }
                ],
                [source],
            )


class PathSafetyAndDeterminismTests(unittest.TestCase):
    def test_record_ids_and_parent_traversal_are_rejected(self) -> None:
        self.assertEqual(validate_record_id("01234"), "01234")
        for unsafe_id in ("1234", "123456", "../12", "12/34", "abcde"):
            with self.subTest(record_id=unsafe_id):
                with self.assertRaises(ValueError):
                    validate_record_id(unsafe_id)

        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "approved"
            root.mkdir()
            inside = root / "inside.txt"
            outside = temporary / "outside.txt"
            inside.write_text("inside", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")

            self.assertEqual(ensure_within(inside, root), inside.resolve())
            with self.assertRaises(UnsafePathError):
                ensure_within(root / ".." / "outside.txt", root)

    def test_canonical_json_is_stable_unicode_and_key_sorted(self) -> None:
        first = {"z": [3, 2, 1], "a": "β"}
        second = {"a": "β", "z": [3, 2, 1]}

        rendered = canonical_json(first)

        self.assertEqual(rendered, canonical_json(second))
        self.assertEqual(rendered, '{\n  "a": "β",\n  "z": [\n    3,\n    2,\n    1\n  ]\n}\n')

    def test_table_derivatives_include_typed_scientific_records_without_csv(self) -> None:
        table = TableItem(
            table_id="table_001",
            source_id="tbl1",
            label="Table 1",
            title_markdown="K<sub>a</sub>",
            title_plain="K_{a} [M^{−1}] measurements",
            parts=[
                TablePart(
                    part_id="part-01",
                    rows=[
                        [
                            TableCell("Polyamide on pTEST", "Polyamide on pTEST", True),
                            TableCell("5′-aTGGACAt-3′", "5′-aTGGACAt-3′", True),
                        ],
                        [
                            TableCell("2 b", "2 b", False),
                            TableCell(
                                "≤1×10^{8} [≥190]^{[e]}",
                                "<strong>≤1×10<sup>8</sup></strong>",
                                False,
                            ),
                        ],
                    ],
                )
            ],
            footnotes_markdown=["[a] Synthetic scope. [e] Alternate match."],
            footnotes_plain=["[a] Synthetic scope. [e] Alternate match."],
            source_path="html/main.html",
            source_locator="//table[1]",
            source_kind="html",
            structure_assets={"2b": "tables/main/table_001_cells/structure_2b.png"},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            structure_image = (
                root
                / "tables"
                / "main"
                / "table_001_cells"
                / "structure_2b.png"
            )
            structure_image.parent.mkdir(parents=True)
            structure_image.write_bytes(b"synthetic table-cell image")
            write_table_derivatives([table], root)
            payload = json.loads((root / table.json_path).read_text(encoding="utf-8"))
            record = payload["machine_records"][0]

            self.assertEqual(record["compound_id"], "2b")
            self.assertEqual(
                record["structure_asset"],
                "tables/main/table_001_cells/structure_2b.png",
            )
            self.assertEqual(record["association_constant"]["relation"], "<=")
            self.assertEqual(record["association_constant"]["exponent"], 8)
            self.assertEqual(record["specificity"], {"relation": ">=", "value": 190.0})
            self.assertTrue(record["match_site"])
            self.assertEqual(record["footnotes"], ["e"])
            self.assertEqual(
                payload["footnotes_typed"],
                [
                    {"label": "a", "scope": "table", "text": "Synthetic scope."},
                    {"label": "e", "scope": "table", "text": "Alternate match."},
                ],
            )
            self.assertEqual(payload["source_kind"], "html")
            self.assertNotIn("source", payload)
            self.assertEqual(list(root.rglob("*.csv")), [])
            self.assertEqual(list(root.rglob("table_001.png")), [])
            self.assertTrue(structure_image.is_file())

    def test_table_schema_marker_is_the_only_change_to_faithful_json_content(self) -> None:
        table = TableItem(
            table_id="table_009",
            source_id="tbl9",
            label="Table 9",
            title_markdown="β scope and selectivity",
            title_plain="β scope and selectivity",
            parts=[
                TablePart(
                    part_id="tbl9-part-01",
                    rows=[
                        [
                            TableCell(
                                "Condition",
                                "<strong>Condition</strong>",
                                True,
                                rowspan=2,
                            ),
                            TableCell("Results", "Results", True, colspan=2),
                        ],
                        [
                            TableCell("Yield (%)", "Yield (%)", True),
                            TableCell("ee (%)", "<em>ee</em> (%)", True),
                        ],
                        [
                            TableCell("A", "A", False),
                            TableCell(
                                "≤1×10^{−3}",
                                "≤1×10<sup>−3</sup>",
                                False,
                            ),
                        ],
                        [TableCell("Not reported", "<span>Not reported</span>", False)],
                    ],
                ),
                TablePart(
                    part_id="tbl9-part-02",
                    rows=[
                        [TableCell("Continued", "<u>Continued</u>", True)],
                        [TableCell("β-form", "β-form", False)],
                    ],
                ),
            ],
            footnotes_markdown=[
                "[a] Values are means; *n* = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            footnotes_plain=[
                "[a] Values are means; n = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            source_path="html/main.html",
            source_locator="//table[9]",
            source_kind="html",
            structure_assets={
                "β-form": "tables/main/table_009_cells/structure_beta.png"
            },
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_table_derivatives([table], root)
            payload = json.loads((root / str(table.json_path)).read_text("utf-8"))

        # This is the complete pre-schema table payload. Removing the new
        # path-base declaration must leave it byte-for-data identical: the
        # schema describes this representation and does not normalize it.
        expected_pre_schema_payload = {
            "schema_version": "1.0",
            "table_id": "table_009",
            "label": "Table 9",
            "title": "β scope and selectivity",
            "parts": [
                {
                    "part_id": "tbl9-part-01",
                    "rows": [
                        [
                            {
                                "text": "Condition",
                                "html": "<strong>Condition</strong>",
                                "header": True,
                                "rowspan": 2,
                                "colspan": 1,
                            },
                            {
                                "text": "Results",
                                "html": "Results",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 2,
                            },
                        ],
                        [
                            {
                                "text": "Yield (%)",
                                "html": "Yield (%)",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                            {
                                "text": "ee (%)",
                                "html": "<em>ee</em> (%)",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                        ],
                        [
                            {
                                "text": "A",
                                "html": "A",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                            {
                                "text": "≤1×10^{−3}",
                                "html": "≤1×10<sup>−3</sup>",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                        ],
                        [
                            {
                                "text": "Not reported",
                                "html": "<span>Not reported</span>",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                    ],
                },
                {
                    "part_id": "tbl9-part-02",
                    "rows": [
                        [
                            {
                                "text": "Continued",
                                "html": "<u>Continued</u>",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                        [
                            {
                                "text": "β-form",
                                "html": "β-form",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                    ],
                },
            ],
            "footnotes": [
                "[a] Values are means; n = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            "footnotes_typed": [
                {
                    "label": "a",
                    "scope": "table",
                    "text": "Values are means; n = 3.",
                },
                {
                    "label": "e",
                    "scope": "table",
                    "text": "As reported by the authors—unchanged.",
                },
            ],
            "structure_assets": {
                "β-form": "tables/main/table_009_cells/structure_beta.png"
            },
            "machine_records": [],
            "source_kind": "html",
        }
        self.assertEqual(payload["asset_path_base"], "extraction_root")
        self.assertEqual(
            {key: value for key, value in payload.items() if key != "asset_path_base"},
            expected_pre_schema_payload,
        )
        self.assertEqual(
            [len(row) for row in payload["parts"][0]["rows"]], [2, 2, 2, 1]
        )
        self.assertEqual(len(payload["parts"]), 2)
        validate_table_payload(payload)

    def test_every_table_emits_json_even_when_rows_are_not_rectangular(self) -> None:
        tables = [
            TableItem(
                table_id="table_001",
                source_id="tbl1",
                label="Table 1",
                title_markdown="Complete table",
                title_plain="Complete table",
                parts=[
                    TablePart(
                        part_id="tbl1-part-01",
                        rows=[
                            [
                                TableCell("Name", "Name", True),
                                TableCell("Value", "Value", True),
                            ],
                            [
                                TableCell("alpha", "alpha", False),
                                TableCell("1", "1", False),
                            ],
                        ],
                    )
                ],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path="html/main.html",
                source_locator="//table[1]",
                source_kind="html",
            ),
            TableItem(
                table_id="table_002",
                source_id="tbl2",
                label="Table 2",
                title_markdown="Ragged table",
                title_plain="Ragged table",
                parts=[
                    TablePart(
                        part_id="tbl2-part-01",
                        rows=[
                            [
                                TableCell("Name", "Name", True),
                                TableCell("Value", "Value", True),
                            ],
                            [TableCell("beta", "beta", False)],
                        ],
                    )
                ],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path="html/main.html",
                source_locator="//table[2]",
                source_kind="html",
            ),
        ]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_table_derivatives(tables, root)

            for table in tables:
                with self.subTest(table_id=table.table_id):
                    json_path = root / "tables" / "main" / f"{table.table_id}.json"
                    self.assertTrue(json_path.is_file())
                    payload = json.loads(json_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["table_id"], table.table_id)
                    self.assertEqual(payload["source_kind"], "html")
                    self.assertNotIn("source", payload)
            self.assertEqual(list(root.rglob("*.csv")), [])
            self.assertFalse(
                any(
                    path.is_dir() and path.name.endswith("_cells")
                    for path in root.rglob("*")
                )
            )

    def test_table_image_requirements_apply_only_to_pdf_and_image_sources(self) -> None:
        def make_table(number: int, source_kind: str) -> TableItem:
            return TableItem(
                table_id=f"table_{number:03d}",
                source_id=f"tbl{number}",
                label=f"Table {number}",
                title_markdown=f"Table {number}",
                title_plain=f"Table {number}",
                parts=[],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path=f"{source_kind}/table-{number}",
                source_locator=f"table {number}",
                source_kind=source_kind,
            )

        html_table = make_table(1, "html")
        pdf_table = make_table(2, "pdf")
        image_table = make_table(3, "image")
        article = SimpleNamespace(
            figures=[],
            tables=[html_table, pdf_table, image_table],
            warnings=[],
        )

        _attach_assets(article, [], [])
        _record_missing_asset_warnings(article, [])

        missing_table_warnings = [
            warning
            for warning in article.warnings
            if warning["code"] == "main_table_source_image_missing"
        ]
        self.assertEqual(
            [warning["source_path"] for warning in missing_table_warnings],
            [pdf_table.source_path, image_table.source_path],
        )
        self.assertNotIn(
            html_table.source_path,
            {warning["source_path"] for warning in missing_table_warnings},
        )

        article.warnings.clear()
        _attach_assets(
            article,
            [],
            [
                {
                    "asset_id": pdf_table.table_id,
                    "output_path": "tables/main/table_002.png",
                },
                {
                    "asset_id": image_table.table_id,
                    "output_path": "tables/main/table_003.png",
                },
            ],
        )
        _record_missing_asset_warnings(article, [])

        self.assertIsNone(html_table.image_path)
        self.assertEqual(pdf_table.image_path, "tables/main/table_002.png")
        self.assertEqual(image_table.image_path, "tables/main/table_003.png")
        self.assertEqual(article.warnings, [])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for table in (pdf_table, image_table):
                image_path = root / str(table.image_path)
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(b"synthetic full-table image")

            write_table_derivatives(article.tables, root)

            self.assertFalse((root / "tables/main/table_001.png").exists())
            for table in article.tables:
                payload = json.loads(
                    (root / f"tables/main/{table.table_id}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["source_kind"], table.source_kind)
                self.assertNotIn("source", payload)
                if table.requires_source_image:
                    self.assertTrue((root / str(table.image_path)).is_file())
            self.assertEqual(list(root.rglob("*.csv")), [])


class TableSchemaContractTests(unittest.TestCase):
    def test_typed_table_footnotes_recognize_parenthesized_suffix_marker(self) -> None:
        table = SimpleNamespace(
            footnotes_plain=["a) Abbreviations: DNMTi, DNA methyl transferase inhibitor."]
        )

        self.assertEqual(
            typed_table_footnotes(table),
            [
                {
                    "label": "a",
                    "scope": "table",
                    "text": "Abbreviations: DNMTi, DNA methyl transferase inhibitor.",
                }
            ],
        )

    def test_typed_table_footnotes_recognize_roman_submarkers(self) -> None:
        table = SimpleNamespace(
            footnotes_plain=[
                "[a] Ratio definition. [i] One-site fit. [ii] Langmuir fit."
            ]
        )

        self.assertEqual(
            typed_table_footnotes(table),
            [
                {"label": "a", "scope": "table", "text": "Ratio definition."},
                {"label": "i", "scope": "table", "text": "One-site fit."},
                {"label": "ii", "scope": "table", "text": "Langmuir fit."},
            ],
        )

    @staticmethod
    def valid_payload(source_kind: str = "html") -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "asset_path_base": "extraction_root",
            "table_id": "table_001",
            "label": "Table 1",
            "title": "Faithful synthetic table",
            "parts": [
                {
                    "part_id": "part-01",
                    "rows": [
                        [
                            {
                                "text": "α",
                                "html": "<em>α</em><sup>2</sup>",
                                "header": True,
                                "rowspan": 2,
                                "colspan": 3,
                            }
                        ],
                        [],
                    ],
                },
                {"part_id": "part-02", "rows": []},
            ],
            "footnotes": ["[e] Authors' wording; retained verbatim."],
            "footnotes_typed": [
                {
                    "label": "e",
                    "scope": "table",
                    "text": "Authors' wording; retained verbatim.",
                }
            ],
            "structure_assets": {},
            "machine_records": [],
            "source_kind": source_kind,
        }
        if source_kind in {"pdf", "image"}:
            payload["source_image"] = "tables/main/table_001.png"
        return payload

    def test_schema_accepts_faithful_multipart_spans_html_and_ragged_rows(self) -> None:
        payload = self.valid_payload()
        before = deepcopy(payload)

        validate_table_payload(payload)

        self.assertEqual(payload, before)
        self.assertEqual(payload["parts"][0]["rows"][1], [])  # type: ignore[index]
        self.assertEqual(
            payload["parts"][0]["rows"][0][0]["html"],  # type: ignore[index]
            "<em>α</em><sup>2</sup>",
        )
        self.assertEqual(
            payload["footnotes"], ["[e] Authors' wording; retained verbatim."]
        )

    def test_schema_rejects_invalid_container_without_rewriting_it(self) -> None:
        cases: dict[str, object] = {}

        missing_title = self.valid_payload()
        del missing_title["title"]
        cases["missing required field"] = missing_title

        wrong_version = self.valid_payload()
        wrong_version["schema_version"] = "2.0"
        cases["unsupported schema version"] = wrong_version

        wrong_path_base = self.valid_payload()
        wrong_path_base["asset_path_base"] = "table_directory"
        cases["ambiguous asset path base"] = wrong_path_base

        invalid_span = self.valid_payload()
        invalid_span["parts"][0]["rows"][0][0]["rowspan"] = 0  # type: ignore[index]
        cases["invalid cell span"] = invalid_span

        unexpected_field = self.valid_payload()
        unexpected_field["publisher_layout"] = "normalized"
        cases["undefined container field"] = unexpected_field

        cases["non-object root"] = []

        for description, payload in cases.items():
            with self.subTest(description=description):
                before = deepcopy(payload)
                with self.assertRaises(TableSchemaError) as raised:
                    validate_table_payload(payload)
                self.assertTrue(raised.exception.violations)
                self.assertEqual(payload, before)

    def test_source_image_is_required_only_for_pdf_or_image_sources(self) -> None:
        for source_kind in ("html", "document", "presentation", "spreadsheet"):
            with self.subTest(source_kind=source_kind):
                validate_table_payload(self.valid_payload(source_kind))
                native_with_image = self.valid_payload(source_kind)
                native_with_image["source_image"] = "tables/main/table_001.png"
                with self.assertRaises(TableSchemaError):
                    validate_table_payload(native_with_image)

        for source_kind in ("pdf", "image"):
            with self.subTest(source_kind=source_kind):
                payload = self.valid_payload(source_kind)
                validate_table_payload(payload)
                del payload["source_image"]
                with self.assertRaises(TableSchemaError):
                    validate_table_payload(payload)


class IndependentValidatorTests(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.write_text(canonical_json(value), encoding="utf-8")

    def test_html_table_requires_structured_json_link_but_not_source_image(self) -> None:
        record_text = """# Synthetic article

## Tables

### Table 1

Assets: [structured data](tables/main/table_001.json)

<table><tr><th>Value</th></tr><tr><td>1</td></tr></table>
"""

        self.assertEqual(_required_asset_findings(record_text), [])

        findings_without_json = _required_asset_findings(
            record_text.replace(
                "Assets: [structured data](tables/main/table_001.json)\n\n", ""
            )
        )
        self.assertEqual(
            [finding.code for finding in findings_without_json],
            ["missing_consolidated_asset"],
        )
        self.assertIn("structured JSON", findings_without_json[0].message)

    def test_table_csv_is_an_unexpected_derivative(self) -> None:
        with TemporaryDirectory() as directory:
            extraction = Path(directory)
            table_root = extraction / "tables" / "main"
            table_root.mkdir(parents=True)
            self.write_json(
                table_root / "table_001.json",
                TableSchemaContractTests.valid_payload(),
            )
            csv_path = table_root / "table_001.csv"
            csv_path.write_text("name,value\nalpha,1\n", encoding="utf-8")

            findings = _table_derivative_findings(
                extraction,
                {Path("tables/main/table_001.json")},
            )

            csv_findings = [
                finding for finding in findings if finding.code == "unexpected_table_csv"
            ]
            self.assertEqual(len(csv_findings), 1)
            self.assertEqual(csv_findings[0].path, "tables/main/table_001.csv")

    def test_malformed_and_schema_invalid_table_json_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            extraction = Path(directory)
            table_root = extraction / "tables" / "main"
            table_root.mkdir(parents=True)

            malformed_path = table_root / "table_001.json"
            malformed_path.write_text('{"schema_version": "1.0",', encoding="utf-8")

            schema_invalid_path = table_root / "table_002.json"
            schema_invalid = TableSchemaContractTests.valid_payload()
            schema_invalid["table_id"] = "table_002"
            del schema_invalid["asset_path_base"]
            self.write_json(schema_invalid_path, schema_invalid)

            findings = _table_derivative_findings(
                extraction,
                {
                    Path("tables/main/table_001.json"),
                    Path("tables/main/table_002.json"),
                },
            )

            rejected = [
                (finding.code, finding.path, finding.severity)
                for finding in findings
                if finding.code in {"invalid_table_json", "invalid_table_schema"}
            ]
            self.assertEqual(
                rejected,
                [
                    (
                        "invalid_table_json",
                        "tables/main/table_001.json",
                        "critical",
                    ),
                    (
                        "invalid_table_schema",
                        "tables/main/table_002.json",
                        "structural",
                    ),
                ],
            )

    def test_reports_unsafe_links_placeholders_orphans_hashes_and_counts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            assets = extraction / "assets"
            assets.mkdir(parents=True)
            diagnostic.mkdir()

            (extraction / "record.md").write_text(
                """# Synthetic validation article

[Unsafe](../outside.txt)
[Unsafe scheme](javascript:alert)
[Missing](assets/missing.bin)
[Linked](assets/linked.bin)

Unresolved publisher token: equation/tex2gif-sup-42.gif

## Figures

Figure 1. A wholly synthetic result.
""",
                encoding="utf-8",
            )
            linked = assets / "linked.bin"
            linked.write_bytes(b"linked synthetic asset")
            (extraction / "orphan.bin").write_bytes(b"unlinked synthetic asset")

            self.write_json(
                diagnostic / "manifest.json",
                {
                    "schema_version": "1.0",
                    "files": [
                        {
                            "output_path": "assets/linked.bin",
                            "sha256": "0" * 64,
                        },
                        {
                            "output_path": "../escaped.bin",
                            "sha256": "1" * 64,
                        },
                    ],
                },
            )
            self.write_json(diagnostic / "sources.json", {"sources": []})
            (diagnostic / "coverage.jsonl").write_text("", encoding="utf-8")
            self.write_json(diagnostic / "quality.json", {"schema_version": "1.0"})
            self.write_json(
                diagnostic / "confidence.json",
                {
                    "schema_version": "1.0",
                    "categories": {
                        "synthetic": {"level": "high", "basis": "synthetic fixture"}
                    },
                },
            )
            (diagnostic / "warnings.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "code": "main_table_source_image_missing",
                        "severity": "scientific",
                        "message": "A PDF-sourced table has no full-table image.",
                        "source_path": "pdf/main.pdf",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            expected_counts = {"main_figures": 2}
            first = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic validation article",
                expected_counts=expected_counts,
            )
            second = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic validation article",
                expected_counts=expected_counts,
            )

            codes = {finding.code for finding in first.findings}
            self.assertTrue(
                {
                    "unsafe_local_link",
                    "unsafe_external_scheme",
                    "missing_local_link",
                    "publisher_equation_placeholder",
                    "orphan_output_file",
                    "output_hash_mismatch",
                    "invalid_manifest_path",
                    "content_count_mismatch",
                    "diagnostic_warning_main_table_source_image_missing",
                }.issubset(codes)
            )
            self.assertEqual(first.status, "fail")
            self.assertEqual(first.counts["main_figures"], 0)
            self.assertEqual(first.as_dict(), second.as_dict())

    def test_valid_candidate_with_matching_hash_and_count_passes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            assets = extraction / "figures"
            assets.mkdir(parents=True)
            diagnostic.mkdir()

            (extraction / "record.md").write_text(
                """# Synthetic passing article

## Figure and Scheme Captions

### Figure 1

Asset: [Figure 1](figures/figure_001.png)

Synthetic caption.
""",
                encoding="utf-8",
            )
            image = assets / "figure_001.png"
            image.write_bytes(b"synthetic image bytes")

            self.write_json(
                diagnostic / "manifest.json",
                {
                    "files": [
                        {
                            "path": "figures/figure_001.png",
                            "bytes": image.stat().st_size,
                            "sha256": sha256_file(image),
                        },
                        {
                            "path": "record.md",
                            "bytes": (extraction / "record.md").stat().st_size,
                            "sha256": sha256_file(extraction / "record.md"),
                        },
                    ]
                },
            )
            self.write_json(diagnostic / "sources.json", {"sources": []})
            (diagnostic / "coverage.jsonl").write_text(
                json.dumps(
                    {
                        "coverage_id": "synthetic-record",
                        "content_kind": "article",
                        "status": "included",
                        "source_path": "generated",
                        "source_locator": "synthetic validation fixture",
                        "output_path": "record.md",
                        "output_locator": {"start_line": 1, "end_line": 9},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.write_json(diagnostic / "quality.json", {"schema_version": "1.0"})
            self.write_json(
                diagnostic / "confidence.json",
                {
                    "schema_version": "1.0",
                    "categories": {
                        "synthetic": {"level": "high", "basis": "synthetic fixture"}
                    },
                },
            )

            report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic passing article",
                expected_counts={"main_figures": 1},
            )

            self.assertEqual(report.findings, ())
            self.assertTrue(report.passed)

            (extraction / "record.md").write_text(
                """# Synthetic passing article

## Local Assets

- [Figure 1](figures/figure_001.png)
""",
                encoding="utf-8",
            )
            truncated = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic passing article",
                expected_counts={"main_figures": 1},
            )
            self.assertIn(
                "content_count_mismatch",
                {finding.code for finding in truncated.findings},
            )


if __name__ == "__main__":
    unittest.main()
