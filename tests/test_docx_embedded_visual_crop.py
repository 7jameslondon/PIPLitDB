"""Reviewed native-layout crops also cover uncaptioned Office drawings."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from scripts.extraction.models import SourceFile
from scripts.extraction.docx_supplement import DOCX_MEDIA_TYPE, RenderedDocxFigureCrop
from scripts.extraction.supplements import _apply_reviewed_docx_figure_crops


class EmbeddedVisualCropTests(unittest.TestCase):
    def exercise(self, category="supplement_image", parent="supplement_001", output=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            extraction = root / "extraction"
            original = root / "source.docx"
            original.write_bytes(b"immutable-source")
            relative = "figures/supplement_001/embedded_visual_001.png"
            target = extraction / relative
            target.parent.mkdir(parents=True)
            buffer = io.BytesIO()
            Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
            target.write_bytes(buffer.getvalue())
            asset = {"asset_id": "supplement_001_embedded_visual_001", "category": category,
                     "parent_id": parent, "media_type": "image/png", "output_path": relative,
                     "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                     "source_locator": "word/media/image1.emf"}
            source = SourceFile(role="supplement", path=original, relative_path="source.docx",
                                size=16, sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
                                detected_format=DOCX_MEDIA_TYPE)
            specs = [{"asset_id": asset["asset_id"], "output_path": output or relative,
                      "page": 1, "box": (10., 20., 110., 120.), "expected_page_count": 1}]
            def renderer(path, scratch, requests, dpi):
                self.assertEqual(path.read_bytes(), b"immutable-source")
                result = scratch / "complete.png"
                Image.new("RGB", (20, 20), "blue").save(result)
                request = requests[0]
                return [RenderedDocxFigureCrop(asset_id=request.asset_id, path=result,
                        pixel_width=20, pixel_height=20, page_count=1, page=1,
                        box=request.box, dpi=dpi, renderer="test", renderer_version="1")]
            figures = []
            _apply_reviewed_docx_figure_crops(source=source, supplement_id="supplement_001",
                copied_source=original, extraction_root=extraction, figures=figures,
                assets=[asset], warnings=[], specs=specs, renderer=renderer)
            self.assertEqual(original.read_bytes(), b"immutable-source")
            self.assertEqual(figures, [])  # No invented caption or numbered figure.
            self.assertEqual(asset["render_method"], "reviewed_docx_pdf_crop")
            self.assertEqual(asset["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(Image.open(target).size, (20, 20))

    def test_existing_uncaptioned_visual_can_be_replaced(self):
        self.exercise()
        self.exercise(parent=None)  # Parser assigns parent only during serialization.

    def test_downloads_and_other_supplement_assets_are_rejected(self):
        for category, parent in [("supplement_data", "supplement_001"),
                                 ("supplement_image", "supplement_002")]:
            with self.subTest(category=category, parent=parent), self.assertRaises(ValueError):
                self.exercise(category=category, parent=parent)

    def test_output_must_match_the_existing_visual(self):
        with self.assertRaises(ValueError):
            self.exercise(output="figures/supplement_001/other.png")
