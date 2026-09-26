import unittest
from scripts.extraction.pdf_text_extractor import _DecodedChar, _render_markdown
from scripts.extraction.rich_text import inline_markup_to_safe_html


class AdjacentPdfEmphasisTests(unittest.TestCase):
    def test_adjacent_italic_then_bold_retains_both_without_crossing_tags(self):
        chars = [_DecodedChar('segment-', {}, False, True),
                 _DecodedChar('X', {}, True, False),
                 _DecodedChar('-tail', {}, False, True)]
        rendered = inline_markup_to_safe_html(_render_markdown(chars))
        self.assertEqual(rendered, '<em>segment-</em><strong>X</strong><em>-tail</em>')

    def test_adjacent_bold_then_italic_and_literal_star(self):
        chars = [_DecodedChar('X', {}, True, False),
                 _DecodedChar('tail*', {}, False, True)]
        rendered = inline_markup_to_safe_html(_render_markdown(chars))
        self.assertEqual(rendered, '<strong>X</strong><em>tail*</em>')

    def test_scripted_style_boundary_remains_balanced(self):
        sub = _DecodedChar('a', {}, False, True)
        sub.script = 'sub'
        chars = [_DecodedChar('K', {}, False, True), sub,
                 _DecodedChar('X', {}, True, False)]
        self.assertEqual(inline_markup_to_safe_html(_render_markdown(chars)),
                         '<em>K</em><sub><em>a</em></sub><strong>X</strong>')
