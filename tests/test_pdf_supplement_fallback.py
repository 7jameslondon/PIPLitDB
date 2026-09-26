from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from reportlab.pdfgen import canvas

from scripts.extraction.models import ContentBlock, SourceFile
from scripts.extraction.supplements import (
    _apply_decoded_line_text_replacements,
    _apply_supplement_block_text_replacements,
    _apply_supplement_text_replacements,
    _figure_graphic_regions,
    _supplement_text_replacements,
    _verify_supplement_text_replacements,
    extract_supplements,
)


class PdfSupplementFallbackTests(unittest.TestCase):
    @staticmethod
    def _native_pdf(path: Path) -> bytes:
        document = canvas.Canvas(str(path))
        document.drawString(72, 740, "Reporting Summary")
        document.drawString(72, 720, "1. Native checklist text.")
        document.showPage()
        document.drawString(72, 740, "2. Second native page.")
        document.save()
        return path.read_bytes()

    def test_hash_gated_pypdf_engine_extracts_native_lines_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "checklist.pdf"
            source_bytes = self._native_pdf(path)
            relative = "papers (private)/00001/supplementary/checklist.pdf"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format="application/pdf",
            )

            supplement = extract_supplements(
                [source],
                root / "extraction",
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": relative,
                            "source_sha256": source_hash,
                            "native_text_engine": "pypdf",
                        }
                    ]
                },
            )[0]

            self.assertEqual(supplement.warnings, [])
            self.assertEqual(
                [block.plain_text for block in supplement.blocks],
                [
                    "Reporting Summary",
                    "1. Native checklist text.",
                    "2. Second native page.",
                ],
            )
            self.assertTrue(
                all("pypdf-native-line=" in block.source_locator for block in supplement.blocks)
            )
            self.assertEqual(
                (root / "extraction" / supplement.copied_path).read_bytes(),
                source_bytes,
            )

    def test_hash_gated_native_reading_regions_preserve_reviewed_column_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "columns.pdf"
            document = canvas.Canvas(str(path))
            document.drawString(72, 740, "Left first")
            document.drawString(320, 740, "Right third")
            document.drawString(72, 720, "Left second")
            document.drawString(320, 720, "Right fourth")
            document.save()
            source_bytes = path.read_bytes()
            relative = "papers (private)/00001/supplementary/columns.pdf"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format="application/pdf",
            )

            supplement = extract_supplements(
                [source],
                root / "extraction",
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": relative,
                            "source_sha256": source_hash,
                            "text_stream": "decoded",
                            "native_reading_regions": [
                                {
                                    "region_id": "left",
                                    "page": 1,
                                    "box": [45, 35, 300, 150],
                                },
                                {
                                    "region_id": "right",
                                    "page": 1,
                                    "box": [300, 35, 570, 150],
                                },
                            ],
                        }
                    ]
                },
            )[0]

            extracted = "\n".join(block.plain_text for block in supplement.blocks)
            self.assertLess(extracted.index("Left first"), extracted.index("Left second"))
            self.assertLess(extracted.index("Left second"), extracted.index("Right third"))
            self.assertLess(extracted.index("Right third"), extracted.index("Right fourth"))

    def test_hash_gated_block_text_replacement_repairs_joined_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "checklist.pdf"
            source_bytes = self._native_pdf(path)
            relative = "papers (private)/00001/supplementary/checklist.pdf"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format="application/pdf",
            )

            supplement = extract_supplements(
                [source],
                root / "extraction",
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": relative,
                            "source_sha256": source_hash,
                            "native_text_engine": "pypdf",
                            "block_text_replacements": [
                                {
                                    "old": "Reporting Summary",
                                    "new": "Reporting overview",
                                    "expected_hits": 1,
                                    "reason": "Synthetic joined-block defect.",
                                    "evidence": "Synthetic rendered evidence.",
                                }
                            ],
                        }
                    ]
                },
            )[0]

            self.assertEqual(supplement.blocks[0].plain_text, "Reporting overview")
            self.assertEqual(supplement.blocks[0].markdown, "Reporting overview")
            self.assertEqual(supplement.warnings, [])
            self.assertEqual(supplement.repairs[0]["mode"], "exact_literal_post_block")

    def test_block_text_replacement_crosses_decoded_rich_text_boundary(self) -> None:
        replacements = [
            {
                "old": "carbox-amido",
                "new": "carboxamido",
                "expected_hits": 1,
                "reason": "Synthetic print line-wrap defect.",
                "evidence": "Synthetic rendered evidence.",
                "hits": 0,
            }
        ]
        repaired = _apply_supplement_block_text_replacements(
            [
                ContentBlock(
                    block_id="supplement_001-page-001-block-001",
                    kind="text",
                    plain_text="2-carbox-amidoethyl",
                    markdown="2-carbox-<sub>amido</sub>ethyl",
                    source_path="supplement.pdf",
                    source_locator="page=1;native-lines=1-2",
                )
            ],
            replacements,
        )

        self.assertEqual(repaired[0].plain_text, "2-carboxamidoethyl")
        self.assertEqual(repaired[0].markdown, "2-carbox<sub>amido</sub>ethyl")
        self.assertEqual(replacements[0]["hits"], 1)

    def test_invalid_native_engine_fails_closed_as_a_diagnostic_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "checklist.pdf"
            source_bytes = self._native_pdf(path)
            relative = "papers (private)/00001/supplementary/checklist.pdf"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format="application/pdf",
            )

            supplement = extract_supplements(
                [source],
                root / "extraction",
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": relative,
                            "source_sha256": source_hash,
                            "native_text_engine": "guess",
                        }
                    ]
                },
            )[0]

            self.assertEqual(supplement.blocks, [])
            self.assertEqual(
                [warning["code"] for warning in supplement.warnings],
                ["supplement_pdf_native_text_extraction_failed"],
            )

    def test_reviewed_text_replacement_is_exact_and_hit_counted(self) -> None:
        replacements = _supplement_text_replacements(
            {
                "text_replacements": [
                    {
                        "old": "gra�tude",
                        "new": "gratitude",
                        "expected_hits": 2,
                        "reason": "The native font map lost the ti ligature.",
                        "evidence": "Both rendered words read gratitude.",
                    }
                ]
            }
        )
        repaired = _apply_supplement_text_replacements(
            "gra�tude and gra�tude, not gratitude", replacements, count_hits=True
        )
        self.assertEqual(repaired, "gratitude and gratitude, not gratitude")
        _verify_supplement_text_replacements(replacements)

        near_miss = _supplement_text_replacements(
            {
                "text_replacements": [
                    {
                        "old": "gra�tude",
                        "new": "gratitude",
                        "expected_hits": 3,
                        "reason": "The native font map lost the ti ligature.",
                        "evidence": "The rendered word reads gratitude.",
                    }
                ]
            }
        )
        _apply_supplement_text_replacements(
            "gra�tude and gra�tude", near_miss, count_hits=True
        )
        with self.assertRaisesRegex(ValueError, "matched 2 times; expected 3"):
            _verify_supplement_text_replacements(near_miss)

    def test_decoded_replacement_crosses_inline_style_boundary(self) -> None:
        replacements = _supplement_text_replacements(
            {
                "text_replacements": [
                    {
                        "old": "a)achieves",
                        "new": "a) achieves",
                        "expected_hits": 1,
                        "reason": "The native line join omitted a visible word space.",
                        "evidence": "The rendered source shows a word space.",
                    }
                ]
            }
        )

        plain, markdown = _apply_decoded_line_text_replacements(
            "PA30 (Figure SI 2a)achieves close",
            "**PA30** (**Figure SI 2a**)achieves close",
            replacements,
            count_hits=True,
        )

        self.assertEqual(plain, "PA30 (Figure SI 2a) achieves close")
        self.assertEqual(markdown, "**PA30** (**Figure SI 2a**) achieves close")
        _verify_supplement_text_replacements(replacements)

    def test_decoded_replacement_removes_empty_style_left_by_deleted_label(self) -> None:
        replacements = _supplement_text_replacements(
            {
                "text_replacements": [
                    {
                        "old": ", 1643, OEE",
                        "new": ", 1643,",
                        "expected_hits": 1,
                        "reason": "A drawing label was merged into the analytical line.",
                        "evidence": "The rendered source places OEE only in the drawing.",
                    }
                ]
            }
        )

        plain, markdown = _apply_decoded_line_text_replacements(
            "FT-IR 3519, 1643, OEE 1446",
            "FT-IR 3519, 1643, **OEE** 1446",
            replacements,
            count_hits=True,
        )

        self.assertEqual(plain, "FT-IR 3519, 1643, 1446")
        self.assertEqual(markdown, "FT-IR 3519, 1643, 1446")
        _verify_supplement_text_replacements(replacements)

    def test_successful_pdf_replacement_emits_hash_bound_repair_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "checklist.pdf"
            source_bytes = self._native_pdf(path)
            relative = "papers (private)/00001/supplementary/checklist.pdf"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format="application/pdf",
            )

            supplement = extract_supplements(
                [source],
                root / "extraction",
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": relative,
                            "source_sha256": source_hash,
                            "native_text_engine": "pypdf",
                            "text_replacements": [
                                {
                                    "old": "Reporting Summary",
                                    "new": "Reporting overview",
                                    "expected_hits": 1,
                                    "reason": "Synthetic source text defect.",
                                    "evidence": "Synthetic rendered evidence.",
                                }
                            ],
                        }
                    ]
                },
            )[0]

            self.assertEqual(supplement.blocks[0].plain_text, "Reporting overview")
            self.assertEqual(
                supplement.repairs,
                [
                    {
                        "schema_version": "1.0",
                        "repair_id": "supplement_001-text-replacement-001",
                        "mode": "exact_literal",
                        "pattern": "Reporting Summary",
                        "replacement": "Reporting overview",
                        "occurrences": 1,
                        "reason": "Synthetic source text defect.",
                        "evidence": "Synthetic rendered evidence.",
                        "source_path": relative,
                        "source_sha256": source_hash,
                        "source_locator": "native-text;exact-literal-replacement",
                    }
                ],
            )

    def test_pdfplumber_stream_supports_exact_text_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "checklist.pdf"
            source_bytes = self._native_pdf(path)
            relative = "papers (private)/00001/supplementary/checklist.pdf"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=relative,
                size=len(source_bytes),
                sha256=source_hash,
                detected_format="application/pdf",
            )

            supplement = extract_supplements(
                [source],
                root / "extraction",
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": relative,
                            "source_sha256": source_hash,
                            "text_stream": "pdfplumber",
                            "text_replacements": [
                                {
                                    "old": "Reporting Summary",
                                    "new": "Reporting overview",
                                    "expected_hits": 1,
                                    "reason": "Synthetic source text defect.",
                                    "evidence": "Synthetic rendered evidence.",
                                }
                            ],
                        }
                    ]
                },
            )[0]

            self.assertEqual(supplement.blocks[0].plain_text, "Reporting overview")
            self.assertEqual(supplement.warnings, [])
            self.assertEqual(supplement.repairs[0]["occurrences"], 1)

    def test_pdfplumber_stream_deduplicates_overpainted_glyphs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "overpainted.pdf"
            document = canvas.Canvas(str(path))
            for offset in (0.0, 0.12, 0.24, 0.36):
                document.drawString(72 + offset, 740, "Supporting Information")
            document.save()
            source_bytes = path.read_bytes()
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path=(
                    "papers (private)/00001/supplementary/overpainted.pdf"
                ),
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format="application/pdf",
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(
                [block.plain_text for block in supplement.blocks],
                ["Supporting Information"],
            )
            self.assertEqual(supplement.warnings, [])

    def test_inline_raster_glyph_does_not_hide_native_text_line(self) -> None:
        page = SimpleNamespace(
            images=[
                {"x0": 100, "top": 200, "x1": 103, "bottom": 208},
                {"x0": 300, "top": 300, "x1": 500, "bottom": 500},
            ],
            lines=[],
            curves=[],
            rects=[],
        )

        regions = _figure_graphic_regions(
            page,
            protected_text_regions=[(72, 198, 540, 212)],
        )

        self.assertEqual(regions, [(300.0, 300.0, 500.0, 500.0)])

    def test_standalone_media_is_preserved_without_an_ocr_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "original_data.jpg"
            source_bytes = b"synthetic-image-payload"
            path.write_bytes(source_bytes)
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path="papers (private)/00001/supplementary/original_data.jpg",
                size=len(source_bytes),
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_format="image/jpeg",
            )

            supplement = extract_supplements([source], root / "extraction")[0]

            self.assertEqual(supplement.blocks, [])
            self.assertEqual(supplement.warnings, [])
            self.assertEqual(
                (root / "extraction" / supplement.copied_path).read_bytes(),
                source_bytes,
            )


if __name__ == "__main__":
    unittest.main()
