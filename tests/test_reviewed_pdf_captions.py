from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter

from scripts.extraction.metadata import RecordMetadata
from scripts.extraction.ocr import (
    OcrConfigurationError,
    OcrEngineDiagnostics,
    OcrPolicyError,
    ocr_pdf_from_config,
    parse_pdf_ocr_config,
)
from scripts.extraction.pdf_article_extractor import (
    PdfArticleExtractionError,
    _figures,
    extract_pdf_article,
)
from scripts.extraction.pdf_text_extractor import (
    PdfTextDocument,
    PdfTextLine,
    PdfTextPage,
)
from scripts.extraction.renderer import render_record


SOURCE = "papers (private)/00001/pdf/main.pdf"


def _crop() -> dict:
    return {
        "asset_id": "scheme_001",
        "category": "scheme",
        "label": "Scheme 1",
        "source_path": SOURCE,
        "page": 1,
        "box": [20, 20, 80, 80],
        "caption_box": [22, 22, 78, 30],
        "caption_reviewed_value": "Scheme 1. Reviewed β label.",
    }


def _config() -> dict:
    return {
        "dpi": 300,
        "page_regions": [
            {
                "page": 1,
                "reading_order": [
                    {"region_id": "body", "box": [0, 0, 100, 100]}
                ],
            }
        ],
    }


def _document() -> PdfTextDocument:
    return PdfTextDocument(
        relative_path=SOURCE,
        pages=[
            PdfTextPage(
                page=1,
                width=100,
                height=100,
                classification="image_only",
                lines=[
                    PdfTextLine(
                        page=1,
                        bbox=(0, 0, 90, 8),
                        plain_text="INTRODUCTION",
                        markdown="INTRODUCTION",
                        source_locator="PDF page 1, box [0, 0, 90, 8]",
                        source_region_id="body",
                    ),
                    PdfTextLine(
                        page=1,
                        bbox=(0, 10, 90, 18),
                        plain_text="Document text remains intact.",
                        markdown="Document text remains intact.",
                        source_locator="PDF page 1, box [0, 10, 90, 18]",
                        source_region_id="body",
                    ),
                ],
            )
        ],
        diagnostic_rows=[],
        warnings=[],
        all_pages_classified=True,
    )


class CapturingEngine:
    def __init__(self) -> None:
        self.images: list[Image.Image] = []

    def recognize(self, image: Image.Image) -> tuple:
        self.images.append(image.copy())
        return ()

    def diagnostics(self) -> OcrEngineDiagnostics:
        return OcrEngineDiagnostics(
            name="synthetic",
            version="1.0",
            backend="synthetic",
            backend_version="1.0",
            network_access="disabled",
            execution_provider="test",
        )

    def close(self) -> None:
        for image in self.images:
            image.close()


