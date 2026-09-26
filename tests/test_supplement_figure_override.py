from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.extraction.models import SourceFile, SupplementExtraction
from scripts.extraction.pipeline import (
    ExtractionError,
    _apply_supplement_figure_overrides,
)


class SupplementFigureOverrideTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        path = root / "figure.doc"
        path.write_bytes(b"legacy-word-figure")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source = SourceFile(
            role="supplement",
            path=path,
            relative_path="papers (private)/00001/supplementary/figure.doc",
            size=path.stat().st_size,
            sha256=digest,
            detected_format="application/msword",
        )
        asset_id = "supplement_001_embedded_visual_001"
        output_path = "figures/supplement_001/embedded_visual_001.png"
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=source,
            copied_path="supplementary/supplement_001/figure.doc",
            blocks=[],
            figures=[],
            warnings=[],
            assets=[
                {
                    "asset_id": asset_id,
                    "category": "supplement_image",
                    "label": "Embedded DOCX visual 1",
                    "output_path": output_path,
                }
            ],
            asset_ids=[asset_id],
        )
        spec = {
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "asset_id": asset_id,
            "expected_asset_category": "supplement_image",
            "expected_asset_output_path": output_path,
            "label": "Figure S1",
            "caption_plain": "Supplementary Figure S1",
            "caption_markdown": "Supplementary Figure S1",
            "source_locator": "page=1;single embedded visual;reviewed",
            "reason": "The source review establishes the authored figure identity.",
            "evidence": "Exact source hash, visual inspection, and article cross-reference.",
        }
        return temporary, source, supplement, spec

    def test_promotes_exact_preserved_visual_without_changing_pixels(self):
        temporary, source, supplement, spec = self.fixture()
        self.addCleanup(temporary.cleanup)

        _apply_supplement_figure_overrides([supplement], [spec], [source])

        self.assertEqual(len(supplement.figures), 1)
        self.assertEqual(supplement.figures[0].figure_id, spec["asset_id"])
        self.assertEqual(supplement.figures[0].label, "Figure S1")
        self.assertEqual(supplement.assets[0]["category"], "supplement_figure")
        self.assertEqual(
            supplement.assets[0]["output_path"], spec["expected_asset_output_path"]
        )

    def test_rejects_stale_source_hash(self):
        temporary, source, supplement, spec = self.fixture()
        self.addCleanup(temporary.cleanup)
        spec["source_sha256"] = "0" * 64

        with self.assertRaisesRegex(
            ExtractionError, "invalid supplement_figure_overrides"
        ):
            _apply_supplement_figure_overrides([supplement], [spec], [source])


if __name__ == "__main__":
    unittest.main()
