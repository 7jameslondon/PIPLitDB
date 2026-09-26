import unittest
from lxml import html
from scripts.extraction.html_extractor import plain_text, render_inline


class InlineRadicalDotTests(unittest.TestCase):
    def test_accessible_dot_retains_scientific_pairing_and_emphasis(self):
        node = html.fromstring('<p><em>A</em><img alt="radical dot" src="data:image/gif;base64,AA==">T/T<img alt="radical dot" src="symbol.gif">A</p>')
        self.assertEqual(plain_text(node), 'A·T/T·A')
        self.assertEqual(render_inline(node, markup='html'), '<em>A</em>·T/T·A')

    def test_other_image_descriptions_are_not_invented_prose(self):
        node = html.fromstring('<p>A<img alt="Figure 1. radical dot reaction">T</p>')
        self.assertEqual(plain_text(node), 'AT')
        self.assertEqual(render_inline(node, markup='html'), 'AT')
