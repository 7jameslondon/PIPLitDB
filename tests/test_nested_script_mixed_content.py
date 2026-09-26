import unittest

from lxml import html

from scripts.extraction.html_extractor import plain_text, render_inline
from scripts.extraction.rich_text import inline_markup_to_safe_html, plain_text_from_safe_html


class NestedScriptMixedContentTests(unittest.TestCase):
    def test_redundant_wrapper_retains_inner_mixed_script(self):
        for source, expected in (
            ('x<sup><sup>n<sub>G</sub></sup></sup>', 'x^{n_{G}}'),
            ('K<sub><sub>x<sup>2</sup></sub></sub>', 'K_{x^{2}}'),
            ('x<sup>2<sup>n</sup></sup>', 'x^{2^{n}}'),
            ('x<sup><sub>n</sub></sup>', 'x^{_{n}}'),
        ):
            with self.subTest(source=source):
                node = html.fromstring('<p>' + source + '</p>')
                self.assertEqual(plain_text(node), expected)
                self.assertEqual(plain_text_from_safe_html(inline_markup_to_safe_html(render_inline(node))), expected)
