import base64
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image
from scripts.extraction.html_extractor import extract_html


class FigureInlineSymbolAssetExclusionTests(unittest.TestCase):
    def extract(self, caption_markup, alt="radical dot"):
        output = io.BytesIO()
        Image.new("RGB", (4, 3), (20, 40, 60)).save(output, format="PNG")
        original = output.getvalue()
        payload = base64.b64encode(original).decode("ascii")
        symbol = f'<img alt="{alt}" src="data:image/png;base64,{payload}">'
        source = (
            '<body><article><h1>Inline caption symbol</h1><section><h2>Results</h2>'
            f'<figure id="fig1"><span><img src="data:image/png;base64,{payload}"></span>'
            + caption_markup.format(symbol=symbol)
            + '</figure></section></article></body>'
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(source, encoding="utf-8")
            result = extract_html(path, "html/main.html")
        return result, original

    def test_caption_dot_remains_text_without_becoming_a_false_panel(self):
        for caption in (
            '<figcaption><p>Figure 1. Tris{symbol}HCl.</p></figcaption>',
            '<span><span><p><span>Figure 1</span>. Tris{symbol}HCl.</p></span></span>',
        ):
            with self.subTest(caption=caption):
                result, original = self.extract(caption)
                self.assertEqual(len(result.embedded_assets), 1)
                self.assertEqual(result.embedded_assets[0].data, original)
                self.assertNotIn("|", result.embedded_assets[0].source_locator)
                self.assertIn("Tris·HCl", result.figures[0].caption_plain)

    def test_non_symbol_panel_is_not_dropped_by_description_substring(self):
        result, original = self.extract(
            '<span>{symbol}</span><figcaption>Figure 1. Two panels.</figcaption>',
            alt="radical dot reaction panel",
        )
        self.assertNotEqual(result.embedded_assets[0].data, original)
        self.assertIn("|", result.embedded_assets[0].source_locator)
        with Image.open(io.BytesIO(result.embedded_assets[0].data)) as image:
            self.assertEqual(image.size, (4, 6))
