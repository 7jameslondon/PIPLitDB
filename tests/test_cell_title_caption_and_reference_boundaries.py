import unittest
from unittest.mock import patch

from lxml import html

from scripts.extraction.html_extractor import (
    _cell_press_semantic_reference_blocks,
    _figure_caption,
)


class CellTitleCaptionAndReferenceBoundaryTests(unittest.TestCase):
    def test_title_only_figure_keeps_label_out_of_caption(self) -> None:
        document = html.fromstring(
            """<article><figure id="FIG1">
            <a><img src="data:image/jpeg;base64,AA=="/></a><div></div>
            <figcaption><div id="FIG1-title"><span>Figure 1</span>
            <span>Structures and Synthesis of Conjugates <b>1</b>, <b>2</b>, and <b>3</b></span>
            </div></figcaption></figure></article>"""
        )
        with patch(
            "scripts.extraction.html_extractor._cell_press_header_details",
            return_value={},
        ):
            label, rich, plain = _figure_caption(document.xpath(".//figure")[0])

        self.assertEqual(label, "Figure 1")
        self.assertEqual(
            plain, "Structures and Synthesis of Conjugates 1, 2, and 3"
        )
        self.assertNotIn("Figure 1", rich)

    def test_semantic_reference_restores_title_subtitle_colon(self) -> None:
        document = html.fromstring(
            """<article data-extraction-dialect="cell-press-semantic">
            <section id="references"><div id="bibliography" role="doc-bibliography"><div>
            <div><div id="BIB1"><div><div><div>
              <a href="#body-ref-BIB1" title="View in article">1.</a></div>
              <div>A. Author</div>
              <div><strong>Main title</strong>subtitle text</div>
              <div><em>Journal.</em> 2003; <strong>1</strong>:1-2</div>
            </div><div><a href="https://example.org" target="_blank">Index</a></div>
            </div></div></div></div></div></section></article>"""
        )
        with patch(
            "scripts.extraction.html_extractor._cell_press_header_details",
            return_value={},
        ):
            references = _cell_press_semantic_reference_blocks(
                document.xpath(".//section")[0], "html/main.html"
            )

        self.assertIsNotNone(references)
        self.assertEqual(
            references[0].plain_text,
            "1. A. Author Main title: subtitle text Journal. 2003; 1:1-2",
        )


if __name__ == "__main__":
    unittest.main()
