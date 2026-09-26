from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html
from scripts.extraction.rich_text import block_markup_to_safe_html, plain_text_from_safe_html


class AcsNestedScriptContinuationsTests(unittest.TestCase):
    def test_nested_temperature_subscript_survives_styled_siblings(self):
        source = '''<html><body><article><h1>Example</h1><h2>Results</h2>
        <p>Constant (<em>K<sub>T</sub></em><sub><sub>m</sub></sub>) follows.</p>
        <div content-id="bi000e00001"><p>Equation</p></div>
        <p>Separated K<sub>T</sub> <sub><sub>m</sub></sub>.</p>
        </article></body></html>'''
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'main.html'
            path.write_text(source, encoding='utf-8')
            result = extract_html(path, 'main.html')
            self.assertEqual(path.read_text(encoding='utf-8'), source)
        blocks = [b for s in result.sections for b in s.blocks]
        combined = ' '.join(b.plain_text for b in blocks)
        self.assertIn('K_{T_{m}}', combined)
        for block in blocks:
            if 'Constant' in block.plain_text:
                rich = block_markup_to_safe_html(block.markdown, kind=block.kind)
                self.assertIn('K_{T_{m}}', plain_text_from_safe_html(rich))
        self.assertIn('Separated K_{T} _{m}', combined)
