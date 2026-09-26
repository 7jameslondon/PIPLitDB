import unittest
from scripts.extraction.pdf_text_extractor import _DecodedChar, _infer_scripts, _render_markdown
from scripts.extraction.rich_text import inline_markup_to_safe_html, validate_safe_html_fragment, UnsafeRichTextError
from scripts.extraction.validation import _SafeRecordHtmlParser


class CompactWordSubscriptTests(unittest.TestCase):
    def test_small_lowered_origin_with_shared_glyph_bottom(self):
        for text in ('2', 'd', 'nt'):
            chars = [_DecodedChar(c, {'size':12,'top':239.372,'bottom':251.372,'matrix':(1,0,0,1,i*6,543.22)},False,False) for i,c in enumerate('value')]
            chars.extend(_DecodedChar(c, {'size':8.04,'top':243.43664,'bottom':251.47664,'matrix':(1,0,0,1,30+i*4,542.26)},False,False) for i,c in enumerate(text))
            _infer_scripts(chars)
            self.assertEqual(_render_markdown(chars), f'value<sub>{text}</sub>')

    def test_color_is_preserved_but_other_styles_and_attributes_are_rejected(self):
        value='<span style="color:#FF0000"><u>ACGT</u></span>'
        expected='<span style="color:#ff0000"><u>ACGT</u></span>'
        self.assertEqual(inline_markup_to_safe_html(value), expected)
        validate_safe_html_fragment(expected)
        parser = _SafeRecordHtmlParser()
        parser.feed(expected)
        self.assertEqual(parser.finish(), ())
        self.assertEqual(parser.visible_text, 'ACGT')
        for unsafe in ('<span style="color:red">A</span>', '<span style="color:#ff0000;background:url(x)">A</span>', '<span onclick="x">A</span>', '<span style="color:#ff0000" id="x">A</span>'):
            with self.subTest(unsafe=unsafe), self.assertRaises(UnsafeRichTextError):
                validate_safe_html_fragment(unsafe)
            parser = _SafeRecordHtmlParser()
            parser.feed(unsafe)
            self.assertTrue(parser.finish())


if __name__ == '__main__':
    unittest.main()
