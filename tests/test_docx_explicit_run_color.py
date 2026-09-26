import unittest
from xml.etree import ElementTree as ET
from scripts.extraction.docx_supplement import _paragraph_text
from scripts.extraction.rich_text import inline_markup_to_safe_html


class ExplicitRunColorTests(unittest.TestCase):
    def test_color_and_emphasis_survive_without_changing_plain_text(self):
        node = ET.fromstring('''<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:rPr><w:b/><w:color w:val="0000FF"/></w:rPr><w:t>blue</w:t></w:r><w:r><w:t> and </w:t></w:r><w:r><w:rPr><w:color w:val="FF0000"/></w:rPr><w:t>red</w:t></w:r></w:p>''')
        rich, plain = _paragraph_text(node)
        self.assertEqual(plain, 'blue and red')
        self.assertIn('<span style="color:#0000ff"><strong>blue</strong></span>', inline_markup_to_safe_html(rich))
        self.assertIn('<span style="color:#ff0000">red</span>', inline_markup_to_safe_html(rich))

    def test_auto_and_invalid_colors_are_not_injected_into_html(self):
        for value in ('auto', 'red', 'bad;style'):
            node = ET.fromstring(f'''<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:rPr><w:color w:val="{value}"/></w:rPr><w:t>value</w:t></w:r></w:p>''')
            self.assertEqual(_paragraph_text(node), ('value', 'value'))
