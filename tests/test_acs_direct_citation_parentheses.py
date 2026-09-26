from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html


class AcsDirectCitationParenthesesTests(unittest.TestCase):
    def test_direct_nested_citations_preserve_labels_and_non_citations(self):
        source = '''<html><body><article><h1>Example article</h1>
        <h2>Introduction</h2><p>Claim (<a ref-data-modal-source-id="r1">(1)</a>).
        Detail (<a ref-data-modal-source-id="r17">(17a)</a>).
        Range (<a ref-data-modal-source-id="r23">(2–3)</a>).
        Mixed (<a ref-data-modal-source-id="r28 r36 r37">(28,36–37)</a>).
        Single <a ref-data-modal-source-id="r4">(4)</a>.
        Other (<a href="https://example.org">(5)</a>).
        Math ((x)).</p></article></body></html>'''
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'article.html'
            path.write_text(source, encoding='utf-8')
            result = extract_html(path, 'article.html')
            self.assertEqual(path.read_text(encoding='utf-8'), source)
        text = ' '.join(block.plain_text for section in result.sections for block in section.blocks)
        for expected in ('Claim (1).', 'Detail (17a).', 'Range (2–3).', 'Mixed (28,36–37).', 'Single (4).', 'Math ((x)).'):
            self.assertIn(expected, text)
        self.assertIn('Other ((5)', text)
