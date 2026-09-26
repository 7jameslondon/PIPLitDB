from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from scripts.extraction.models import SourceFile
from scripts.extraction.postscript_supplement import RenderedPostscript
from scripts.extraction.supplements import extract_supplements


class StandaloneImageSupplementTests(unittest.TestCase):
    @staticmethod
    def _source(root: Path, payload: bytes) -> SourceFile:
        path = root / "source.tif"
        path.write_bytes(payload)
        return SourceFile(
            role="supplement",
            path=path,
            relative_path="papers (private)/00001/supplementary/source.tif",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            detected_format="image/tiff",
        )

    @staticmethod
    def _spec(source: SourceFile, **changes: object) -> dict[str, object]:
        spec: dict[str, object] = {
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "asset_id": "supplement_001_figure_s1",
            "label": "Figure S1",
            "caption_plain": "Supplementary Figure 1",
            "caption_markdown": "Supplementary Figure 1",
            "source_locator": "frame=1;visible-heading=Supplementary Figure 1;reviewed",
            "output_path": "supplementary/supplement_001/figures/figure_s1.png",
            "expected_frames": 1,
            "frame": 1,
            "kind": "figure",
            "reason": "The publisher identifies this standalone TIFF as Figure S1.",
            "evidence": "Exact source hash and visible heading.",
        }
        spec.update(changes)
        return spec

    @staticmethod
    def _postscript_source(root: Path) -> SourceFile:
        payload = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 2 2\n"
        path = root / "source.eps"
        path.write_bytes(payload)
        return SourceFile(
            role="supplement",
            path=path,
            relative_path="papers (private)/00001/supplementary/source.eps",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            detected_format="application/postscript",
        )

    @staticmethod
    def _postscript_renderer(
        source_path: Path, working_root: Path, dpi: int
    ) -> RenderedPostscript:
        del source_path, working_root, dpi
        output = io.BytesIO()
        Image.new("RGB", (7, 5), (21, 34, 55)).save(output, format="PNG")
        return RenderedPostscript(
            png_bytes=output.getvalue(),
            pixel_width=7,
            pixel_height=5,
            renderer="test compositor",
            renderer_version="1",
        )

    def test_reviewed_tiff_is_preserved_and_gets_lossless_display_figure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.BytesIO()
            Image.new("RGB", (5, 3), (17, 61, 143)).save(
                buffer, format="TIFF", compression="tiff_lzw"
            )
            payload = buffer.getvalue()
            source = self._source(root, payload)
            extraction_root = root / "extraction"

            supplement = extract_supplements(
                [source],
                extraction_root,
                standalone_image_specs=[self._spec(source)],
            )[0]

            copied = extraction_root / supplement.copied_path
            self.assertEqual(copied.read_bytes(), payload)
            self.assertEqual(len(supplement.figures), 1)
            self.assertEqual(len(supplement.assets), 1)
            figure = supplement.figures[0]
            asset = supplement.assets[0]
            self.assertEqual(figure.label, "Figure S1")
            self.assertEqual(figure.caption_plain, "Supplementary Figure 1")
            self.assertEqual(asset["category"], "figure")
            self.assertEqual(asset["parent_id"], "supplement_001")
            self.assertEqual(asset["ocr_performed"], False)
            self.assertEqual((asset["width"], asset["height"]), (5, 3))
            display = extraction_root / str(asset["output_path"])
            self.assertEqual(hashlib.sha256(display.read_bytes()).hexdigest(), asset["sha256"])
            with Image.open(copied) as original, Image.open(display) as rendered:
                self.assertIsNone(
                    ImageChops.difference(
                        original.convert("RGB"), rendered.convert("RGB")
                    ).getbbox()
                )

    def test_reviewed_tiff_rejects_a_stale_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.BytesIO()
            Image.new("RGB", (2, 2), "white").save(buffer, format="TIFF")
            source = self._source(root, buffer.getvalue())

            with self.assertRaisesRegex(ValueError, "invalid standalone_image_figures"):
                extract_supplements(
                    [source],
                    root / "extraction",
                    standalone_image_specs=[
                        self._spec(source, source_sha256="0" * 64)
                    ],
                )

    def test_reviewed_safe_svg_is_preserved_as_a_browser_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            payload = (
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 5">'
                b'<path d="M0 0h8v5H0z" fill="#123456"/></svg>'
            )
            path = root / "source.svg"
            path.write_bytes(payload)
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path="papers (private)/00001/supplementary/source.svg",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                detected_format="image/svg+xml",
            )
            extraction_root = root / "extraction"
            spec = self._spec(
                source,
                output_path="supplementary/supplement_001/figures/figure_s1.svg",
            )

            supplement = extract_supplements(
                [source], extraction_root, standalone_image_specs=[spec]
            )[0]

            asset = supplement.assets[0]
            self.assertEqual(asset["media_type"], "image/svg+xml")
            self.assertEqual(asset["ocr_performed"], False)
            self.assertEqual(
                (extraction_root / str(asset["output_path"])).read_bytes(), payload
            )

    def test_multiframe_tiff_requires_complete_reviewed_frame_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            buffer = io.BytesIO()
            first = Image.new("RGB", (2, 2), "red")
            second = Image.new("RGB", (2, 2), "blue")
            first.save(buffer, format="TIFF", save_all=True, append_images=[second])
            source = self._source(root, buffer.getvalue())

            with self.assertRaisesRegex(ValueError, "frame coverage is incomplete"):
                extract_supplements(
                    [source],
                    root / "extraction",
                    standalone_image_specs=[
                        self._spec(source, expected_frames=2, frame=1)
                    ],
                )

    def test_reviewed_postscript_figure_gets_a_lossless_browser_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = self._postscript_source(root)
            extraction_root = root / "extraction"

            supplement = extract_supplements(
                [source],
                extraction_root,
                standalone_image_specs=[self._spec(source)],
                postscript_renderer=self._postscript_renderer,
            )[0]

            self.assertEqual(
                (extraction_root / supplement.copied_path).read_bytes(),
                source.path.read_bytes(),
            )
            self.assertEqual(len(supplement.figures), 1)
            self.assertEqual(len(supplement.assets), 1)
            self.assertEqual(supplement.assets[0]["media_type"], "image/png")
            self.assertEqual(supplement.assets[0]["ocr_performed"], False)
            self.assertEqual(
                (supplement.assets[0]["width"], supplement.assets[0]["height"]),
                (7, 5),
            )

    def test_reviewed_postscript_table_keeps_structure_and_source_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = self._postscript_source(root)
            extraction_root = root / "extraction"
            table = {
                "number": "S1",
                "page": 1,
                "title_plain": "Table S1. Measurements.",
                "title_markdown": "Table S1. Measurements.",
                "source_locator": "postscript-page=1;visible-table=S1",
                "source_kind": "image",
                "reason": "The publisher supplied the table as an EPS visual.",
                "evidence": "Reviewed exact source hash and positioned EPS text.",
                "parts": [
                    {
                        "rows": [
                            [
                                {"text": "Agent", "header": True},
                                {"text": "Value", "header": True},
                            ],
                            [{"text": "ARE-1"}, {"text": "2.5"}],
                        ]
                    }
                ],
                "footnotes": [],
            }

            supplement = extract_supplements(
                [source],
                extraction_root,
                pdf_text_config={
                    "supplements": [
                        {
                            "source_path": source.relative_path,
                            "source_sha256": source.sha256,
                            "table_overrides": [table],
                        }
                    ]
                },
                postscript_renderer=self._postscript_renderer,
            )[0]

            self.assertEqual(len(supplement.tables), 1)
            self.assertEqual(
                [[cell.text for cell in row] for row in supplement.tables[0].parts[0].rows],
                [["Agent", "Value"], ["ARE-1", "2.5"]],
            )
            self.assertEqual(supplement.tables[0].source_kind, "image")
            self.assertEqual(len(supplement.assets), 1)
            self.assertEqual(
                supplement.assets[0]["asset_id"], supplement.tables[0].table_id
            )
            self.assertEqual(supplement.assets[0]["category"], "supplement_table")


if __name__ == "__main__":
    unittest.main()
