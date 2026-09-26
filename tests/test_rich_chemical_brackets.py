import unittest

from scripts.extraction.rich_text import inline_markup_to_safe_html, plain_text_from_safe_html


class RichChemicalBracketTests(unittest.TestCase):
    def test_styled_parenthetical_is_not_a_markdown_link_target(self):
        source = '<strong>N-[hydroxypropyl](<em>tert</em>-butoxy)carboxamide.</strong>'
        rendered = inline_markup_to_safe_html(source)
        self.assertEqual(rendered, source)
        self.assertEqual(plain_text_from_safe_html(rendered), 'N-[hydroxypropyl](tert-butoxy)carboxamide.')

    def test_actual_relative_and_external_links_remain_links(self):
        self.assertEqual(inline_markup_to_safe_html('[data](assets/data.txt)'), '<a href="assets/data.txt">data</a>')
        self.assertEqual(inline_markup_to_safe_html('[source](https://example.org/paper)'), '<a href="https://example.org/paper">source</a>')
