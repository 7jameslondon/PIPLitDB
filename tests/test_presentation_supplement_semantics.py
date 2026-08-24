from __future__ import annotations

import unittest
from pathlib import Path

from scripts.extraction.models import ContentBlock, SourceFile
from scripts.extraction.supplements import _presentation_caption_semantics


def block(
    identifier: str,
    kind: str,
    text: str,
    paragraph: int,
    *,
    slide: int = 1,
    shape_name: str = "Caption",
) -> ContentBlock:
    return ContentBlock(
        block_id=identifier,
        kind=kind,
        markdown=text,
        plain_text=text,
        source_path="papers (private)/00001/supplementary/figure.pptx",
        source_locator=(
            f"slide={slide};shape-id=9;shape-name={shape_name};paragraph={paragraph}"
        ),
        source_geometry=[{"slide": slide, "x": 1, "y": 2, "width": 3, "height": 4}],
    )


class PresentationSupplementSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SourceFile(
            role="supplement",
            path=Path("figure.pptx"),
            relative_path="papers (private)/00001/supplementary/figure.pptx",
            size=1,
            sha256="0" * 64,
            detected_format=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )

    def test_merges_caption_and_links_complete_slide_render(self) -> None:
        blocks = [
            block("caption-1", "figure_caption", "Figure SI5. Main finding.", 1),
            block("caption-2", "text", "Second caption paragraph.", 2),
            block("label-1", "text", "1", 1, shape_name="Compound label"),
        ]
        assets = [
            {
                "asset_id": "supplement_005_slide_001_render",
                "category": "supplement_slide_render",
                "output_path": "supplementary/supplement_005/figures/slide-001.png",
                "parent_id": "supplement_005",
                "presentation_slide_number": 1,
                "presentation_slide_count": 1,
            },
            {
                "asset_id": "image-1",
                "category": "supplement_image",
                "output_path": "supplementary/supplement_005/media/image1.tiff",
                "parent_id": "supplement_005",
                "presentation_slide_numbers": [1],
            },
            {
                "asset_id": "workbook-1",
                "category": "supplement_data",
                "output_path": "supplementary/supplement_005/embedded/data.xlsx",
                "parent_id": "supplement_005_slide_001_chart_001",
            },
        ]

        retained, figures = _presentation_caption_semantics(
            blocks, assets, self.source, "supplement_005"
        )

        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].kind, "figure_text")
        self.assertEqual(retained[0].plain_text, "1")
        self.assertEqual(retained[0].source_geometry, blocks[2].source_geometry)
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].figure_id, "supplement_005_slide_001_render")
        self.assertEqual(figures[0].label, "Figure SI5")
        self.assertEqual(
            figures[0].caption_plain,
            "Figure SI5. Main finding.\nSecond caption paragraph.",
        )
        self.assertEqual(assets[0]["category"], "figure")
        self.assertEqual(assets[1]["parent_id"], figures[0].figure_id)
        self.assertEqual(assets[2]["parent_id"], "supplement_005_slide_001_chart_001")

    def test_promotes_matching_figure_slide_in_multiple_slide_presentation(self) -> None:
        blocks = [
            block("caption-1", "figure_caption", "Figure SI5. Main finding.", 1)
        ]
        assets = [
            {
                "asset_id": "supplement_005_slide_001_render",
                "category": "supplement_slide_render",
                "output_path": "supplementary/supplement_005/figures/slide-001.png",
                "parent_id": "supplement_005",
                "presentation_slide_number": 1,
                "presentation_slide_count": 2,
            },
            {
                "asset_id": "image-1",
                "category": "supplement_image",
                "output_path": "supplementary/supplement_005/media/image1.tiff",
                "parent_id": "supplement_005",
                "presentation_slide_numbers": [1],
            },
        ]

        retained, figures = _presentation_caption_semantics(
            blocks, assets, self.source, "supplement_005"
        )

        self.assertEqual(retained, [])
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].figure_id, "supplement_005_slide_001_render")
        self.assertEqual(assets[0]["category"], "figure")
        self.assertEqual(assets[1]["parent_id"], figures[0].figure_id)

    def test_does_not_promote_caption_without_matching_slide_render(self) -> None:
        blocks = [
            block(
                "caption-1",
                "figure_caption",
                "Figure SI5. Main finding.",
                1,
                slide=2,
            )
        ]
        assets = [
            {
                "asset_id": "supplement_005_slide_001_render",
                "category": "supplement_slide_render",
                "output_path": "supplementary/supplement_005/figures/slide-001.png",
                "parent_id": "supplement_005",
                "presentation_slide_number": 1,
                "presentation_slide_count": 1,
            }
        ]

        retained, figures = _presentation_caption_semantics(
            blocks, assets, self.source, "supplement_005"
        )

        self.assertEqual(figures, [])
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].plain_text, blocks[0].plain_text)

    def test_keeps_merged_caption_block_when_complete_render_is_unavailable(self) -> None:
        retained, figures = _presentation_caption_semantics(
            [
                block("caption-1", "figure_caption", "Figure SI2. Result.", 1),
                block("caption-2", "text", "Continuation.", 2),
            ],
            [],
            self.source,
            "supplement_002",
        )
        self.assertEqual(figures, [])
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].kind, "figure_caption")
        self.assertEqual(retained[0].plain_text, "Figure SI2. Result.\nContinuation.")


if __name__ == "__main__":
    unittest.main()
