from pathlib import Path
import re
import unittest


class ViewerUnicodeFallbackTests(unittest.TestCase):
    def test_caption_text_keeps_scientific_symbol_font_fallback(self):
        viewer = (Path(__file__).resolve().parents[1] / 'extraction_viewer.html').read_text(encoding='utf-8')
        style = re.search(r'\.text-block\s*\{([^}]+)\}', viewer).group(1)
        fonts = re.search(r'font-family:\s*([^;]+)', style).group(1)
        self.assertLess(fonts.index('Georgia'), fonts.index('Segoe UI Symbol'))
        self.assertLess(fonts.index('Segoe UI Symbol'), fonts.index('serif'))
        # The same fallback reaches UI captions in the image lightbox.
        self.assertRegex(viewer, r'font-family: Inter,[^;]+"Segoe UI Symbol"')
        self.assertIn('function renderEnclosingMarks(node)', viewer)
        self.assertRegex(viewer, r'\.enclosed-symbol\s*\{[^}]+border-radius:\s*50%')
        self.assertIn('symbol.textContent = part.slice(0, -1)', viewer)
        self.assertIn('mark.textContent = "\\u20dd"', viewer)