class ReviewedPdfCaptionTests(unittest.TestCase):
    def test_reviewed_caption_skips_ocr_but_retains_complete_visual_exclusion(self) -> None:
        crop = _crop()
        original = deepcopy(crop)
        plan = parse_pdf_ocr_config(_config(), crop_specs=[crop])

        self.assertEqual([region.region_id for region in plan.regions], ["body"])
        self.assertEqual(len(plan.visual_exclusions), 1)
        self.assertEqual(plan.visual_exclusions[0].box, (20, 20, 80, 80))
        self.assertEqual(plan.visual_exclusions[0].role, "scheme")
        self.assertEqual(crop, original)

    def test_multipart_visual_crop_adds_each_part_as_an_ocr_exclusion(self) -> None:
        crop = _crop()
        crop.pop("page")
        crop.pop("box")
        crop["parts"] = [
            {"page": 1, "box": [20, 20, 80, 40]},
            {"page": 1, "box": [50, 45, 80, 80]},
        ]
        original = deepcopy(crop)

        plan = parse_pdf_ocr_config(_config(), crop_specs=[crop])

        self.assertEqual(
            [item.exclusion_id for item in plan.visual_exclusions],
            ["crop-scheme_001-part-001", "crop-scheme_001-part-002"],
        )
        self.assertEqual(
            [item.box for item in plan.visual_exclusions],
            [(20, 20, 80, 40), (50, 45, 80, 80)],
        )
        self.assertEqual(crop, original)

    def test_ordinary_caption_remains_separate_ocr_target(self) -> None:
        crop = _crop()
        del crop["caption_reviewed_value"]
        crop["caption_box"] = [20, 82, 80, 90]
        plan = parse_pdf_ocr_config(_config(), crop_specs=[crop])

        self.assertEqual(
            [region.region_id for region in plan.regions],
            ["body", "caption-scheme_001"],
        )
        caption = plan.regions[1]
        self.assertEqual((caption.role, caption.asset_id), ("caption", "scheme_001"))
        self.assertEqual(caption.box, (20, 82, 80, 90))

    def test_explicit_invalid_reviewed_values_fail_in_planner_and_article_parser(self) -> None:
        for value in (None, "", " \n\t", 123, False, [], {}):
            with self.subTest(value=value):
                crop = {**_crop(), "caption_reviewed_value": value}
                with self.assertRaisesRegex(OcrConfigurationError, "nonempty text"):
                    parse_pdf_ocr_config(_config(), crop_specs=[crop])
                with self.assertRaisesRegex(PdfArticleExtractionError, "nonempty text"):
                    _figures(_document(), SOURCE, [crop])

    def test_reviewed_caption_still_requires_valid_geometry(self) -> None:
        cases = [
            {"caption_box": None},
            {"caption_box": [22, 22, 78]},
            {"caption_box": [78, 22, 22, 30]},
            {"caption_box": [22, float("nan"), 78, 30]},
            {"caption_page": False},
            {"caption_page": 0},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                with self.assertRaises(OcrConfigurationError):
                    parse_pdf_ocr_config(_config(), crop_specs=[{**_crop(), **updates}])

    def _run_ocr(self, crop: dict, engine: CapturingEngine):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(source)
            with patch(
                "scripts.extraction.ocr._render_pdf_region",
                side_effect=lambda *args, **kwargs: Image.new("RGB", (100, 100), "black"),
            ):
                return ocr_pdf_from_config(source, SOURCE, _config(), engine, crop_specs=[crop])

    def test_reviewed_caption_never_reaches_engine_and_all_visual_pixels_stay_masked(self) -> None:
        engine = CapturingEngine()
        self.addCleanup(engine.close)
        run = self._run_ocr(_crop(), engine)

        self.assertEqual([region.region_id for region in run.regions], ["body"])
        self.assertEqual(len(engine.images), 1)
        captured = engine.images[0]
        with captured.crop((20, 20, 80, 80)) as visual:
            self.assertEqual(visual.getextrema(), ((255, 255),) * 3)
        self.assertEqual(captured.getpixel((10, 10)), (0, 0, 0))
        self.assertEqual(run.regions[0].masked_exclusions[0]["box"], [20, 20, 80, 80])

    def test_unreviewed_overlapping_caption_still_fails_before_caption_ocr(self) -> None:
        engine = CapturingEngine()
        self.addCleanup(engine.close)
        crop = _crop()
        del crop["caption_reviewed_value"]
        with self.assertRaisesRegex(OcrPolicyError, "wholly inside"):
            self._run_ocr(crop, engine)
        self.assertEqual(len(engine.images), 1)  # Only the safely masked body ran.

    def test_ordinary_safe_caption_still_reaches_engine(self) -> None:
        engine = CapturingEngine()
        self.addCleanup(engine.close)
        crop = _crop()
        del crop["caption_reviewed_value"]
        crop["caption_box"] = [20, 82, 80, 90]
        run = self._run_ocr(crop, engine)

        self.assertEqual(len(engine.images), 2)
        self.assertEqual([region.role for region in run.regions], ["document_text", "caption"])
        self.assertEqual(run.regions[1].masked_exclusions, ())
        self.assertEqual(engine.images[1].getextrema(), ((0, 0),) * 3)

    def test_reviewed_text_geometry_and_caption_coverage_survive_without_ocr_lines(self) -> None:
        metadata = RecordMetadata(
            record_id="00001",
            title="Synthetic article",
            authors=("Test Author",),
            journal="Synthetic Journal",
            publication_year=2000,
            doi="10.0000/test",
            document_type="research_article",
        )
        article = extract_pdf_article(
            Path("unused.pdf"), SOURCE, metadata,
            crop_specs=[_crop()], text_document=_document(),
        )
        self.assertEqual(len(article.figures), 1)
        figure = article.figures[0]
        self.assertEqual(figure.caption_plain, _crop()["caption_reviewed_value"])
        self.assertEqual(figure.caption_markdown, figure.caption_plain)
        self.assertEqual(figure.source_path, SOURCE)
        locator = "PDF page 1, caption box [22.00, 22.00, 78.00, 30.00]"
        self.assertEqual(figure.source_locator, locator)
        self.assertEqual(article.sections[0].blocks[0].plain_text, "Document text remains intact.")
        rendered = render_record(metadata, article, [], [])
        caption_rows = [row for row in rendered.coverage if row["coverage_id"] == "caption-scheme_001"]
        self.assertEqual(len(caption_rows), 1)
        self.assertEqual(caption_rows[0]["source_locator"], locator)
        self.assertEqual(caption_rows[0]["source_path"], SOURCE)
        self.assertEqual(caption_rows[0]["content_kind"], "scheme_caption")
        self.assertEqual(caption_rows[0]["status"], "included")


if __name__ == "__main__":
    unittest.main()
