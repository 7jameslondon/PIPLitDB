from lxml import html
import unittest

from scripts.extraction.html_extractor import plain_text, render_inline
from scripts.extraction.rich_text import rich_text_matches_plain


class EmphasizedPunctuationTests(unittest.TestCase):
    def test_split_journal_punctuation_preserves_plain_rich_parity(self):
        element = html.fromstring('<p><em>Chem</em> <em>.</em> <em>Biol.</em> 1997.</p>')
        rich = render_inline(element, markup='html')
        self.assertEqual(plain_text(element), 'Chem. Biol. 1997.')
        self.assertTrue(rich_text_matches_plain(plain_text(element), rich))
        self.assertIn('Chem.', rich)

    def test_decimal_and_script_boundaries_remain_distinct(self):
        for source, expected in [
            ('<p>x <em>.5</em></p>', 'x <em>.5</em>'),
            ('<p>x <sup>.</sup></p>', 'x <sup>.</sup>'),
            ('<p>x <em>word</em></p>', 'x <em>word</em>'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(render_inline(html.fromstring(source), markup='html'), expected)
